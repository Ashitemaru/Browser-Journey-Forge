"""Minimal stdio MCP server that exposes distilled skills to Claude Desktop.

Why this exists: Claude Desktop has no external API to inject content into a
running chat, and its "Skills" upload is a manual zip. But Desktop *does* speak
MCP (that's how it gets Playwright). So we expose the distilled skills as an MCP
tool. The tool reads the registry + SKILL.md FRESH on every call, so after the
user records → auto-distill writes a new skill, the very next `get_skill` call
returns it — no Claude Desktop restart needed. The only one-time cost is adding
this server to claude_desktop_config.json (folded into Playwright setup).

Protocol: JSON-RPC 2.0 over stdio, newline-delimited (MCP stdio transport).
Pure stdlib so it runs inside the PyInstaller-frozen sidecar.

Tools:
  list_skills()        -> every distilled skill (domain::capability + scope)
  get_skill(query)     -> full SKILL.md best-matching a site domain / url / capability
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "journey-forge-skills", "version": "1.0.0"}


def _state_dir() -> Path:
    # Mirror harness.config: state lives under <JFL_DATA_DIR>/harness. The MCP
    # entry passes JFL_DATA_DIR so we read the same registry the pipeline writes.
    data_dir = os.environ.get("JFL_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "harness"
    return Path(__file__).resolve().parents[1] / "data" / "harness"


def _load_registry() -> list[dict]:
    reg = _state_dir() / "registry.json"
    if not reg.is_file():
        return []
    try:
        return json.loads(reg.read_text()).get("skills", [])
    except (json.JSONDecodeError, OSError):
        return []


def _read_skill_md(entry: dict) -> str:
    p = _state_dir() / entry.get("skill_path", "")
    try:
        return p.read_text()
    except OSError:
        return ""


# ── two-layer retrieval (harness-backed) ──────────────────────────────────────
# Layer 1 (semantic): harness.registry.query_top_k understands the user's intent
# (any language, domain-weighted) and returns the relevant ATOMIC skills.
# Layer 2 (synthesis): harness.registry.synthesize_playbook sequences them into
# one ordered playbook for the goal. Both need an LLM key, which lives in
# <JFL_DATA_DIR>/config.json — we inject it into harness.config the same way the
# server's _apply_harness_config does. If harness or the key is unavailable we
# degrade to the local token-score path below (no crash, still returns a skill).

_HARNESS_READY = False


def _ensure_harness_config() -> bool:
    """Make harness importable + inject the LLM key/base from config.json.

    Returns True if harness is usable with an LLM key (layers 1+2 available),
    False otherwise (caller falls back to local token scoring)."""
    global _HARNESS_READY
    try:
        from harness import config as hconfig  # noqa: F401
    except Exception:  # noqa: BLE001 — frozen/dev path may not expose harness
        return False

    # Read product config.json (sibling of harness/ state dir).
    data_dir = os.environ.get("JFL_DATA_DIR")
    cfg = {}
    cfg_path = (Path(data_dir) if data_dir else _state_dir().parent) / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        cfg = {}

    key = cfg.get("llm_key") or os.environ.get("SF_LLM_KEY", "")
    if not key:
        return False
    from harness import config as hconfig
    hconfig.LLM_KEY = key
    if cfg.get("llm_base"):
        hconfig.LLM_BASE = str(cfg["llm_base"]).rstrip("/")
    hconfig.LLM_INSECURE = True  # gateway uses a self-signed chain
    if cfg.get("distill_model"):
        hconfig.DISTILL_MODEL = cfg["distill_model"]
    classify = cfg.get("classify_model") or cfg.get("distill_model")
    if classify:
        hconfig.CLASSIFY_MODEL = classify
        hconfig.BUCKET_MODEL = classify
    _HARNESS_READY = True
    return True


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _query_domain(query: str) -> str:
    """Pull a registrable-ish host out of a url or bare domain query."""
    q = _norm(query)
    if "://" in q:
        q = urlparse(q).netloc or q
    q = q.split("/")[0]
    if q.startswith("www."):
        q = q[4:]
    return q


def _score(entry: dict, query: str) -> int:
    """How well a registry entry matches a free-text query (higher = better)."""
    q = _norm(query)
    if not q:
        return 0
    qd = _query_domain(query)
    domains = [_norm(d) for d in entry.get("domains", [])]
    cap = _norm(entry.get("capacity_id", ""))
    scope = _norm(entry.get("scope", ""))
    name = _norm(entry.get("skill_name", ""))
    score = 0
    for d in domains:
        if not d:
            continue
        if qd and (qd == d or qd in d or d in qd):
            score += 100
        if q in d or d in q:
            score += 40
    for hay in (cap, scope, name):
        if q and q in hay:
            score += 20
    # token overlap against capability/scope (e.g. "flights" matches "flight search")
    qtokens = {t for t in re.split(r"[^a-z0-9]+", q) if len(t) > 2}
    haytokens = set(re.split(r"[^a-z0-9]+", f"{cap} {scope} {name} {' '.join(domains)}"))
    score += 8 * len(qtokens & haytokens)
    return score


def _skill_summary(entry: dict) -> dict:
    return {
        "capability": entry.get("capacity_id", ""),
        "domains": entry.get("domains", []),
        "scope": entry.get("scope", ""),
        "examples": entry.get("example_count", entry.get("segment_count")),
    }


# ── tools ────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "get_skill",
        "description": (
            "ALWAYS call this FIRST, before doing any browser/web-automation task. "
            "Pass the user's FULL ORIGINAL GOAL verbatim — a whole sentence is best "
            "(e.g. 'find me the cheapest flight from Shanghai to Tokyo in July'). Do "
            "NOT reduce it to keywords. This loads a distilled, site-specific "
            "operating procedure: for a multi-step goal it returns an ORDERED "
            "playbook assembled from several site skills; for a single operation it "
            "returns one SKILL.md. Follow what it returns step-by-step. If nothing "
            "matches, proceed normally."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user's full task goal (a whole sentence), or a target site domain / URL.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_skills",
        "description": "List every distilled skill currently available (domain + capability + scope).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _tool_list_skills() -> str:
    skills = _load_registry()
    if not skills:
        return "No distilled skills available yet. Record a task and let it distill first."
    return json.dumps([_skill_summary(s) for s in skills], ensure_ascii=False, indent=2)


def _tool_get_skill(args: dict) -> str:
    query = args.get("query", "")
    skills = _load_registry()
    if not skills:
        return "No distilled skills available yet."

    # Layer 1+2: semantic retrieval → playbook synthesis (needs harness + LLM key).
    if _ensure_harness_config():
        try:
            return _get_skill_via_harness(query)
        except Exception:  # noqa: BLE001 — any failure degrades to local scoring
            pass

    # Fallback: local token scoring over the raw registry (offline / no key).
    return _get_skill_local(query, skills)


def _get_skill_via_harness(query: str) -> str:
    from harness import registry

    ranked = registry.query_top_k(query, k=8)
    if not ranked:
        skills = _load_registry()
        avail = ", ".join(sorted({d for s in skills for d in s.get("domains", [])})) or "(none)"
        return f"No skill matched '{query}'. Available site skills: {avail}."

    # A single strong match is one atomic operation — return its SKILL.md directly.
    top_dominates = ranked[0][1] >= 0.85 and (len(ranked) < 2 or ranked[1][1] < 0.5)
    if len(ranked) == 1 or top_dominates:
        entry, _ = ranked[0]
        md = (_state_dir() / entry.skill_path).read_text() if entry.skill_path else ""
        if md:
            return (
                f"# Loaded skill: {entry.capacity_id}\n"
                f"# domains: {', '.join(entry.domains)}\n"
                f"# Follow this procedure for the task.\n\n{md}"
            )

    # Multiple relevant atomic skills → synthesize an ordered playbook for the goal.
    domains = sorted({d for e, _ in ranked for d in e.domains})
    try:
        playbook = registry.synthesize_playbook(query, ranked)
    except Exception:  # noqa: BLE001 — synthesis failed; concatenate raw skills
        return _concat_skill_mds(query, ranked)
    if not playbook.strip():
        return _concat_skill_mds(query, ranked)
    used = ", ".join(e.capacity_id for e, _ in ranked[:6])
    return (
        f"# Playbook for: {query}\n"
        f"# domains: {', '.join(domains)}\n"
        f"# Assembled from site skills: {used}\n"
        f"# Follow this ordered procedure top-to-bottom.\n\n{playbook}"
    )


def _concat_skill_mds(query: str, ranked: list) -> str:
    """Degraded layer-2: stitch the top atomic SKILL.md files together in order."""
    parts = [f"# Relevant skills for: {query}\n# Follow these in order.\n"]
    for entry, _ in ranked[:6]:
        md = (_state_dir() / entry.skill_path).read_text() if entry.skill_path else ""
        if md:
            parts.append(f"\n---\n## {entry.capacity_id} ({', '.join(entry.domains)})\n\n{md}")
    return "\n".join(parts)


def _get_skill_local(query: str, skills: list[dict]) -> str:
    ranked = sorted(skills, key=lambda e: _score(e, query), reverse=True)
    best = ranked[0]
    if _score(best, query) <= 0:
        avail = ", ".join(sorted({d for s in skills for d in s.get("domains", [])})) or "(none)"
        return f"No skill matched '{query}'. Available site skills: {avail}."
    md = _read_skill_md(best)
    if not md:
        return f"Matched skill '{best.get('capacity_id')}' but its SKILL.md is missing on disk."
    header = (
        f"# Loaded skill: {best.get('capacity_id')}\n"
        f"# domains: {', '.join(best.get('domains', []))}\n"
        f"# Follow this procedure for the task.\n\n"
    )
    return header + md


def _dispatch_tool(name: str, args: dict) -> str:
    if name == "list_skills":
        return _tool_list_skills()
    if name == "get_skill":
        return _tool_get_skill(args or {})
    raise ValueError(f"unknown tool: {name}")


# ── JSON-RPC plumbing ─────────────────────────────────────────────────────────
def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _handle(req: dict) -> None:
    method = req.get("method")
    req_id = req.get("id")
    # notifications (no id) get no response
    if method == "initialize":
        client_proto = (req.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        _result(req_id, {
            "protocolVersion": client_proto,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    elif method in ("notifications/initialized", "initialized"):
        return
    elif method == "ping":
        _result(req_id, {})
    elif method == "tools/list":
        _result(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = req.get("params") or {}
        try:
            text = _dispatch_tool(params.get("name", ""), params.get("arguments") or {})
            _result(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:  # noqa: BLE001 — surface as a tool error, not a crash
            _result(req_id, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
    elif req_id is not None:
        _error(req_id, -32601, f"method not found: {method}")


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(req)
        except Exception:  # never die on a single bad message
            pass


if __name__ == "__main__":
    serve()
