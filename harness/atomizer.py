"""Trajectory atomizer: split a NormalizedTrack into Segments."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .adapter import NormalizedTrack
from .event_utils import NormalizedEvent, registered_domain, summarize_events

MIN_SEGMENT_EVENTS = 3
MAX_SEGMENT_EVENTS = 80

IFRAME_DOMAINS = frozenset([
    "stripe.com", "js.stripe.com", "m.stripe.network",
    "recaptcha.net", "google.com",
    "betterbugs.io", "recording.betterbugs.io",
    "hcaptcha.com", "challenges.cloudflare.com",
    "gstatic.com",
])

MODIFIER_KEYS = frozenset(["Shift", "Meta", "Alt", "Control", "CapsLock"])


@dataclass
class Segment:
    segment_id: str
    source_track_id: str
    domain: str
    start_idx: int
    end_idx: int
    events: list[NormalizedEvent]
    boundary_reason: str
    entry_url: str
    exit_url: str
    duration_ms: int
    event_summary: str


def _is_iframe_pageload(event: NormalizedEvent) -> bool:
    if event.type != "pageLoad":
        return False
    rd = registered_domain(event.url)
    return rd in IFRAME_DOMAINS


def _is_lone_modifier(event: NormalizedEvent, idx: int, events: list[NormalizedEvent]) -> bool:
    if event.type != "keydown" or event.key not in MODIFIER_KEYS:
        return False
    if idx + 1 < len(events):
        nxt = events[idx + 1]
        if nxt.type == "keydown" and nxt.key not in MODIFIER_KEYS and nxt.ts - event.ts < 500:
            return False
    return True


def _path_prefix(url: str, depth: int = 2) -> str:
    try:
        path = urlparse(url).path or "/"
        parts = [p for p in path.split("/") if p]
        return "/" + "/".join(parts[:depth])
    except Exception:
        return "/"


def _filter_noise(events: list[NormalizedEvent]) -> list[NormalizedEvent]:
    filtered: list[NormalizedEvent] = []
    for i, e in enumerate(events):
        if _is_iframe_pageload(e):
            continue
        if _is_lone_modifier(e, i, events):
            continue
        filtered.append(e)

    deduped: list[NormalizedEvent] = []
    for e in filtered:
        if (
            e.type == "click"
            and deduped
            and deduped[-1].type == "click"
            and deduped[-1].target_xpath == e.target_xpath
            and deduped[-1].url == e.url
            and e.ts - deduped[-1].ts < 2000
        ):
            deduped[-1] = e
            continue
        deduped.append(e)

    return deduped


def _find_boundaries(events: list[NormalizedEvent], track_domain: str) -> list[tuple[int, str]]:
    """Return list of (index, reason) where cuts should happen BEFORE that index."""
    boundaries: list[tuple[int, str]] = []
    prev_ts = 0
    prev_reg_domain = registered_domain(events[0].url) if events else ""
    prev_path_prefix = _path_prefix(events[0].url) if events else "/"

    for i, e in enumerate(events):
        if i == 0:
            prev_ts = e.ts
            continue

        cur_reg_domain = registered_domain(e.url)

        if cur_reg_domain and prev_reg_domain and cur_reg_domain != prev_reg_domain:
            if cur_reg_domain != registered_domain(track_domain):
                pass
            else:
                boundaries.append((i, "domain_change"))
                prev_reg_domain = cur_reg_domain
                prev_path_prefix = _path_prefix(e.url)
                prev_ts = e.ts
                continue

        if e.ts - prev_ts > 15_000:
            boundaries.append((i, "idle_gap"))
            prev_reg_domain = cur_reg_domain
            prev_path_prefix = _path_prefix(e.url)
            prev_ts = e.ts
            continue

        if e.type == "pageLoad" and cur_reg_domain == prev_reg_domain:
            cur_prefix = _path_prefix(e.url)
            if cur_prefix != prev_path_prefix and prev_path_prefix != "/":
                boundaries.append((i, "path_change"))
                prev_path_prefix = cur_prefix
                prev_ts = e.ts
                continue

        if e.type == "submit":
            lookahead = events[i + 1 : i + 5]
            if any(la.type == "pageLoad" for la in lookahead):
                nav_idx = next(
                    j for j in range(i + 1, min(i + 5, len(events)))
                    if events[j].type == "pageLoad"
                )
                boundaries.append((nav_idx + 1, "submit_nav"))
                prev_ts = e.ts
                continue

        prev_reg_domain = cur_reg_domain
        if e.type == "pageLoad":
            prev_path_prefix = _path_prefix(e.url)
        prev_ts = e.ts

    return boundaries


def segment_trajectory(track: NormalizedTrack) -> list[Segment]:
    if not track.events:
        return []

    clean = _filter_noise(track.events)
    if len(clean) < MIN_SEGMENT_EVENTS:
        return [_make_segment(track, clean, 0, len(clean), "end_of_track")]

    boundaries = _find_boundaries(clean, track.domain)
    cut_points = sorted(set(b[0] for b in boundaries))
    boundary_reasons = {b[0]: b[1] for b in boundaries}

    raw_segments: list[tuple[int, int, str]] = []
    prev = 0
    for cp in cut_points:
        if cp > prev:
            raw_segments.append((prev, cp, boundary_reasons.get(cp, "unknown")))
        prev = cp
    if prev < len(clean):
        raw_segments.append((prev, len(clean), "end_of_track"))

    def _dom(s: int, e: int) -> str:
        return _segment_domain(clean[s:e], "")

    # Merge under-sized segments into the previous one — but NEVER across a
    # domain boundary, or a tiny visit to site B would contaminate site A's
    # bucket (and get mislabeled as A).
    merged: list[tuple[int, int, str]] = []
    for start, end, reason in raw_segments:
        seg_len = end - start
        if seg_len < MIN_SEGMENT_EVENTS and merged and _dom(merged[-1][0], merged[-1][1]) == _dom(start, end):
            prev_start, prev_end, prev_reason = merged[-1]
            merged[-1] = (prev_start, end, prev_reason)
        else:
            merged.append((start, end, reason))

    if (
        len(merged) > 1
        and (merged[-1][1] - merged[-1][0]) < MIN_SEGMENT_EVENTS
        and _dom(merged[-1][0], merged[-1][1]) == _dom(merged[-2][0], merged[-2][1])
    ):
        last = merged.pop()
        prev_start, prev_end, prev_reason = merged[-1]
        merged[-1] = (prev_start, last[1], prev_reason)

    final: list[tuple[int, int, str]] = []
    for start, end, reason in merged:
        if end - start > MAX_SEGMENT_EVENTS:
            chunks = _split_oversized(clean, start, end)
            final.extend(chunks)
        else:
            final.append((start, end, reason))

    segments: list[Segment] = []
    for start, end, reason in final:
        seg_events = clean[start:end]
        if not seg_events:
            continue
        segments.append(_make_segment(track, seg_events, start, end, reason))

    return segments


def _split_oversized(
    events: list[NormalizedEvent], start: int, end: int
) -> list[tuple[int, int, str]]:
    chunks: list[tuple[int, int, str]] = []
    cur_start = start
    while cur_start < end:
        cur_end = min(cur_start + MAX_SEGMENT_EVENTS, end)
        if cur_end < end:
            best_cut = cur_end
            for j in range(cur_end - 1, cur_start + MIN_SEGMENT_EVENTS, -1):
                if events[j].type == "pageLoad":
                    best_cut = j
                    break
            cur_end = best_cut
        chunks.append((cur_start, cur_end, "max_size_split"))
        cur_start = cur_end
    return chunks


def _segment_domain(events: list[NormalizedEvent], fallback: str) -> str:
    """The dominant registrable domain across a segment's own events.

    A single recording can span several sites; each segment must be bucketed
    under the site it actually happened on, not the whole trace's first domain.
    """
    counts: dict[str, int] = {}
    for e in events:
        rd = registered_domain(e.url)
        if rd:
            counts[rd] = counts.get(rd, 0) + 1
    return max(counts, key=counts.get) if counts else fallback


def _make_segment(
    track: NormalizedTrack,
    events: list[NormalizedEvent],
    start: int,
    end: int,
    reason: str,
) -> Segment:
    entry_url = events[0].url if events else ""
    exit_url = events[-1].url if events else ""
    duration = (events[-1].ts - events[0].ts) if len(events) > 1 else 0

    return Segment(
        segment_id=f"{track.track_id}::{start}::{end}",
        source_track_id=track.track_id,
        domain=_segment_domain(events, track.domain),
        start_idx=start,
        end_idx=end,
        events=events,
        boundary_reason=reason,
        entry_url=entry_url,
        exit_url=exit_url,
        duration_ms=max(0, duration),
        event_summary=summarize_events(events),
    )
