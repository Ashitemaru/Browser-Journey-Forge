"""Pipeline checkpoint for resume support."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import config


def _cp_path() -> Path:
    return config.STATE_DIR / "checkpoint.json"


def _empty() -> dict:
    return {
        "version": 1,
        "updated_at": None,
        "ingested_tracks": [],
        "classified_segments": 0,
        "bucket_count": 0,
        "distilled_buckets": [],
        "pipeline": {
            "atomize": {"completed": 0, "failed": 0},
            "classify": {"completed": 0, "failed": 0, "retry_queue": []},
            "bucket": {"last_run": None},
            "distill": {"completed": 0, "pending": 0, "failed": 0},
        },
    }


def load_checkpoint() -> dict:
    path = _cp_path()
    if path.exists():
        return json.loads(path.read_text())
    return _empty()


def save_checkpoint(cp: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    cp["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = _cp_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(cp, indent=2, ensure_ascii=False))
    tmp.rename(_cp_path())


def mark_track_ingested(cp: dict, track_id: str) -> None:
    if track_id not in cp["ingested_tracks"]:
        cp["ingested_tracks"].append(track_id)


def mark_segment_classified(cp: dict, count: int = 1) -> None:
    cp["classified_segments"] += count
    cp["pipeline"]["classify"]["completed"] += count


def mark_bucket_distilled(cp: dict, bucket_id: str) -> None:
    if bucket_id not in cp["distilled_buckets"]:
        cp["distilled_buckets"].append(bucket_id)
    cp["pipeline"]["distill"]["completed"] = len(cp["distilled_buckets"])
