#!/usr/bin/env python3
"""Frozen sidecar entry for the native (Tauri) app.

PyInstaller freezes THIS into a single binary that the Tauri shell spawns. It
runs the FastAPI server in-process (the distill pipeline is in-process too, so
no subprocess is needed inside the frozen binary). Resources (panel build,
extension build) ship inside the bundle; writable state goes to the user's
Application Support dir.

Run standalone for testing:  python packaging/sidecar_main.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _bundle_base() -> Path:
    # When frozen, PyInstaller unpacks data next to the executable (_MEIPASS).
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]   # repo root in dev


def _default_data_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "JourneyForgeLocal"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", home)) / "JourneyForgeLocal"
    return home / ".journey-forge-local"


def main() -> int:
    base = _bundle_base()

    # Subcommand: serve the stdio MCP skill server instead of the HTTP server.
    # Claude Desktop spawns `jfl-server mcp-skill` (see _skill_mcp_entry in
    # server.py); it reads the registry from JFL_DATA_DIR (passed in the entry's
    # env) and returns distilled skills over stdio. Must short-circuit BEFORE we
    # start uvicorn.
    if len(sys.argv) > 1 and sys.argv[1] == "mcp-skill":
        # skill_mcp now imports `harness` (semantic retrieval + playbook synthesis),
        # so both the repo/bundle base AND server/ must be importable.
        sys.path.insert(0, str(base))
        sys.path.insert(0, str(base / "server"))
        import skill_mcp  # noqa: E402
        skill_mcp.serve()
        return 0

    os.environ.setdefault("JFL_DATA_DIR", str(_default_data_dir()))
    os.environ.setdefault("JFL_APP_BUILD", str(base / "app" / "dist"))

    # The extension ships inside the bundle, but in a onefile build that path is
    # an EPHEMERAL temp dir (gone on exit) — Chrome's "Load unpacked" needs a
    # stable folder. Copy it next to the user's data so it persists and is easy
    # to find, and point the API at that copy.
    data_dir = Path(os.environ["JFL_DATA_DIR"])
    bundled_ext = base / "extension" / "dist" / "chrome-mv3"
    stable_ext = data_dir / "extension" / "chrome-mv3"
    if bundled_ext.is_dir():
        try:
            stable_ext.parent.mkdir(parents=True, exist_ok=True)
            if stable_ext.exists():
                shutil.rmtree(stable_ext)
            shutil.copytree(bundled_ext, stable_ext)
            os.environ["JFL_EXT_BUILD"] = str(stable_ext)
            print(f"[sidecar] extension ready at {stable_ext}", flush=True)
        except Exception as e:  # noqa: BLE001
            os.environ.setdefault("JFL_EXT_BUILD", str(bundled_ext))
            print(f"[sidecar] WARN: could not stage extension: {e}", flush=True)
    else:
        os.environ.setdefault("JFL_EXT_BUILD", str(bundled_ext))

    sys.path.insert(0, str(base))
    sys.path.insert(0, str(base / "server"))

    from server import app, _ensure_dirs, _load_api_keys  # noqa: E402
    import uvicorn  # noqa: E402

    _ensure_dirs()
    _load_api_keys()
    port = int(os.environ.get("JFL_PORT", "8099"))
    print(f"[sidecar] serving on http://127.0.0.1:{port}  data={os.environ['JFL_DATA_DIR']}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
