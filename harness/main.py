#!/usr/bin/env python3
"""Distributed multi-trajectory to multi-skill distillation harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path


# Put the repo root on sys.path so `harness` is importable when run directly.
_REPO_ROOT = Path(__file__).resolve().parents[1]   # journey-forge-local/
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from . import config  # noqa: E402
from .adapter import load_human_track, load_track_file, load_tracks
from .atomizer import Segment, segment_trajectory
from .bucketer import Bucket, bucket_segments, consolidate_domain, get_domain_capacities, load_buckets, save_buckets
from .checkpoint import (
    load_checkpoint,
    mark_bucket_distilled,
    mark_segment_classified,
    mark_track_ingested,
    save_checkpoint,
)
from .classifier import ClassifiedSegment, classify_segments
from .distiller import distill_bucket_sync
from .registry import query_top_k, update_registry_entry


def _segments_path() -> Path:
    return config.STATE_DIR / "segments.jsonl"


def _save_segments(segments: list[ClassifiedSegment]) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with _segments_path().open("a") as f:
        for cs in segments:
            row = {
                "segment_id": cs.segment.segment_id,
                "source_track_id": cs.segment.source_track_id,
                "domain": cs.segment.domain,
                "start_idx": cs.segment.start_idx,
                "end_idx": cs.segment.end_idx,
                "boundary_reason": cs.segment.boundary_reason,
                "entry_url": cs.segment.entry_url,
                "exit_url": cs.segment.exit_url,
                "duration_ms": cs.segment.duration_ms,
                "event_count": len(cs.segment.events),
                "event_summary": cs.segment.event_summary,
                "capacity": cs.label.capacity,
                "description": cs.label.description,
                "entry_conditions": cs.label.entry_conditions,
                "exit_conditions": cs.label.exit_conditions,
                "outcome": cs.label.outcome,
                "domain_hints": cs.label.domain_hints,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_segment_map() -> dict[str, ClassifiedSegment]:
    path = _segments_path()
    if not path.exists():
        return {}
    from .atomizer import Segment as Seg
    from .classifier import CapacityLabel, ClassifiedSegment as CS

    seg_map: dict[str, CS] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        seg = Seg(
            segment_id=row["segment_id"],
            source_track_id=row["source_track_id"],
            domain=row["domain"],
            start_idx=row["start_idx"],
            end_idx=row["end_idx"],
            events=[],
            boundary_reason=row["boundary_reason"],
            entry_url=row["entry_url"],
            exit_url=row["exit_url"],
            duration_ms=row["duration_ms"],
            event_summary=row.get("event_summary", ""),
        )
        label = CapacityLabel(
            capacity=row.get("capacity", "unknown"),
            description=row.get("description", ""),
            entry_conditions=row.get("entry_conditions", []),
            exit_conditions=row.get("exit_conditions", []),
            outcome=row.get("outcome", "unclear"),
            domain_hints=row.get("domain_hints", []),
        )
        seg_map[seg.segment_id] = CS(segment=seg, label=label)
    return seg_map


def cmd_ingest(args: argparse.Namespace) -> None:
    cp = load_checkpoint()

    if args.track_file:
        tracks = load_track_file(Path(args.track_file))
    else:
        tracks_dir = Path(args.tracks_dir) if args.tracks_dir else config.TRACKS_DIR
        all_tracks = load_tracks(tracks_dir)
        tracks = [t for t in all_tracks if t.track_id not in cp["ingested_tracks"]]

    if not tracks:
        print("[ingest] no new tracks to process")
        return

    print(f"[ingest] processing {len(tracks)} track(s)")

    existing_buckets = load_buckets()

    all_segments: list[Segment] = []
    for track in tracks:
        segs = segment_trajectory(track)
        all_segments.extend(segs)
        mark_track_ingested(cp, track.track_id)
        cp["pipeline"]["atomize"]["completed"] += 1
        print(f"  {track.track_id}: {len(segs)} segments")

    # Group segments by domain, classify each group with its domain's existing buckets
    by_domain: dict[str, list[Segment]] = defaultdict(list)
    for seg in all_segments:
        by_domain[seg.domain].append(seg)

    all_classified: list[ClassifiedSegment] = []
    for domain, segs in by_domain.items():
        existing_caps = get_domain_capacities(existing_buckets, domain)
        if existing_caps:
            print(f"[ingest] {domain}: {len(segs)} segs, {len(existing_caps)} existing capacities")
        else:
            print(f"[ingest] {domain}: {len(segs)} segs (new domain)")
        classified = asyncio.run(
            classify_segments(segs, config.PARALLEL, existing_capacities=existing_caps)
        )
        all_classified.extend(classified)

    mark_segment_classified(cp, len(all_classified))
    _save_segments(all_classified)
    print(f"[ingest] classified {len(all_classified)}/{len(all_segments)} segments")

    buckets = bucket_segments(all_classified, existing_buckets)
    save_buckets(buckets)
    cp["bucket_count"] = len(buckets)
    cp["pipeline"]["bucket"]["last_run"] = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()

    save_checkpoint(cp)
    print(f"[ingest] done — {len(buckets)} buckets total")

    ready = [b for b in buckets.values() if b.dirty and len(b.segment_ids) >= config.MIN_BUCKET_SIZE]
    print(f"[ingest] {len(ready)} bucket(s) ready for distillation")


def cmd_distill(args: argparse.Namespace) -> None:
    cp = load_checkpoint()
    buckets = load_buckets()
    segment_map = _load_segment_map()

    if args.bucket:
        targets = {args.bucket: buckets[args.bucket]} if args.bucket in buckets else {}
    elif args.force:
        targets = {bid: b for bid, b in buckets.items() if len(b.segment_ids) >= config.MIN_BUCKET_SIZE}
    else:
        targets = {
            bid: b
            for bid, b in buckets.items()
            if b.dirty and len(b.segment_ids) >= config.MIN_BUCKET_SIZE
        }

    if not targets:
        print("[distill] no buckets ready for distillation")
        return

    print(f"[distill] distilling {len(targets)} bucket(s)")

    for bid, bucket in targets.items():
        try:
            print(f"  [{bucket.domain}] distilling '{bucket.canonical_capacity}' ({len(bucket.segment_ids)} segments)...")
            skill = distill_bucket_sync(bucket, segment_map)
            update_registry_entry(skill)
            bucket.distill_version += 1
            bucket.dirty = False
            bucket.last_distilled_at = skill.meta["distilled_at"]
            mark_bucket_distilled(cp, bid)
            print(f"  ✓ {skill.skill_name}")
        except Exception as e:
            print(f"  ✗ {bid}: {e}")
            cp["pipeline"]["distill"]["failed"] += 1

    save_buckets(buckets)
    save_checkpoint(cp)
    print("[distill] done")


def cmd_query(args: argparse.Namespace) -> None:
    description = " ".join(args.task_description)
    results = query_top_k(description, k=args.k)
    if not results:
        print("No matching skills found.")
        return
    for entry, score in results:
        skill_path = config.HARNESS_ROOT / entry.skill_path
        print(f"\n{'=' * 60}")
        print(f"  {entry.skill_name}  (score={score:.1f})")
        print(f"  capacity: {entry.capacity_id}")
        print(f"  scope: {entry.scope}")
        print(f"  domains: {', '.join(entry.domains)}")
        print(f"  segments: {entry.segment_count}, version: {entry.distill_version}")
        if skill_path.exists():
            content = skill_path.read_text()
            preview = content[:500] + "..." if len(content) > 500 else content
            print(f"  ---\n{preview}")


def cmd_status(args: argparse.Namespace) -> None:
    cp = load_checkpoint()
    buckets = load_buckets()

    print(f"Tracks ingested:    {len(cp['ingested_tracks'])}")
    print(f"Segments classified: {cp['classified_segments']}")
    print(f"Buckets total:      {len(buckets)}")

    ready = [b for b in buckets.values() if b.dirty and len(b.segment_ids) >= config.MIN_BUCKET_SIZE]
    distilled = [b for b in buckets.values() if not b.dirty]
    small = [b for b in buckets.values() if len(b.segment_ids) < config.MIN_BUCKET_SIZE]

    print(f"  ready to distill: {len(ready)}")
    print(f"  already distilled: {len(distilled)}")
    print(f"  too small (< {config.MIN_BUCKET_SIZE}): {len(small)}")
    print(f"Distilled skills:   {len(cp['distilled_buckets'])}")

    # Group by domain
    by_domain: dict[str, list[Bucket]] = defaultdict(list)
    for b in buckets.values():
        by_domain[b.domain].append(b)

    for domain in sorted(by_domain):
        domain_buckets = sorted(by_domain[domain], key=lambda b: -len(b.segment_ids))
        total_segs = sum(len(b.segment_ids) for b in domain_buckets)
        print(f"\n[{domain}] {len(domain_buckets)} buckets, {total_segs} segments")
        for b in domain_buckets:
            flag = "✓" if not b.dirty else ("⏳" if len(b.segment_ids) >= config.MIN_BUCKET_SIZE else "·")
            print(f"  {flag} {b.canonical_capacity:<40} {len(b.segment_ids):>3} segs  v{b.distill_version}")


def cmd_consolidate(args: argparse.Namespace) -> None:
    buckets = load_buckets()

    if args.domain:
        domains = [args.domain]
    else:
        by_domain: dict[str, int] = defaultdict(int)
        for b in buckets.values():
            by_domain[b.domain] += 1
        threshold = args.threshold or 8
        domains = [d for d, c in by_domain.items() if c >= threshold]
        if not domains:
            print(f"[consolidate] no domains with >= {threshold} buckets")
            return

    print(f"[consolidate] checking {len(domains)} domain(s)")
    total_merges = 0

    for domain in sorted(domains):
        domain_count = sum(1 for b in buckets.values() if b.domain == domain)
        print(f"\n[{domain}] {domain_count} buckets")

        try:
            merges = consolidate_domain(domain, buckets)
        except Exception as e:
            print(f"  error: {e}")
            continue

        if not merges:
            print("  no merges needed")
            continue

        for m in merges:
            src_names = [s.split("::", 1)[1] if "::" in s else s for s in m["sources"]]
            tgt_name = m["target"].split("::", 1)[1] if "::" in m["target"] else m["target"]
            print(f"  merge: {', '.join(src_names)} → {tgt_name}")
            print(f"    reason: {m['reason']}")
            total_merges += 1

    if total_merges > 0:
        save_buckets(buckets)
        new_total = len(buckets)
        print(f"\n[consolidate] {total_merges} merge(s) applied, {new_total} buckets total")
    else:
        print("\n[consolidate] no merges needed")


def cmd_full(args: argparse.Namespace) -> None:
    cmd_ingest(args)
    cmd_distill(args)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Distributed multi-trajectory to multi-skill distillation"
    )
    sub = parser.add_subparsers(dest="command")

    p_ingest = sub.add_parser("ingest", help="Atomize, classify, and bucket new trajectories")
    p_ingest.add_argument("--tracks-dir", help="Directory with trajectory JSON files")
    p_ingest.add_argument("--track-file", help="Single trajectory file to process")

    p_distill = sub.add_parser("distill", help="Distill skills from ready buckets")
    p_distill.add_argument("--bucket", help="Distill only this specific bucket")
    p_distill.add_argument("--force", action="store_true", help="Distill all buckets regardless of dirty flag")

    p_query = sub.add_parser("query", help="Query skills by task description")
    p_query.add_argument("task_description", nargs="+", help="Natural language task description")
    p_query.add_argument("-k", type=int, default=5, help="Number of results")

    sub.add_parser("status", help="Show pipeline status")

    p_consolidate = sub.add_parser("consolidate", help="Merge redundant buckets within domains")
    p_consolidate.add_argument("--domain", help="Consolidate only this domain")
    p_consolidate.add_argument("--threshold", type=int, default=8, help="Only consolidate domains with >= N buckets")

    p_full = sub.add_parser("full", help="Run ingest + distill end-to-end")
    p_full.add_argument("--tracks-dir", help="Directory with trajectory JSON files")
    p_full.add_argument("--track-file", help="Single trajectory file to process")
    p_full.add_argument("--bucket", default=None)
    p_full.add_argument("--force", action="store_true")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "ingest": cmd_ingest,
        "distill": cmd_distill,
        "query": cmd_query,
        "status": cmd_status,
        "consolidate": cmd_consolidate,
        "full": cmd_full,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
