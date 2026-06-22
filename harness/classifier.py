"""Capacity classifier: label each Segment with an abstract capability tag."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .atomizer import Segment
from .event_utils import host_path
from .llm import call_llm_fast, parse_json_from_model
from . import config


@dataclass
class CapacityLabel:
    capacity: str
    description: str
    entry_conditions: list[str]
    exit_conditions: list[str]
    outcome: str
    domain_hints: list[str]


@dataclass
class ClassifiedSegment:
    segment: Segment
    label: CapacityLabel


CLASSIFY_PROMPT = """\
You are a browser-action capability classifier. Given a segment of a human
browser trajectory, identify the abstract CAPABILITY (capacity) this segment
demonstrates.

A capacity is a reusable browser skill pattern, for example:
- login-with-credentials
- compose-and-send-email
- search-filter-select
- fill-checkout-form
- navigate-and-select-date
- signup-registration
- add-item-to-cart
- fill-contact-form

Rules:
- Name the capacity in kebab-case, 2-6 words, focusing on VERB + OBJECT.
- Describe entry/exit conditions as observable browser states.
- outcome: "success" if the segment ends with visible evidence of completion,
  "partial" if it appears unfinished, "unclear" if ambiguous.
{existing_section}
Output JSON only:
{{
  "capacity": "kebab-case-name",
  "description": "1-2 sentence description",
  "entry_conditions": ["condition1", "condition2"],
  "exit_conditions": ["condition1", "condition2"],
  "outcome": "success|partial|unclear",
  "domain_hints": ["hint1"]
}}

## Segment from: {track_id}
## Domain: {domain}
## Entry URL: {entry_url}
## Exit URL: {exit_url}
## Duration: {duration_ms}ms
## Events ({event_count} total):
{event_summary}
"""

EXISTING_CAPACITIES_SECTION = """
## Existing capacity buckets for this website (PREFER these):
{capacity_list}

If the segment matches one of the above, you MUST use the EXACT same capacity
name. Only propose a new name if the segment genuinely does not fit any existing
bucket.
"""


def _build_classify_prompt(
    seg: Segment,
    existing_capacities: list[tuple[str, str]] | None = None,
) -> str:
    existing_section = ""
    if existing_capacities:
        lines = [f"- {name}: {desc}" for name, desc in existing_capacities]
        existing_section = EXISTING_CAPACITIES_SECTION.format(
            capacity_list="\n".join(lines)
        )

    return CLASSIFY_PROMPT.format(
        track_id=seg.source_track_id,
        domain=seg.domain,
        entry_url=host_path(seg.entry_url),
        exit_url=host_path(seg.exit_url),
        duration_ms=seg.duration_ms,
        event_count=len(seg.events),
        event_summary=seg.event_summary,
        existing_section=existing_section,
    )


def classify_segment_sync(
    seg: Segment,
    existing_capacities: list[tuple[str, str]] | None = None,
) -> ClassifiedSegment:
    prompt = _build_classify_prompt(seg, existing_capacities)
    text, _ = call_llm_fast(prompt)
    data = parse_json_from_model(text)
    label = CapacityLabel(
        capacity=data.get("capacity", "unknown"),
        description=data.get("description", ""),
        entry_conditions=data.get("entry_conditions", []),
        exit_conditions=data.get("exit_conditions", []),
        outcome=data.get("outcome", "unclear"),
        domain_hints=data.get("domain_hints", []),
    )
    return ClassifiedSegment(segment=seg, label=label)


async def _classify_one(
    seg: Segment,
    sem: asyncio.Semaphore,
    existing_capacities: list[tuple[str, str]] | None = None,
) -> ClassifiedSegment | None:
    async with sem:
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, classify_segment_sync, seg, existing_capacities
            )
        except Exception as e:
            print(f"[classifier] failed {seg.segment_id}: {e}")
            return None


async def classify_segments(
    segments: list[Segment],
    concurrency: int | None = None,
    existing_capacities: list[tuple[str, str]] | None = None,
) -> list[ClassifiedSegment]:
    """Classify segments with incremental capacity list growth.

    Each classified segment's capacity is added to the list for subsequent
    segments, so later segments can match earlier ones' names.
    """
    caps = list(existing_capacities or [])
    seen_caps: set[str] = {c[0] for c in caps}
    results: list[ClassifiedSegment] = []

    for seg in segments:
        try:
            cs = classify_segment_sync(seg, caps if caps else None)
            results.append(cs)
            if cs.label.capacity not in seen_caps:
                seen_caps.add(cs.label.capacity)
                caps.append((cs.label.capacity, cs.label.description))
        except Exception as e:
            print(f"[classifier] failed {seg.segment_id}: {e}")

    return results
