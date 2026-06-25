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
    os.environ.setdefault("JFL_DATA_DIR", str(_default_data_dir()))
    os.environ.setdefault("JFL_APP_BUILD", str(base / "app" / "dist"))
    os.environ.setdefault("JFL_EXT_BUILD", str(base / "extension" / "dist" / "chrome-mv3"))

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
