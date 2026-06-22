"""Per-bucket skill distillation: multi-segment → 1 skill."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .bucketer import Bucket
from .classifier import ClassifiedSegment
from .event_utils import host_path, registered_domain
from .llm import call_llm, parse_json_from_model
from . import config


@dataclass
class DistilledSkill:
    capacity_id: str
    skill_name: str
    scope: str
    preconditions: list[str]
    milestones: list[str]
    terminal_conditions: list[str]
    red_lines: list[str]
    skill_md: str
    trace_guide_md: str
    meta: dict


DISTILL_PROMPT = """\
You are distilling a reusable browser-agent SKILL from multiple trajectory
segments that all demonstrate the same capability: "{capacity_name}".
They come from {domain_count} different website(s): {domain_list}.

Goal: produce a single SKILL.md that any browser agent can follow to perform
this capability on ANY website — not only the observed ones.
Abstract away site-specific selectors and IDs.

Extract from the evidence:
1. The GENERAL PATTERN of this capability
2. Entry preconditions (what must be true to start)
3. Step-by-step procedure (generalized, not site-specific)
4. Milestones (major checkpoints along the way)
5. Exit / terminal conditions (how to know you succeeded)
6. False terminal states (things that look done but aren't)
7. Common failure modes and recovery strategies
8. Anti-drift boundaries (similar-looking actions that are NOT this capability)
9. Red lines (rules that must NEVER be violated)

Output JSON:
{{
  "skill_name": "Human Readable Name",
  "scope": "one sentence describing when to use this skill",
  "preconditions": ["..."],
  "milestones": ["..."],
  "terminal_conditions": ["..."],
  "false_terminal_states": ["..."],
  "recovery_policies": ["..."],
  "anti_drift_boundaries": ["..."],
  "red_lines": ["..."],
  "skill_md": "# Skill: ...\\n\\n(full markdown content)",
  "trace_guide_md": "# TRACE GUIDE: ...\\n\\n(state-card navigation guide)"
}}

## Evidence Segments

{segment_blocks}
"""

INCREMENTAL_ADDENDUM = """
## Current SKILL version (v{version}, distilled from {old_count} segments)

{existing_skill_md}

Update the above SKILL based on the NEW segment evidence:
- Keep rules that are still correct
- Remove rules contradicted by new evidence
- Add newly discovered milestones, recovery strategies, red lines, etc.
"""


def _build_segment_block(cs: ClassifiedSegment, idx: int) -> str:
    seg = cs.segment
    lines = [
        f"### Segment {idx + 1}: from {seg.source_track_id} on {seg.domain}",
        f"Entry: {host_path(seg.entry_url)} → Exit: {host_path(seg.exit_url)} | Outcome: {cs.label.outcome}",
        f"Entry conditions: {', '.join(cs.label.entry_conditions)}",
        f"Exit conditions: {', '.join(cs.label.exit_conditions)}",
        f"Events ({len(seg.events)} total):",
        seg.event_summary,
        "---",
    ]
    return "\n".join(lines)


def distill_bucket_sync(
    bucket: Bucket,
    segment_map: dict[str, ClassifiedSegment],
) -> DistilledSkill:
    segments = [segment_map[sid] for sid in bucket.segment_ids if sid in segment_map]
    if not segments:
        raise ValueError(f"No segments found for bucket {bucket.bucket_id}")

    domains = list({cs.segment.domain for cs in segments})
    segment_blocks = "\n\n".join(
        _build_segment_block(cs, i) for i, cs in enumerate(segments)
    )

    prompt = DISTILL_PROMPT.format(
        capacity_name=bucket.canonical_capacity,
        domain_count=len(domains),
        domain_list=", ".join(domains),
        segment_blocks=segment_blocks,
    )

    skill_dir = _skill_dir_for(bucket.bucket_id)
    existing_skill_path = skill_dir / "SKILL.md"
    if existing_skill_path.exists() and bucket.distill_version > 0:
        existing_md = existing_skill_path.read_text()
        prompt += INCREMENTAL_ADDENDUM.format(
            version=bucket.distill_version,
            old_count=len(bucket.segment_ids) - len(segments),
            existing_skill_md=existing_md[:8000],
        )

    text, usage_meta = call_llm(prompt, model=config.DISTILL_MODEL)
    data = parse_json_from_model(text)

    skill = DistilledSkill(
        capacity_id=bucket.bucket_id,
        skill_name=data.get("skill_name", bucket.canonical_capacity),
        scope=data.get("scope", ""),
        preconditions=data.get("preconditions", []),
        milestones=data.get("milestones", []),
        terminal_conditions=data.get("terminal_conditions", []),
        red_lines=data.get("red_lines", []),
        skill_md=data.get("skill_md", ""),
        trace_guide_md=data.get("trace_guide_md", ""),
        meta={
            "model": usage_meta.get("model", config.DISTILL_MODEL),
            "usage": usage_meta.get("usage", {}),
            "segment_count": len(segments),
            "domains": domains,
            "distill_version": bucket.distill_version + 1,
            "distilled_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    _write_skill(skill, segments)
    return skill


def _skill_dir_for(capacity_id: str) -> Path:
    """Convert bucket_id 'domain::capacity' to path 'skills/domain/capacity'."""
    if "::" in capacity_id:
        domain, cap = capacity_id.split("::", 1)
        return config.STATE_DIR / "skills" / domain / cap
    return config.STATE_DIR / "skills" / capacity_id


def _write_skill(skill: DistilledSkill, segments: list[ClassifiedSegment]) -> None:
    skill_dir = _skill_dir_for(skill.capacity_id)
    skill_dir.mkdir(parents=True, exist_ok=True)

    (skill_dir / "SKILL.md").write_text(skill.skill_md)
    (skill_dir / "TRACE_GUIDE.md").write_text(skill.trace_guide_md)
    (skill_dir / "meta.json").write_text(
        json.dumps(skill.meta, indent=2, ensure_ascii=False)
    )

    evidence_path = skill_dir / "evidence.jsonl"
    with evidence_path.open("w") as f:
        for cs in segments:
            row = {
                "segment_id": cs.segment.segment_id,
                "source_track": cs.segment.source_track_id,
                "domain": cs.segment.domain,
                "capacity": cs.label.capacity,
                "outcome": cs.label.outcome,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"[distiller] wrote {skill_dir}")
