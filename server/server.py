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
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
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

# Overridable so a frozen/native bundle can point at its bundled resources.
APP_BUILD = Path(os.environ.get("JFL_APP_BUILD", str(REPO / "app" / "dist")))           # control-panel SPA
EXT_BUILD = Path(os.environ.get("JFL_EXT_BUILD", str(REPO / "extension" / "dist" / "chrome-mv3")))  # wxt build

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
        # The model used for the cheap per-segment classification + bucket
        # consolidation. Blank → reuse distill_model (a custom gateway may not
        # serve a separate Haiku-class model).
        "classify_model": os.environ.get("SF_CLASSIFY_MODEL", ""),
        # Always skip TLS verification — the product's LLM gateway uses a
        # self-signed / corporate-MITM chain. Not user-configurable.
        "llm_insecure": True,
        # A capability bucket distills once it has at least this many segments.
        "min_bucket_size": int(os.environ.get("SF_MIN_BUCKET_SIZE", "1")),
        # global ~/.claude/skills  OR  an absolute project path's .claude/skills
        # (empty env value falls back to the global default)
        "skills_root": os.environ.get("JFL_SKILLS_ROOT") or str(Path.home() / ".claude" / "skills"),
        # Recording always auto-processes on finalize. Not user-configurable.
        "auto_distill": True,
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

    # Recording always auto-processes — no manual "Process" step.
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
# Runs IN-PROCESS (no subprocess), so it works when the whole thing is frozen
# into a single binary (PyInstaller sidecar / native app). Serialized by a lock
# so concurrent finalizes don't race on buckets.json.
_PIPELINE_LOCK = threading.Lock()
_PROGRESS: dict[str, dict] = {}     # upload_id → {phase,current,total,detail} (in-memory, live)


def _apply_harness_config(cfg: dict) -> None:
    from harness import config as hconfig
    hconfig.LLM_KEY = cfg.get("llm_key", "")
    if cfg.get("llm_base"):
        hconfig.LLM_BASE = str(cfg["llm_base"]).rstrip("/")
    hconfig.LLM_INSECURE = True  # always — gateway uses a self-signed chain
    hconfig.MIN_BUCKET_SIZE = int(cfg.get("min_bucket_size") or 1)
    model = cfg.get("distill_model")
    if model:
        hconfig.DISTILL_MODEL = model
    # Classify/consolidate use the configured classify_model, or fall back to the
    # distill model (no hidden Haiku default a custom gateway might not serve).
    classify = cfg.get("classify_model") or model
    if classify:
        hconfig.CLASSIFY_MODEL = classify
        hconfig.BUCKET_MODEL = classify


def _ingest_distill_install(upload_id: str) -> None:
    """Ingest one trace into the buckets, distill ready buckets, install skills."""
    cfg = _load_config()

    def _rep(phase: str, current: int = 0, total: int = 0, detail: str = "") -> None:
        _PROGRESS[upload_id] = {"phase": phase, "current": current, "total": total, "detail": detail}

    try:
        if not cfg.get("llm_key"):
            raise RuntimeError("no LLM API key configured (set it in Settings)")
        from harness.main import run_distill, run_ingest_file
        from harness.install import install_registry
        from harness import progress as hprogress

        trace_json = _trace_dir(upload_id) / "trace.json"
        with _PIPELINE_LOCK:
            hprogress.set_reporter(_rep)
            _apply_harness_config(cfg)
            _rep("ingest", 0, 0, "atomizing")
            run_ingest_file(trace_json)
            run_distill()
            _rep("install", 0, 0, "")
            installed = install_registry(Path(cfg["skills_root"]))
            hprogress.set_reporter(None)
        _rep("done", 0, 0, f"{len(installed)} skill(s)")
        n_buckets = len(_read_json_file(HARNESS_STATE / "buckets.json", {}).get("buckets", {}))
        note = ""
        if not installed:
            note = ("No skills produced — likely the classify/distill LLM call failed "
                    "(check the LLM key/base/model; a custom gateway may not serve the model).")
        with update_meta(upload_id) as meta:
            meta["distill_status"] = "done"
            meta["distill_result"] = {"ok": True, "installed_count": len(installed),
                                      "buckets": n_buckets, "note": note}
        logger.info("[pipeline] %s: ok, %d skill(s), %d bucket(s)%s",
                    upload_id, len(installed), n_buckets, f" — {note}" if note else "")
    except Exception as e:  # noqa: BLE001
        try:
            from harness import progress as hprogress
            hprogress.set_reporter(None)
        except Exception:
            pass
        _rep("error", 0, 0, str(e)[:200])
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
            "progress": _PROGRESS.get(m.get("upload_id")),
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
    for k in ("llm_base", "distill_model", "classify_model", "skills_root"):
        if k in body:
            cfg[k] = body[k]
    # auto_distill and llm_insecure are always-on, not user-configurable.
    cfg["auto_distill"] = True
    cfg["llm_insecure"] = True
    if "min_bucket_size" in body:
        try:
            cfg["min_bucket_size"] = max(1, int(body["min_bucket_size"]))
        except (TypeError, ValueError):
            pass
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
            "Click ‘Open chrome://extensions’ below",
            "Turn on Developer mode (top-right toggle)",
            "Click ‘Load unpacked’ and pick the folder that just opened in Finder",
            "It’s pre-configured to talk to this app — done",
        ],
    }


def _os_open(*args: str) -> None:
    """Open a folder/URL/app via the OS handler (Finder, default browser, …)."""
    if sys.platform == "darwin":
        subprocess.run(["open", *args], check=False)
    elif sys.platform.startswith("win"):
        subprocess.run(["cmd", "/c", "start", "", *args], check=False)
    else:
        subprocess.run(["xdg-open", *args], check=False)


@app.post("/api/ext/reveal")
def api_ext_reveal(authorization: str = Header(None)):
    _check_auth(authorization)
    if not EXT_BUILD.exists():
        raise HTTPException(404, "extension folder not found")
    _os_open(str(EXT_BUILD))           # reveal the folder in Finder/Explorer
    return {"ok": True, "path": str(EXT_BUILD)}


@app.post("/api/ext/open-chrome")
def api_ext_open_chrome(authorization: str = Header(None)):
    _check_auth(authorization)
    if sys.platform == "darwin":
        # Open the extensions page in Chrome (best-effort; falls back to Finder reveal).
        r = subprocess.run(["open", "-a", "Google Chrome", "chrome://extensions/"], check=False)
        if r.returncode != 0:
            subprocess.run(["open", "chrome://extensions/"], check=False)
    elif sys.platform.startswith("win"):
        subprocess.run(["cmd", "/c", "start", "chrome", "chrome://extensions/"], check=False)
    else:
        subprocess.run(["google-chrome", "chrome://extensions/"], check=False)
    return {"ok": True}


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


# ── Node.js detection + one-click install (Playwright MCP needs npx) ──────────
NODE_VERSION = os.environ.get("JFL_NODE_VERSION", "v22.11.0")


def _node_arch() -> str:
    return "arm64" if platform.machine() == "arm64" else "x64"


def _node_private_dir() -> Path:
    return DATA_DIR / "node" / f"node-{NODE_VERSION}-darwin-{_node_arch()}"


def _find_npx() -> tuple[str | None, str | None]:
    """Resolve absolute (npx, node). Claude Desktop spawns MCP servers with a
    minimal GUI PATH, so a bare 'npx' often fails — we always write absolutes."""
    # 1) our private copy
    priv = _node_private_dir() / "bin"
    if (priv / "npx").exists() and (priv / "node").exists():
        return str(priv / "npx"), str(priv / "node")
    # 2) common install locations + nvm
    cand_dirs = [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin")]
    nvm = Path.home() / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        cand_dirs += [p / "bin" for p in sorted(nvm.iterdir(), reverse=True)]
    for d in cand_dirs:
        if (d / "npx").exists() and (d / "node").exists():
            return str(d / "npx"), str(d / "node")
    # 3) whatever is on PATH
    npx, node = shutil.which("npx"), shutil.which("node")
    if npx and node:
        return npx, node
    return None, None


def _node_version(node: str | None) -> str:
    if not node:
        return ""
    try:
        return subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _playwright_entry() -> dict:
    """Playwright MCP server entry with an ABSOLUTE npx + a PATH that includes
    node's dir (+ TLS bypass for self-signed / corporate-MITM npm download)."""
    npx, node = _find_npx()
    if not npx:
        return {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}  # fallback (likely fails on GUI PATH)
    node_dir = str(Path(npx).parent)
    return {
        "command": npx,
        "args": ["-y", "@playwright/mcp@latest"],
        "env": {
            "PATH": f"{node_dir}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
        },
    }


def _self_mcp_command() -> tuple[str, list[str]]:
    """How to relaunch THIS program as the stdio MCP skill server.

    Frozen (PyInstaller sidecar): the binary itself + the 'mcp-skill' subcommand.
    Dev: the python interpreter + server.py + 'mcp-skill'.
    """
    if getattr(sys, "frozen", False):
        return sys.executable, ["mcp-skill"]
    return sys.executable, [str(Path(__file__).resolve()), "mcp-skill"]


def _skill_mcp_entry() -> dict:
    """MCP entry that serves distilled skills to Claude Desktop. It reads the
    registry fresh on each call, so newly-distilled skills are available with no
    restart — only ADDING this server needs the (one-time) restart."""
    command, args = _self_mcp_command()
    return {
        "command": command,
        "args": args,
        # Point the MCP process at the same data dir the pipeline writes to.
        "env": {"JFL_DATA_DIR": str(DATA_DIR), "PYTHONUNBUFFERED": "1"},
    }


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
    has_skills = False
    if exists:
        try:
            data = json.loads(cfg_path.read_text())
            has_skills = "journey-forge-skills" in (data.get("mcpServers") or {})
        except (json.JSONDecodeError, OSError):
            pass
    npx, node = _find_npx()
    return {
        "config_path": str(cfg_path),
        "config_exists": exists,
        "playwright_configured": has_pw,
        "skills_mcp_configured": has_skills,
        "platform": platform.system(),
        "node_found": bool(npx),
        "node_path": node,
        "npx_path": npx,
        "node_version": _node_version(node),
        "snippet": {"mcpServers": {
            "playwright": _playwright_entry(),
            "journey-forge-skills": _skill_mcp_entry(),
        }},
        "note": "Restart Claude Desktop after configuring for the change to take effect.",
    }


@app.get("/api/desktop/node")
def api_desktop_node(authorization: str = Header(None)):
    _check_auth(authorization)
    npx, node = _find_npx()
    return {
        "found": bool(npx),
        "npx": npx,
        "node": node,
        "version": _node_version(node),
        "install_target": str(_node_private_dir()),
    }


@app.post("/api/desktop/install-node")
def api_desktop_install_node(authorization: str = Header(None)):
    """Download an isolated Node.js into the app's data dir if none is found,
    so Playwright MCP (npx) works without the user installing anything."""
    _check_auth(authorization)
    npx, node = _find_npx()
    if npx:
        return {"ok": True, "already": True, "npx": npx, "node": node, "version": _node_version(node)}
    if sys.platform != "darwin":
        raise HTTPException(400, "auto-install currently supports macOS only")
    base = DATA_DIR / "node"
    base.mkdir(parents=True, exist_ok=True)
    url = f"https://nodejs.org/dist/{NODE_VERSION}/node-{NODE_VERSION}-darwin-{_node_arch()}.tar.gz"
    tgz = base / "node.tgz"
    try:
        try:
            with urllib.request.urlopen(url, timeout=180) as r, open(tgz, "wb") as f:  # noqa: S310
                shutil.copyfileobj(r, f)
        except ssl.SSLError:  # self-signed / MITM cert in the chain
            ctx = ssl._create_unverified_context()  # noqa: S323
            with urllib.request.urlopen(url, timeout=180, context=ctx) as r, open(tgz, "wb") as f:  # noqa: S310
                shutil.copyfileobj(r, f)
        with tarfile.open(tgz) as t:
            t.extractall(base)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"node download/extract failed: {e}")
    finally:
        tgz.unlink(missing_ok=True)
    npx, node = _find_npx()
    if not npx:
        raise HTTPException(500, "installed Node but npx not found afterwards")
    logger.info("[desktop] installed Node %s → %s", NODE_VERSION, npx)
    return {"ok": True, "npx": npx, "node": node, "version": _node_version(node)}


@app.post("/api/desktop/playwright")
def api_desktop_playwright(authorization: str = Header(None)):
    """Add the Playwright MCP server to claude_desktop_config.json (idempotent).

    Writes an absolute npx path + PATH env (GUI apps have a minimal PATH), so the
    server actually spawns. Backs up any existing config to <file>.jfl.bak.
    """
    _check_auth(authorization)
    npx, _node = _find_npx()
    if not npx:
        raise HTTPException(400, "Node.js / npx not found — install it first (POST /api/desktop/install-node)")
    entry = _playwright_entry()
    cfg_path = _claude_desktop_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text())
        except json.JSONDecodeError:
            raise HTTPException(422, f"{cfg_path} is not valid JSON; fix or remove it first")
        shutil.copy2(cfg_path, cfg_path.with_suffix(cfg_path.suffix + ".jfl.bak"))
    skill_entry = _skill_mcp_entry()
    servers = data.setdefault("mcpServers", {})
    already = servers.get("playwright") == entry and servers.get("journey-forge-skills") == skill_entry
    servers["playwright"] = entry
    # Install the distilled-skill MCP server alongside Playwright so both come up
    # in the SAME restart. Afterwards, newly-distilled skills are served live
    # (the tool reads the registry per call) — no further restarts needed.
    servers["journey-forge-skills"] = skill_entry
    _atomic_write(cfg_path, json.dumps(data, indent=2, ensure_ascii=False))
    logger.info("[desktop] playwright + skills MCP %s in %s (npx=%s)",
                "present" if already else "written", cfg_path, npx)
    return {
        "ok": True,
        "config_path": str(cfg_path),
        "npx": npx,
        "skills_mcp": True,
        "already_configured": already,
        "restart_required": not already,
        "message": ("Playwright + distilled-skill MCP configured. Restart Claude Desktop ONCE to apply; "
                    "after that, recording → auto-distill makes new skills available with no restart."),
    }


@app.post("/api/desktop/mcp")
async def api_desktop_mcp(request: Request, authorization: str = Header(None)):
    """Declaratively set which of our MCP servers Claude Desktop has enabled.

    Body: {"playwright": bool, "skills": bool}. Each is independent — the panel
    exposes one toggle per server. Playwright needs Node; the skills server does
    not (it's the frozen sidecar itself). Backs up the config before writing."""
    _check_auth(authorization)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    want_pw = bool(body.get("playwright"))
    want_skills = bool(body.get("skills"))
    if want_pw:
        npx, _ = _find_npx()
        if not npx:
            raise HTTPException(400, "Node.js / npx not found — install it first (Install Node)")
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
    if want_pw:
        servers["playwright"] = _playwright_entry()
    else:
        servers.pop("playwright", None)
    if want_skills:
        servers["journey-forge-skills"] = _skill_mcp_entry()
    else:
        servers.pop("journey-forge-skills", None)
    _atomic_write(cfg_path, json.dumps(data, indent=2, ensure_ascii=False))
    logger.info("[desktop] MCP set playwright=%s skills=%s in %s", want_pw, want_skills, cfg_path)
    return {
        "ok": True,
        "playwright": want_pw,
        "skills": want_skills,
        "config_path": str(cfg_path),
        "restart_required": True,
        "message": "Updated Claude Desktop MCP servers. Restart Claude Desktop once to apply.",
    }


@app.post("/api/desktop/disconnect")
def api_desktop_disconnect(authorization: str = Header(None)):
    """Remove our MCP servers (playwright + journey-forge-skills) from Claude
    Desktop's config — the reverse of /api/desktop/playwright, so the panel can
    offer a toggle. Backs up the config first."""
    _check_auth(authorization)
    cfg_path = _claude_desktop_config_path()
    if not cfg_path.exists():
        return {"ok": True, "removed": [], "restart_required": False,
                "message": "No Claude Desktop config; nothing to disconnect."}
    try:
        data = json.loads(cfg_path.read_text())
    except json.JSONDecodeError:
        raise HTTPException(422, f"{cfg_path} is not valid JSON; fix or remove it first")
    servers = data.get("mcpServers") or {}
    removed = [n for n in ("playwright", "journey-forge-skills") if n in servers]
    if removed:
        shutil.copy2(cfg_path, cfg_path.with_suffix(cfg_path.suffix + ".jfl.bak"))
        for n in removed:
            del servers[n]
        data["mcpServers"] = servers
        _atomic_write(cfg_path, json.dumps(data, indent=2, ensure_ascii=False))
        logger.info("[desktop] disconnected MCP %s from %s", removed, cfg_path)
    return {
        "ok": True,
        "removed": removed,
        "restart_required": bool(removed),
        "message": "Disconnected from Claude Desktop. Restart Claude Desktop to apply.",
    }


# ── Static control panel (mounted last so /api & /v1 win) ────────────────────
# The panel is a single self-contained HTML doc. The native app's WKWebView
# caches it aggressively (same 127.0.0.1:8099 URL across app versions), so a new
# build could keep showing the old UI. Serve it no-store so the webview always
# fetches the version bundled in the running app.
_NOCACHE = {"Cache-Control": "no-store, must-revalidate"}


@app.get("/api/version")
def api_version():
    """The build SHA baked in at freeze time (app/dist/build.json). Lets the
    panel show which build is actually running — no auth so it always renders."""
    f = APP_BUILD / "build.json"
    sha = "dev"
    if f.is_file():
        try:
            sha = json.loads(f.read_text()).get("sha", "dev")
        except (json.JSONDecodeError, OSError):
            pass
    return {"sha": sha}


@app.get("/")
def index():
    idx = APP_BUILD / "index.html"
    if idx.exists():
        return FileResponse(idx, headers=_NOCACHE)
    return JSONResponse({"ok": True, "msg": "Journey-Forge Local server running. "
                         "Build the control panel (app/) to see the UI."})


if APP_BUILD.exists():
    app.mount("/", StaticFiles(directory=str(APP_BUILD), html=True), name="app")


if __name__ == "__main__":
    # Subcommand dispatch: the same binary doubles as the stdio MCP skill server
    # (Claude Desktop spawns it via claude_desktop_config.json). Paths/env are
    # already set above, so harness state resolves to the same data dir.
    if len(sys.argv) > 1 and sys.argv[1] == "mcp-skill":
        import skill_mcp
        skill_mcp.serve()
        sys.exit(0)

    import uvicorn
    _ensure_dirs()
    _load_api_keys()
    port = int(os.environ.get("JFL_PORT", 8099))
    uvicorn.run(app, host="127.0.0.1", port=port)
