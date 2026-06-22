"""Skill registry / index for top-k retrieval."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .distiller import DistilledSkill


@dataclass
class RegistryEntry:
    capacity_id: str
    skill_name: str
    scope: str
    domains: list[str]
    preconditions: list[str]
    terminal_conditions: list[str]
    keywords: list[str]
    segment_count: int
    distill_version: int
    skill_path: str
    trace_guide_path: str


def _registry_path() -> Path:
    return config.STATE_DIR / "registry.json"


def load_registry() -> list[RegistryEntry]:
    path = _registry_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    entries: list[RegistryEntry] = []
    for s in data.get("skills", []):
        entries.append(RegistryEntry(
            capacity_id=s["capacity_id"],
            skill_name=s["skill_name"],
            scope=s.get("scope", ""),
            domains=s.get("domains", []),
            preconditions=s.get("preconditions", []),
            terminal_conditions=s.get("terminal_conditions", []),
            keywords=s.get("keywords", []),
            segment_count=s.get("segment_count", 0),
            distill_version=s.get("distill_version", 0),
            skill_path=s.get("skill_path", ""),
            trace_guide_path=s.get("trace_guide_path", ""),
        ))
    return entries


def save_registry(entries: list[RegistryEntry]) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "skills": [],
    }
    for e in entries:
        out["skills"].append({
            "capacity_id": e.capacity_id,
            "skill_name": e.skill_name,
            "scope": e.scope,
            "domains": e.domains,
            "preconditions": e.preconditions,
            "terminal_conditions": e.terminal_conditions,
            "keywords": e.keywords,
            "segment_count": e.segment_count,
            "distill_version": e.distill_version,
            "skill_path": e.skill_path,
            "trace_guide_path": e.trace_guide_path,
        })
    tmp = _registry_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    tmp.rename(_registry_path())


def _extract_keywords(skill: DistilledSkill) -> list[str]:
    text = " ".join([
        skill.skill_name,
        skill.scope,
        " ".join(skill.preconditions),
        " ".join(skill.terminal_conditions),
        skill.capacity_id.replace("-", " "),
    ]).lower()
    stop = {"the", "a", "an", "is", "are", "to", "in", "on", "of", "and", "or", "for", "with", "that", "this"}
    words = re.findall(r"[a-z]{2,}", text)
    seen: set[str] = set()
    kw: list[str] = []
    for w in words:
        if w not in stop and w not in seen:
            seen.add(w)
            kw.append(w)
    return kw[:30]


def _skill_path(capacity_id: str, filename: str) -> str:
    # Relative to STATE_DIR (== config.HARNESS_ROOT).
    if "::" in capacity_id:
        domain, cap = capacity_id.split("::", 1)
        return f"skills/{domain}/{cap}/{filename}"
    return f"skills/{capacity_id}/{filename}"


def update_registry_entry(skill: DistilledSkill) -> None:
    entries = load_registry()
    new_entry = RegistryEntry(
        capacity_id=skill.capacity_id,
        skill_name=skill.skill_name,
        scope=skill.scope,
        domains=skill.meta.get("domains", []),
        preconditions=skill.preconditions,
        terminal_conditions=skill.terminal_conditions,
        keywords=_extract_keywords(skill),
        segment_count=skill.meta.get("segment_count", 0),
        distill_version=skill.meta.get("distill_version", 0),
        skill_path=_skill_path(skill.capacity_id, "SKILL.md"),
        trace_guide_path=_skill_path(skill.capacity_id, "TRACE_GUIDE.md"),
    )
    entries = [e for e in entries if e.capacity_id != skill.capacity_id]
    entries.append(new_entry)
    save_registry(entries)


QUERY_PROMPT = """\
You are a skill retrieval engine. Given a user's task description (in ANY language),
find the most relevant skills from the registry below.

## User Task
{query}

## Skill Registry ({count} skills)
{catalog}

Return JSON only — an array of the top {k} most relevant skills, ranked by relevance:
{{
  "matches": [
    {{"capacity_id": "domain::skill-name", "relevance": 0.0-1.0, "reason": "why this matches"}}
  ]
}}

Rules:
- Match by SEMANTIC meaning, not keywords. Understand the user's intent regardless of language.
- If the user mentions a specific website/domain, strongly prefer skills from that domain.
- If no skills are relevant at all, return {{"matches": []}}.
- relevance 1.0 = perfect match, 0.5 = partially related, <0.3 = weak match.
- Only include skills with relevance >= 0.3.
"""


def query_top_k(task_description: str, k: int = 5) -> list[tuple[RegistryEntry, float]]:
    entries = load_registry()
    if not entries:
        return []

    catalog_lines = []
    for e in entries:
        catalog_lines.append(
            f"- {e.capacity_id} | {e.skill_name} | {e.scope} | domains: {','.join(e.domains)}"
        )
    catalog = "\n".join(catalog_lines)

    from .llm import call_llm_fast, parse_json_from_model

    prompt = QUERY_PROMPT.format(
        query=task_description,
        count=len(entries),
        catalog=catalog,
        k=k,
    )
    text, _ = call_llm_fast(prompt, max_tokens=4096)
    data = parse_json_from_model(text)

    entry_map = {e.capacity_id: e for e in entries}
    results: list[tuple[RegistryEntry, float]] = []
    for m in data.get("matches", []):
        cid = m.get("capacity_id", "")
        rel = float(m.get("relevance", 0))
        if cid in entry_map and rel >= 0.3:
            results.append((entry_map[cid], rel))

    results.sort(key=lambda x: -x[1])
    return results[:k]
