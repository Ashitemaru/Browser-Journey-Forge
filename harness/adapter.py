"""Input adapter: normalize human-tracks JSON and recorder exports into a common format."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .event_utils import NormalizedEvent, registered_domain


@dataclass
class NormalizedTrack:
    track_id: str
    domain: str
    task_instruction: str | None
    events: list[NormalizedEvent]
    navigation_chain: list[str]
    outcome: dict | None = None
    source_format: str = "human-tracks"


def _norm_human_event(e: dict) -> NormalizedEvent | None:
    etype = e.get("type")
    if not etype:
        return None
    target = e.get("target") or {}
    return NormalizedEvent(
        type=etype,
        url=e.get("url", ""),
        ts=int(e.get("ts", 0)),
        target_tag=target.get("tagName"),
        target_id=target.get("id") or None,
        target_text=(target.get("textContent") or "")[:200] or None,
        target_xpath=target.get("xpath") or None,
        value=e.get("value"),
        key=e.get("key"),
        x=e.get("x"),
        y=e.get("y"),
    )


def load_human_track(path: Path) -> NormalizedTrack:
    data = json.loads(path.read_text())
    events: list[NormalizedEvent] = []
    for raw in data.get("events", []):
        ev = _norm_human_event(raw)
        if ev is not None:
            events.append(ev)

    return NormalizedTrack(
        track_id=data.get("case_id", path.stem),
        domain=data.get("domain", ""),
        task_instruction=data.get("task_instruction"),
        events=events,
        navigation_chain=data.get("navigation_chain", []),
        outcome=data.get("outcome"),
        source_format="human-tracks",
    )


def _norm_recorder_event(e: dict, base_ts: int) -> NormalizedEvent | None:
    kind = e.get("kind")
    if kind == "action":
        target = e.get("target") or {}
        coords = e.get("coords") or {}
        return NormalizedEvent(
            type=e.get("type", "click"),
            url=e.get("url", ""),
            ts=int(e.get("timestamp", 0)) - base_ts,
            target_tag=target.get("tag"),
            target_id=target.get("id") or None,
            target_text=(target.get("text") or target.get("name") or "")[:200] or None,
            target_xpath=target.get("xpath") or None,
            value=e.get("value"),
            key=e.get("key"),
            x=coords.get("x"),
            y=coords.get("y"),
        )
    elif kind == "navigation":
        nav_type = e.get("navType", "load")
        return NormalizedEvent(
            type="pageLoad" if nav_type == "load" else "navigation",
            url=e.get("toUrl") or e.get("url", ""),
            ts=int(e.get("timestamp", 0)) - base_ts,
        )
    return None


def load_recorder_export(path: Path) -> list[NormalizedTrack]:
    data = json.loads(path.read_text())

    if isinstance(data, list):
        trajectories = data
    elif "steps" in data:
        trajectories = [data]
    elif "trajectories" in data:
        trajectories = data["trajectories"]
    else:
        return []

    tracks: list[NormalizedTrack] = []
    for traj in trajectories:
        traj_id = traj.get("id", path.stem)
        steps = traj.get("steps", [])
        raw_events = traj.get("events", [])

        events: list[NormalizedEvent] = []
        base_ts = 0

        if raw_events:
            base_ts = min((e.get("timestamp", 0) for e in raw_events), default=0)
            for raw in raw_events:
                ev = _norm_recorder_event(raw, base_ts)
                if ev is not None:
                    events.append(ev)
        elif steps:
            all_ts = [s.get("action", {}).get("timestamp", 0) for s in steps if s.get("action")]
            base_ts = min(all_ts) if all_ts else 0
            for step in steps:
                action = step.get("action")
                if action:
                    ev = _norm_recorder_event(action, base_ts)
                    if ev is not None:
                        events.append(ev)
                obs_before = step.get("observationBefore") or {}
                if obs_before.get("url") and (not events or events[-1].url != obs_before["url"]):
                    events.append(NormalizedEvent(
                        type="pageLoad",
                        url=obs_before["url"],
                        ts=int(action.get("timestamp", 0)) - base_ts if action else 0,
                    ))

        events.sort(key=lambda e: e.ts)

        nav_chain: list[str] = []
        for ev in events:
            if ev.type == "pageLoad":
                hp = registered_domain(ev.url)
                if hp and (not nav_chain or nav_chain[-1] != hp):
                    nav_chain.append(hp)

        tracks.append(NormalizedTrack(
            track_id=traj_id,
            domain=traj.get("domain", registered_domain(events[0].url) if events else ""),
            task_instruction=traj.get("intentHint"),
            events=events,
            navigation_chain=nav_chain,
            source_format="recorder",
        ))

    return tracks


def _norm_journey_forge_event(e: dict, base_ts: int) -> NormalizedEvent | None:
    """Normalize a journey-forge (journey_trace_v1) event."""
    kind = e.get("kind")

    if kind == "action":
        action_type = e.get("action_type", "click")
        if action_type in ("focus", "blur", "contextmenu", "copy", "cut", "selection"):
            return None
        if action_type == "dblclick":
            action_type = "click"
        if action_type == "wheel":
            action_type = "scroll"
        if action_type == "file_select":
            action_type = "change"

        target = e.get("target") or {}
        coords = e.get("coords") or {}

        raw_value = e.get("value")
        value = None
        if isinstance(raw_value, dict):
            value = raw_value.get("value")
        elif isinstance(raw_value, str):
            value = raw_value

        return NormalizedEvent(
            type=action_type,
            url=e.get("url", ""),
            ts=int(e.get("timestamp", 0)) - base_ts,
            target_tag=target.get("tag"),
            target_id=target.get("id") or None,
            target_text=(target.get("text") or target.get("name") or "")[:200] or None,
            target_xpath=target.get("xpath") or None,
            value=value,
            key=e.get("key"),
            x=coords.get("x"),
            y=coords.get("y"),
        )

    elif kind == "navigation":
        nav_type = e.get("nav_type", "load")
        etype = "pageLoad" if nav_type == "load" else "navigation"
        url = e.get("to_url") or e.get("url", "")
        return NormalizedEvent(
            type=etype,
            url=url,
            ts=int(e.get("timestamp", 0)) - base_ts,
        )

    return None


def load_journey_forge_trace(path: Path) -> NormalizedTrack:
    """Load a journey-forge (journey_trace_v1) exported trace."""
    data = json.loads(path.read_text())

    raw_events = data.get("events", [])
    base_ts = min((e.get("timestamp", 0) for e in raw_events), default=0) if raw_events else 0

    events: list[NormalizedEvent] = []
    for raw in raw_events:
        ev = _norm_journey_forge_event(raw, base_ts)
        if ev is not None:
            events.append(ev)

    events.sort(key=lambda e: e.ts)

    nav_chain: list[str] = []
    for ev in events:
        if ev.type == "pageLoad":
            hp = registered_domain(ev.url)
            if hp and (not nav_chain or nav_chain[-1] != hp):
                nav_chain.append(hp)

    summary = data.get("summary", {})
    domains = summary.get("domains", [])
    domain = domains[0] if domains else (registered_domain(events[0].url) if events else "")

    return NormalizedTrack(
        track_id=data.get("trace_id", path.stem),
        domain=domain,
        task_instruction=data.get("label") or data.get("description"),
        events=events,
        navigation_chain=nav_chain,
        source_format="journey-forge",
    )


def _detect_format(path: Path) -> str:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        if data.get("schema_version") == "journey_trace_v1":
            return "journey-forge"
        if "schema_version" in data and "case_id" in data:
            return "human-tracks"
        if "steps" in data or "trajectories" in data:
            return "recorder"
        if "kind" in data:
            return "recorder"
    if isinstance(data, list) and data and "steps" in data[0]:
        return "recorder"
    return "human-tracks"


def load_track_file(path: Path) -> list[NormalizedTrack]:
    """Load one trajectory file, auto-detecting its format."""
    fmt = _detect_format(path)
    if fmt == "journey-forge":
        return [load_journey_forge_trace(path)]
    if fmt == "recorder":
        return load_recorder_export(path)
    return [load_human_track(path)]


def load_tracks(input_dir: Path) -> list[NormalizedTrack]:
    tracks: list[NormalizedTrack] = []
    for p in sorted(input_dir.glob("*.json")):
        if p.name.startswith(".") or p.name.startswith("_"):
            continue
        try:
            fmt = _detect_format(p)
            if fmt == "journey-forge":
                tracks.append(load_journey_forge_trace(p))
            elif fmt == "recorder":
                tracks.extend(load_recorder_export(p))
            else:
                tracks.append(load_human_track(p))
        except Exception as e:
            print(f"[adapter] skip {p.name}: {e}")
    return tracks
