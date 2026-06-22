"""Event processing utilities — ported from distill-v11-ir.mjs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class NormalizedEvent:
    type: str
    url: str
    ts: int
    target_tag: str | None = None
    target_id: str | None = None
    target_text: str | None = None
    target_xpath: str | None = None
    value: str | None = None
    key: str | None = None
    x: int | None = None
    y: int | None = None


def redact(text: str) -> str:
    s = str(text) if text is not None else ""
    s = re.sub(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", "<runtime-email>", s, flags=re.I)
    s = re.sub(r"\b(?:\d[ \-]?){13,19}\b", "<runtime-payment-card>", s)
    s = re.sub(
        r"\b\d{3,4}\b(?=\s*(?:cvv|cvc|security|$))",
        "<runtime-cvc>",
        s,
        flags=re.I,
    )
    s = re.sub(r"\b\d{6}\b", "<runtime-verification-code>", s)
    s = re.sub(r"\bcb[a-f0-9]{8,}\b", "<runtime-account-token>", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def host_path(url: str) -> str:
    try:
        u = urlparse(url)
        host = re.sub(r"^www\.", "", u.hostname or "")
        path = u.path or "/"
        if len(path) > 100:
            path = path[:96] + "..."
        return f"{host}{path}"
    except Exception:
        return str(url or "")[:120]


def registered_domain(url: str) -> str:
    """Extract eTLD+1-ish domain from URL (simple heuristic)."""
    try:
        hostname = urlparse(url).hostname or ""
        hostname = re.sub(r"^www\.", "", hostname)
        parts = hostname.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return hostname
    except Exception:
        return ""


def label_of(event: NormalizedEvent) -> str:
    tag = event.target_tag or ""
    text = event.target_text or event.target_id or tag or event.type or ""
    return redact(text)[:180]


def value_of(event: NormalizedEvent) -> str:
    label = label_of(event).lower()
    v = str(event.value) if event.value is not None else ""
    if re.search(r"password|passwd|passcode", label):
        v = "<runtime-password>"
    if re.search(r"email|mail", label):
        v = "<runtime-email>"
    return redact(v)[:220]


_KEEP_TYPES = frozenset(
    ["pageLoad", "navigation", "click", "input", "change", "submit", "keydown", "keyup", "scroll"]
)


def summarize_events(events: list[NormalizedEvent], max_lines: int = 120) -> str:
    keep: list[str] = []
    last_key = ""
    repeat = 0

    def flush_repeat() -> None:
        nonlocal repeat
        if repeat > 1 and keep:
            keep[-1] += f" x{repeat}"
        repeat = 0

    for e in events:
        if e.type not in _KEEP_TYPES:
            continue
        path = host_path(e.url)
        label = label_of(e)
        val = value_of(e)

        if e.type in ("pageLoad", "navigation"):
            text = f"{e.type:<10} {path}"
        elif e.type in ("input", "change"):
            text = f"{e.type:<10} {path} :: {label}"
            if val:
                text += f' = "{val}"'
        elif e.type == "click":
            text = f"{e.type:<10} {path} :: {label}"
        elif e.type in ("keydown", "keyup"):
            text = f"{e.type:<10} {path} :: {label} key={e.key or ''}"
        elif e.type == "submit":
            text = f"{e.type:<10} {path} :: {label}"
        else:
            text = f"{e.type:<10} {path}"

        key = re.sub(r' = ".+?"$', "", text)
        if key == last_key:
            repeat += 1
            continue
        flush_repeat()
        keep.append(text)
        last_key = key
        repeat = 1

    flush_repeat()

    if len(keep) <= max_lines:
        return "\n".join(keep)

    head_count = int(max_lines * 0.35)
    tail_count = max_lines - head_count
    return "\n".join(
        [
            *keep[:head_count],
            f"... omitted {len(keep) - max_lines} middle events ...",
            *keep[-tail_count:],
        ]
    )
