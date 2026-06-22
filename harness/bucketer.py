"""Bucket classified segments by domain + capacity."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .classifier import ClassifiedSegment
from . import config


@dataclass
class Bucket:
    bucket_id: str
    domain: str
    canonical_capacity: str
    description: str
    segment_ids: list[str] = field(default_factory=list)
    distill_version: int = 0
    last_distilled_at: str | None = None
    dirty: bool = True
    created_at: str | None = None
    last_segment_added_at: str | None = None


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9\-]", "", name.lower().replace("_", "-").replace(" ", "-"))


def _buckets_path() -> Path:
    return config.STATE_DIR / "buckets.json"


def load_buckets() -> dict[str, Bucket]:
    path = _buckets_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    buckets: dict[str, Bucket] = {}
    for bid, bdata in data.get("buckets", {}).items():
        buckets[bid] = Bucket(
            bucket_id=bdata["bucket_id"],
            domain=bdata.get("domain", ""),
            canonical_capacity=bdata["canonical_capacity"],
            description=bdata["description"],
            segment_ids=bdata.get("segment_ids", []),
            distill_version=bdata.get("distill_version", 0),
            last_distilled_at=bdata.get("last_distilled_at"),
            dirty=bdata.get("dirty", True),
            created_at=bdata.get("created_at"),
            last_segment_added_at=bdata.get("last_segment_added_at"),
        )
    return buckets


def save_buckets(buckets: dict[str, Bucket]) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "version": 3,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "buckets": {},
    }
    for bid, b in buckets.items():
        out["buckets"][bid] = {
            "bucket_id": b.bucket_id,
            "domain": b.domain,
            "canonical_capacity": b.canonical_capacity,
            "description": b.description,
            "segment_ids": b.segment_ids,
            "distill_version": b.distill_version,
            "last_distilled_at": b.last_distilled_at,
            "dirty": b.dirty,
            "created_at": b.created_at,
            "last_segment_added_at": b.last_segment_added_at,
        }
    tmp = _buckets_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    tmp.rename(_buckets_path())


def get_domain_capacities(buckets: dict[str, Bucket], domain: str) -> list[tuple[str, str]]:
    """Return (capacity_name, description) pairs for a given domain."""
    return [
        (b.canonical_capacity, b.description)
        for b in buckets.values()
        if b.domain == domain
    ]


def bucket_segments(
    classified: list[ClassifiedSegment],
    existing_buckets: dict[str, Bucket] | None = None,
) -> dict[str, Bucket]:
    buckets = dict(existing_buckets or {})
    now = datetime.now(timezone.utc).isoformat()

    for cs in classified:
        domain = cs.segment.domain
        cap = cs.label.capacity
        bid = f"{domain}::{_slugify(cap)}"
        sid = cs.segment.segment_id

        if bid in buckets:
            if sid not in buckets[bid].segment_ids:
                buckets[bid].segment_ids.append(sid)
                buckets[bid].dirty = True
                buckets[bid].last_segment_added_at = now
        else:
            buckets[bid] = Bucket(
                bucket_id=bid,
                domain=domain,
                canonical_capacity=cap,
                description=cs.label.description,
                segment_ids=[sid],
                created_at=now,
                last_segment_added_at=now,
            )

    return buckets


CONSOLIDATE_PROMPT = """\
You are a skill bucket consolidation engine. A website "{domain}" has accumulated
{count} capacity buckets. Some may be redundant, overlapping, or too granular.

Your job: propose a MERGE MAP that consolidates similar buckets into fewer, more
meaningful ones. Each group of merged buckets gets ONE canonical name.

## Current buckets for {domain}:
{bucket_list}

Rules:
- Merge buckets that describe the SAME user intent (e.g. "login-with-password" and
  "sign-in-with-credentials" are the same thing).
- Merge buckets that are sub-steps of a larger flow IF they always co-occur
  (e.g. "enter-email" + "enter-password" → "login-with-credentials").
- Do NOT merge buckets that are genuinely different capabilities.
- Keep the BEST name from each merge group (most descriptive, verb+object style).
- Cold buckets (marked with ❄) with very few segments are prime merge candidates.
- Output ONLY groups that have 2+ buckets to merge. Buckets not mentioned stay as-is.

Output JSON:
{{
  "merges": [
    {{
      "target": "canonical-bucket-name",
      "target_description": "updated description for merged bucket",
      "sources": ["old-name-1", "old-name-2"],
      "reason": "why these should be merged"
    }}
  ]
}}

If no merges are needed, return {{"merges": []}}.
"""


def consolidate_domain(
    domain: str,
    buckets: dict[str, Bucket],
    min_segment_threshold: int = 2,
    ingest_count: int = 0,
) -> list[dict]:
    """Propose and apply merges for a domain's buckets. Returns the merge list."""
    from .llm import call_llm_fast, parse_json_from_model

    domain_buckets = {
        bid: b for bid, b in buckets.items() if b.domain == domain
    }
    if len(domain_buckets) < 2:
        return []

    bucket_lines = []
    for bid, b in sorted(domain_buckets.items(), key=lambda x: -len(x[1].segment_ids)):
        cold = "❄ " if len(b.segment_ids) <= min_segment_threshold else ""
        bucket_lines.append(
            f"- {cold}{b.canonical_capacity} ({len(b.segment_ids)} segs): {b.description}"
        )

    prompt = CONSOLIDATE_PROMPT.format(
        domain=domain,
        count=len(domain_buckets),
        bucket_list="\n".join(bucket_lines),
    )
    text, _ = call_llm_fast(prompt, max_tokens=4096)
    data = parse_json_from_model(text)
    merges = data.get("merges", [])

    if not merges:
        return []

    applied = []
    for merge in merges:
        target_name = merge.get("target", "")
        target_desc = merge.get("target_description", "")
        sources = merge.get("sources", [])
        if not target_name or len(sources) < 2:
            continue

        target_bid = f"{domain}::{_slugify(target_name)}"

        source_bids = []
        for src in sources:
            src_bid = f"{domain}::{_slugify(src)}"
            if src_bid in buckets:
                source_bids.append(src_bid)

        if len(source_bids) < 2:
            continue

        if target_bid not in buckets:
            first_src = source_bids[0]
            buckets[target_bid] = Bucket(
                bucket_id=target_bid,
                domain=domain,
                canonical_capacity=target_name,
                description=target_desc or buckets[first_src].description,
                segment_ids=[],
                created_at=buckets[first_src].created_at,
                last_segment_added_at=datetime.now(timezone.utc).isoformat(),
            )

        for src_bid in source_bids:
            if src_bid == target_bid:
                continue
            src_bucket = buckets[src_bid]
            for sid in src_bucket.segment_ids:
                if sid not in buckets[target_bid].segment_ids:
                    buckets[target_bid].segment_ids.append(sid)
            buckets[target_bid].dirty = True
            if target_desc:
                buckets[target_bid].description = target_desc
            del buckets[src_bid]

        applied.append({
            "target": target_bid,
            "sources": source_bids,
            "reason": merge.get("reason", ""),
        })

    return applied
