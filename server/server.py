#!/usr/bin/env python3
"""Journey-Forge Local — minimal local ingestion + control server.

A single-user, ClawBench-agnostic server for the local product:

  POST /v1/traces/init                          init resumable upload
  PUT  /v1/traces/{upload_id}/chunks/{index}    upload a gzip-NDJSON event chunk
  POST /v1/traces/{upload_id}/finalize          assemble + (optionally) auto-distill
  GET  /v1/traces/{upload_id}/status            poll status / distill result

  GET  /api/traj                                list uploaded trajectories
  GET  /api/traj/{upload_id}                    one trajectory detail
  GET  /api/skills                              list distilled / installed skills
  POST /api/skills/{name}/redistill             re-run distillation for a track
  DELETE /api/skills/{name}                     remove an installed skill
  GET  /api/config  /  PUT /api/config          read / update product config
  GET  /api/ext                                 extension build dir + load steps
  POST /api/distill/{upload_id}                 manually (re)trigger distill+install
  GET  /                                        control-panel SPA (app/ build)

There is NO identity-bundle service, NO task corpus / queue, NO judge, and NO
eval_schema / interception here — those are ClawBench scoring concepts and have
no place in the product. The upload protocol (init/chunks/finalize, resumable,
idempotent) is the only thing kept from the research ingestion server.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Paths ──────────────────────────────────────────────────────────────────
REPO = Path(__file__).resolve().parents[1]            # journey-forge-local/
# Make the `harness` package importable in-process (server.py lives in server/).
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
DATA_DIR = Path(os.environ.get("JFL_DATA_DIR", str(REPO / "data")))
# Make sure the in-process harness (harness.config) resolves the same data dir.
os.environ.setdefault("JFL_DATA_DIR", str(DATA_DIR))
TRACES_DIR = DATA_DIR / "traces"
HARNESS_STATE = DATA_DIR / "harness"                   # pipeline state: buckets/skills/registry
API_KEYS_FILE = DATA_DIR / "api-keys.json"
CONFIG_FILE = DATA_DIR / "config.json"

APP_BUILD = REPO / "app" / "dist"                      # control-panel SPA build
EXT_BUILD = REPO / "extension" / "dist" / "chrome-mv3"  # wxt build output

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.environ.get("JFL_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("journey_forge_local")

MAX_EVENT_CHUNK_BYTES = int(os.environ.get("JFL_MAX_EVENT_CHUNK_BYTES", 16 * 1024 * 1024))
MAX_MEDIA_CHUNK_BYTES = int(os.environ.get("JFL_MAX_MEDIA_CHUNK_BYTES", 64 * 1024 * 1024))
_VALID_CHUNK_KINDS = {"events", "media"}
_UPLOAD_ID_RE = re.compile(r"upl_[0-9a-f]{12}$")
_NAME_RE = re.compile(r"[A-Za-z0-9._-]+$")

app = FastAPI(title="Journey-Forge Local")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ── Config (single source of truth, editable from the panel) ─────────────────
def _default_config() -> dict:
    return {
        "llm_key": os.environ.get("SF_LLM_KEY", ""),
        "llm_base": os.environ.get("SF_LLM_BASE", "https://api.anthropic.com"),
        "distill_model": os.environ.get("SF_DISTILL_MODEL", "claude-opus-4-8"),
        # global ~/.claude/skills  OR  an absolute project path's .claude/skills
        # (empty env value falls back to the global default)
        "skills_root": os.environ.get("JFL_SKILLS_ROOT") or str(Path.home() / ".claude" / "skills"),
        "auto_distill": os.environ.get("JFL_AUTODISTILL", "1") not in ("0", "false", ""),
    }


def _load_config() -> dict:
    cfg = _default_config()
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except json.JSONDecodeError:
            logger.warning("config.json corrupt — using defaults")
    return cfg


def _save_config(cfg: dict) -> None:
    _ensure_dirs()
    _atomic_write(CONFIG_FILE, json.dumps(cfg, indent=2, ensure_ascii=False))


# ── Generic helpers (ported from the research server, trimmed) ───────────────
def _ensure_dirs() -> None:
    for d in (DATA_DIR, TRACES_DIR, HARNESS_STATE):
        d.mkdir(parents=True, exist_ok=True)


def _load_api_keys() -> set[str]:
    if API_KEYS_FILE.exists():
        return set(json.loads(API_KEYS_FILE.read_text()))
    _ensure_dirs()
    # Seed the stable default key the product extension ships with, so a freshly
    # loaded extension connects with zero config (localhost-only, dogfood). Set
    # JFL_DEFAULT_KEY to override, or edit api-keys.json after first run.
    key = os.environ.get("JFL_DEFAULT_KEY", "jfl-local-dev-key")
    API_KEYS_FILE.write_text(json.dumps([key], indent=2))
    logger.warning("Seeded default API key → %s", API_KEYS_FILE)
    return {key}


def _check_auth(authorization: str | None) -> None:
    if not authorization:
        raise HTTPException(401, "Missing Authorization header")
    token = authorization.replace("Bearer ", "").strip()
    if token not in _load_api_keys():
        raise HTTPException(401, "Invalid API key")


def _trace_dir(upload_id: str) -> Path:
    return TRACES_DIR / upload_id


def _validate_upload_id(upload_id: str) -> None:
    if not _UPLOAD_ID_RE.fullmatch(upload_id or ""):
        raise HTTPException(400, "Invalid upload_id")


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    if not name or not _NAME_RE.fullmatch(name):
        raise HTTPException(400, f"Invalid name: {name!r}")
    return name


def _upload_id_for(trace_id: str) -> str:
    return "upl_" + hashlib.sha256(trace_id.encode()).hexdigest()[:12]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, what: str):
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"Corrupt {what}: {e}")


@contextmanager
def _file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


@contextmanager
def update_meta(upload_id: str, *, create: bool = False):
    _validate_upload_id(upload_id)
    td = _trace_dir(upload_id)
    td.mkdir(parents=True, exist_ok=True)
    mf = td / "meta.json"
    with _file_lock(td / "meta.lock"):
        if mf.exists():
            meta = _read_json(mf, "trace metadata")
        elif create:
            meta = {}
        else:
            raise HTTPException(404, f"Unknown upload_id: {upload_id}")
        yield meta
        _atomic_write(mf, json.dumps(meta, indent=2, ensure_ascii=False))


def _load_meta(upload_id: str) -> dict:
    _validate_upload_id(upload_id)
    mf = _trace_dir(upload_id) / "meta.json"
    if not mf.exists():
        raise HTTPException(404, f"Unknown upload_id: {upload_id}")
    return _read_json(mf, "trace metadata")


def _registered_domain(url: str) -> str:
    """Best-effort registrable domain (no public-suffix list; good enough local)."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return ""
    host = host.lower().lstrip(".")
    if not host or all(c.isdigit() or c == "." for c in host):
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Handle common 2-level public suffixes (co.uk, com.au, ...).
    two = ".".join(parts[-2:])
    if parts[-2] in ("co", "com", "org", "net", "gov", "edu", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return two


# ── Upload protocol ──────────────────────────────────────────────────────────
@app.post("/v1/traces/init")
async def trace_init(request: Request, authorization: str = Header(None)):
    _check_auth(authorization)
    _ensure_dirs()
    body = await request.json()
    trace_id = body.get("trace_id")
    if not trace_id:
        raise HTTPException(400, "trace_id required")
    upload_id = _upload_id_for(trace_id)
    with update_meta(upload_id, create=True) as meta:
        if not meta:
            meta.update({
                "upload_id": upload_id,
                "trace_id": trace_id,
                "schema_version": body.get("schema_version"),
                "recording_mode": body.get("recording_mode"),
                "label": body.get("label", ""),
                "description": body.get("description", ""),
                "tags": body.get("tags", []),
                "summary": body.get("summary", {}),
                "capture_settings": body.get("capture_settings", {}),
                "status": "initialized",
                "accepted_chunks": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            (_trace_dir(upload_id) / "chunks").mkdir(parents=True, exist_ok=True)
        return {
            "upload_id": upload_id,
            "accepted_chunks": meta.get("accepted_chunks", []),
            "status": meta.get("status", "initialized"),
        }


@app.put("/v1/traces/{upload_id}/chunks/{chunk_index}")
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    request: Request,
    authorization: str = Header(None),
    x_trace_chunk_sha256: str = Header(None),
    x_trace_chunk_kind: str = Header("events"),
):
    _check_auth(authorization)
    _validate_upload_id(upload_id)
    if x_trace_chunk_kind not in _VALID_CHUNK_KINDS:
        raise HTTPException(400, f"Invalid chunk kind: {x_trace_chunk_kind!r}")
    limit = MAX_MEDIA_CHUNK_BYTES if x_trace_chunk_kind == "media" else MAX_EVENT_CHUNK_BYTES
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"Chunk exceeds {limit} byte limit")
    body = await request.body()
    if len(body) > limit:
        raise HTTPException(413, f"Chunk exceeds {limit} byte limit")
    actual_sha = hashlib.sha256(body).hexdigest()
    if x_trace_chunk_sha256 and actual_sha != x_trace_chunk_sha256:
        raise HTTPException(409, f"SHA-256 mismatch: expected {x_trace_chunk_sha256}, got {actual_sha}")
    with update_meta(upload_id) as meta:
        existing = next((ac for ac in meta.get("accepted_chunks", []) if ac["index"] == chunk_index), None)
        if existing:
            if existing["sha256"] != actual_sha:
                raise HTTPException(409, f"Chunk {chunk_index} already uploaded with different hash")
        else:
            cp = _trace_dir(upload_id) / "chunks" / f"{chunk_index:04d}.{x_trace_chunk_kind}.gz"
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_bytes(body)
            meta.setdefault("accepted_chunks", []).append({
                "index": chunk_index, "kind": x_trace_chunk_kind,
                "sha256": actual_sha, "bytes": len(body),
            })
            meta["status"] = "uploading"
    return {"ok": True, "chunk_index": chunk_index, "sha256": actual_sha}


@app.post("/v1/traces/{upload_id}/finalize")
async def finalize_trace(upload_id: str, request: Request, authorization: str = Header(None)):
    _check_auth(authorization)
    _validate_upload_id(upload_id)
    body = await request.json()
    with update_meta(upload_id) as meta:
        meta["status"] = "processing"
        meta["finalize_manifest"] = body
        meta["finalized_at"] = datetime.now(timezone.utc).isoformat()
        trace_id = meta["trace_id"]
        meta_snapshot = dict(meta)

    errors = _assemble_trace(upload_id, meta_snapshot)
    if errors:
        with update_meta(upload_id) as meta:
            meta["status"] = "degraded"
            meta["assembly_errors"] = errors
        logger.error("[finalize] %s: %d chunk(s) failed → degraded", trace_id, len(errors))
        return {"status": "degraded", "trace_id": trace_id, "assembly_errors": errors}

    with update_meta(upload_id) as meta:
        meta["status"] = "accepted"

    if _load_config().get("auto_distill"):
        with update_meta(upload_id) as meta:
            meta["distill_status"] = "running"
        threading.Thread(target=_ingest_distill_install, args=(upload_id,), daemon=True).start()
        logger.info("[finalize] %s: auto-distill started", upload_id)

    return {"status": "accepted", "trace_id": trace_id}


@app.get("/v1/traces/{upload_id}/status")
def trace_status(upload_id: str, authorization: str = Header(None)):
    _check_auth(authorization)
    meta = _load_meta(upload_id)
    accepted = [c["index"] for c in meta.get("accepted_chunks", [])]
    return {
        "upload_id": upload_id,
        "status": meta.get("status"),
        "accepted_chunks": accepted,
        "distill_status": meta.get("distill_status"),
        "distill_result": meta.get("distill_result"),
    }


# ── Assembly + conversion to the distiller's track schema ────────────────────
def _assemble_trace(upload_id: str, meta: dict) -> list[dict]:
    td = _trace_dir(upload_id)
    events: list[dict] = []
    errors: list[dict] = []
    for ci in sorted(meta.get("accepted_chunks", []), key=lambda c: c["index"]):
        if ci["kind"] != "events":
            continue
        idx = ci["index"]
        cp = td / "chunks" / f"{idx:04d}.events.gz"
        if not cp.exists():
            errors.append({"index": idx, "error": "chunk file missing"})
            continue
        try:
            raw = gzip.decompress(cp.read_bytes()).decode("utf-8")
        except Exception as e:
            errors.append({"index": idx, "error": f"decompress failed: {e}"})
            continue
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                errors.append({"index": idx, "error": f"bad event json: {e}"})

    trace = {
        "schema_version": meta.get("schema_version") or "journey_trace_v1",
        "trace_id": meta["trace_id"],
        "recording_mode": meta.get("recording_mode"),
        "label": meta.get("label", ""),
        "description": meta.get("description", ""),
        "tags": meta.get("tags", []),
        "summary": meta.get("summary", {}),
        "events": events,
    }
    _atomic_write(td / "trace.json", json.dumps(trace, ensure_ascii=False))
    logger.info("[assemble] %s: %d events", meta["trace_id"], len(events))
    return errors


# ── Distillation pipeline (harness: atomize → classify → bucket → distill) ────
def _harness_env(cfg: dict) -> dict:
    env = dict(os.environ)
    env["JFL_DATA_DIR"] = str(DATA_DIR)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env["SF_LLM_KEY"] = cfg.get("llm_key", "")
    env["SF_LLM_BASE"] = cfg.get("llm_base", "")
    env["SF_DISTILL_MODEL"] = cfg.get("distill_model", "")
    return env


def _run_harness(args: list[str], cfg: dict, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "harness.main", *args],
        cwd=str(REPO), capture_output=True, text=True, env=_harness_env(cfg), timeout=timeout,
    )


def _install_all(cfg: dict) -> list[dict]:
    """Install every distilled bucket skill into the chosen skills root (Code + Desktop zip)."""
    from harness.install import install_registry
    return install_registry(Path(cfg["skills_root"]))


def _ingest_distill_install(upload_id: str) -> None:
    """Ingest one trace into the buckets, distill ready buckets, install skills."""
    cfg = _load_config()
    try:
        if not cfg.get("llm_key"):
            raise RuntimeError("no LLM API key configured (set it in Settings)")
        trace_json = _trace_dir(upload_id) / "trace.json"
        r1 = _run_harness(["ingest", "--track-file", str(trace_json)], cfg, 600)
        if r1.returncode != 0:
            raise RuntimeError(f"ingest failed: {(r1.stderr or r1.stdout)[-1500:]}")
        r2 = _run_harness(["distill"], cfg, 1200)
        if r2.returncode != 0:
            raise RuntimeError(f"distill failed: {(r2.stderr or r2.stdout)[-1500:]}")
        installed = _install_all(cfg)
        with update_meta(upload_id) as meta:
            meta["distill_status"] = "done"
            meta["distill_result"] = {
                "ok": True, "installed_count": len(installed),
                "ingest_log": (r1.stdout or "")[-1500:],
                "distill_log": (r2.stdout or "")[-1500:],
            }
        logger.info("[pipeline] %s: ok, %d skill(s) installed", upload_id, len(installed))
    except Exception as e:  # noqa: BLE001
        with update_meta(upload_id) as meta:
            meta["distill_status"] = "error"
            meta["distill_result"] = {"ok": False, "error": str(e)}
        logger.error("[pipeline] %s failed: %s", upload_id, e)


# ── Control API (for the panel) ──────────────────────────────────────────────
def _all_meta() -> list[dict]:
    out = []
    if TRACES_DIR.exists():
        for d in sorted(TRACES_DIR.iterdir()):
            mf = d / "meta.json"
            if mf.exists():
                try:
                    out.append(json.loads(mf.read_text()))
                except json.JSONDecodeError:
                    continue
    return out


@app.get("/api/traj")
def api_traj(authorization: str = Header(None)):
    _check_auth(authorization)
    items = []
    for m in _all_meta():
        items.append({
            "upload_id": m.get("upload_id"),
            "label": m.get("label", ""),
            "description": m.get("description", ""),
            "status": m.get("status"),
            "distill_status": m.get("distill_status"),
            "created_at": m.get("created_at"),
            "n_chunks": len(m.get("accepted_chunks", [])),
        })
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"trajectories": items}


@app.get("/api/traj/{upload_id}")
def api_traj_one(upload_id: str, authorization: str = Header(None)):
    _check_auth(authorization)
    meta = _load_meta(upload_id)
    trace_path = _trace_dir(upload_id) / "trace.json"
    trace = json.loads(trace_path.read_text()) if trace_path.exists() else None
    return {"meta": meta, "trace": trace}


@app.get("/api/skills")
def api_skills(authorization: str = Header(None)):
    _check_auth(authorization)
    cfg = _load_config()
    root = Path(cfg["skills_root"])
    installed = []
    if root.exists():
        for d in sorted(root.iterdir()):
            sk = d / "SKILL.md"
            if sk.is_file():
                installed.append({"name": d.name, "path": str(sk),
                                  "bytes": sk.stat().st_size})
    return {"skills_root": str(root), "installed": installed}


@app.post("/api/distill/{upload_id}")
def api_distill(upload_id: str, authorization: str = Header(None)):
    _check_auth(authorization)
    _load_meta(upload_id)  # 404s if unknown
    with update_meta(upload_id) as meta:
        meta["distill_status"] = "running"
    threading.Thread(target=_ingest_distill_install, args=(upload_id,), daemon=True).start()
    return {"ok": True, "upload_id": upload_id}


# ── Capacity buckets (per-site skill buckets) ────────────────────────────────
def _read_json_file(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


@app.get("/api/buckets")
def api_buckets(authorization: str = Header(None)):
    _check_auth(authorization)
    buckets = _read_json_file(HARNESS_STATE / "buckets.json", {}).get("buckets", {})
    reg = {s["capacity_id"]: s for s in _read_json_file(HARNESS_STATE / "registry.json", {}).get("skills", [])}
    by_domain: dict[str, list] = {}
    for bid, b in buckets.items():
        dom = b.get("domain", "")
        entry = reg.get(bid)
        by_domain.setdefault(dom, []).append({
            "bucket_id": bid,
            "capacity": b.get("canonical_capacity", ""),
            "description": b.get("description", ""),
            "segments": len(b.get("segment_ids", [])),
            "version": b.get("distill_version", 0),
            "distilled": b.get("distill_version", 0) > 0,
            "dirty": b.get("dirty", True),
            "skill_name": entry.get("skill_name") if entry else None,
            "scope": entry.get("scope", "") if entry else "",
        })
    domains = [
        {"domain": d, "buckets": sorted(v, key=lambda x: -x["segments"])}
        for d, v in sorted(by_domain.items())
    ]
    total = sum(len(d["buckets"]) for d in domains)
    return {"domains": domains, "bucket_count": total}


def _bucket_skill_dir(bucket_id: str) -> Path:
    if "::" not in bucket_id:
        raise HTTPException(400, "bad bucket id")
    domain, cap = bucket_id.split("::", 1)
    if "/" in domain or "/" in cap or ".." in bucket_id:
        raise HTTPException(400, "bad bucket id")
    return HARNESS_STATE / "skills" / domain / cap


@app.get("/api/skill")
def api_skill(bucket: str, authorization: str = Header(None)):
    _check_auth(authorization)
    d = _bucket_skill_dir(bucket)
    sk = d / "SKILL.md"
    if not sk.is_file():
        raise HTTPException(404, "no distilled skill for this bucket yet")
    tg = d / "TRACE_GUIDE.md"
    return {
        "bucket_id": bucket,
        "skill_md": sk.read_text(),
        "trace_guide_md": tg.read_text() if tg.is_file() else "",
        "meta": _read_json_file(d / "meta.json", {}),
    }


@app.get("/api/skill/zip")
def api_skill_zip(bucket: str, authorization: str = Header(None)):
    _check_auth(authorization)
    d = _bucket_skill_dir(bucket)
    zips = sorted(d.glob("*.zip"))
    if not zips:
        raise HTTPException(404, "no Desktop bundle yet (distill first)")
    return FileResponse(zips[0], media_type="application/zip", filename=zips[0].name)


@app.delete("/api/skills/{name}")
def api_skill_delete(name: str, authorization: str = Header(None)):
    _check_auth(authorization)
    name = _safe_name(name)
    cfg = _load_config()
    d = Path(cfg["skills_root"]) / name
    if d.is_dir():
        for p in sorted(d.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        d.rmdir()
        return {"ok": True, "removed": str(d)}
    raise HTTPException(404, f"No installed skill named {name!r}")


@app.get("/api/config")
def api_config_get(authorization: str = Header(None)):
    _check_auth(authorization)
    cfg = _load_config()
    cfg["llm_key_set"] = bool(cfg.get("llm_key"))  # never echo the key back
    cfg.pop("llm_key", None)
    return cfg


@app.put("/api/config")
async def api_config_put(request: Request, authorization: str = Header(None)):
    _check_auth(authorization)
    body = await request.json()
    cfg = _load_config()
    for k in ("llm_base", "distill_model", "skills_root", "auto_distill"):
        if k in body:
            cfg[k] = body[k]
    if body.get("llm_key"):  # only overwrite when a non-empty key is sent
        cfg["llm_key"] = body["llm_key"]
    _save_config(cfg)
    return {"ok": True}


@app.get("/api/ext")
def api_ext(authorization: str = Header(None)):
    _check_auth(authorization)
    return {
        "build_dir": str(EXT_BUILD),
        "built": EXT_BUILD.exists(),
        "steps": [
            "Open chrome://extensions",
            "Enable Developer mode (top-right)",
            "Click 'Load unpacked' and select the build_dir above",
            "The extension is pre-configured to talk to this local server",
        ],
    }


# ── Claude Desktop integration (browser execution via Playwright MCP) ─────────
def _claude_desktop_config_path() -> Path:
    """OS-specific path to Claude Desktop's MCP config file.

    Claude Desktop (the app) reads MCP servers from claude_desktop_config.json.
    This is unrelated to Claude Code; it is how the desktop app gets browser
    control (Playwright MCP). Path differs per platform.
    """
    override = os.environ.get("JFL_CLAUDE_DESKTOP_CONFIG")
    if override:
        return Path(override)
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    # Linux (community builds) / dev box
    return home / ".config" / "Claude" / "claude_desktop_config.json"


_PLAYWRIGHT_MCP = {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}


@app.get("/api/desktop/config")
def api_desktop_config(authorization: str = Header(None)):
    _check_auth(authorization)
    cfg_path = _claude_desktop_config_path()
    exists = cfg_path.exists()
    has_pw = False
    if exists:
        try:
            data = json.loads(cfg_path.read_text())
            has_pw = "playwright" in (data.get("mcpServers") or {})
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "config_path": str(cfg_path),
        "config_exists": exists,
        "playwright_configured": has_pw,
        "platform": platform.system(),
        "snippet": {"mcpServers": {"playwright": _PLAYWRIGHT_MCP}},
        "note": "Restart Claude Desktop after configuring for the change to take effect.",
    }


@app.post("/api/desktop/playwright")
def api_desktop_playwright(authorization: str = Header(None)):
    """Add the Playwright MCP server to claude_desktop_config.json (idempotent).

    Backs up any existing config to <file>.jfl.bak before writing. After this,
    the user restarts Claude Desktop and distilled skills can really drive a
    browser (click/type/navigate) instead of being advisory text only.
    """
    _check_auth(authorization)
    cfg_path = _claude_desktop_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text())
        except json.JSONDecodeError:
            raise HTTPException(422, f"{cfg_path} is not valid JSON; fix or remove it first")
        shutil.copy2(cfg_path, cfg_path.with_suffix(cfg_path.suffix + ".jfl.bak"))
    servers = data.setdefault("mcpServers", {})
    already = servers.get("playwright") == _PLAYWRIGHT_MCP
    servers["playwright"] = _PLAYWRIGHT_MCP
    _atomic_write(cfg_path, json.dumps(data, indent=2, ensure_ascii=False))
    logger.info("[desktop] playwright MCP %s in %s", "present" if already else "written", cfg_path)
    return {
        "ok": True,
        "config_path": str(cfg_path),
        "already_configured": already,
        "restart_required": not already,
        "message": "Playwright MCP configured. Restart Claude Desktop to apply.",
    }


# ── Static control panel (mounted last so /api & /v1 win) ────────────────────
@app.get("/")
def index():
    idx = APP_BUILD / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return JSONResponse({"ok": True, "msg": "Journey-Forge Local server running. "
                         "Build the control panel (app/) to see the UI."})


if APP_BUILD.exists():
    app.mount("/", StaticFiles(directory=str(APP_BUILD), html=True), name="app")


if __name__ == "__main__":
    import uvicorn
    _ensure_dirs()
    _load_api_keys()
    port = int(os.environ.get("JFL_PORT", 8099))
    uvicorn.run(app, host="127.0.0.1", port=port)
