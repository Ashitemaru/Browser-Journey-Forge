#!/usr/bin/env python3
"""Journey-Forge Local — desktop entry point.

Starts the local server (server/server.py) on a background thread, then opens
the control panel in the system browser. Port selection is resilient:
  - prefer 8099 (the port the product extension is preset to);
  - if our own server is already running there, just open it (no second copy);
  - if some other process holds 8099, fall back to a free port and warn that the
    extension's default endpoint won't match until you update it.

Native pywebview window is opt-in via JFL_USE_PYWEBVIEW=1 (the browser is the
reliable default).
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO / "server"
PREFERRED_PORT = int(os.environ.get("JFL_PORT", 8099))

PORT = PREFERRED_PORT          # resolved in main()
URL = f"http://127.0.0.1:{PORT}/"

# localhost must never go through an HTTP proxy (corporate proxy env, etc.).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _load_env_local() -> None:
    """Load .env.local into os.environ (does not override existing vars).

    Prefers $JFL_HOME/.env.local (the writable working dir used by the macOS
    .app) and falls back to REPO/.env.local.
    """
    home = os.environ.get("JFL_HOME")
    env_file = Path(home) / ".env.local" if home else REPO / ".env.local"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _is_our_server(port: int) -> bool:
    """True if something on this port is *our* control panel (safe to reuse)."""
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}/", timeout=1.5) as r:  # noqa: S310
            return b"Journey-Forge" in r.read(4096)
    except Exception:  # noqa: BLE001
        return False


def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server() -> None:
    sys.path.insert(0, str(SERVER_DIR))
    import uvicorn  # noqa: WPS433
    from server import app, _ensure_dirs, _load_api_keys  # type: ignore

    _ensure_dirs()
    _load_api_keys()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


def _wait_for_server(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _OPENER.open(URL, timeout=1)  # noqa: S310
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.25)
    return False


def _open_panel(keep_alive: bool = True) -> None:
    if keep_alive and os.environ.get("JFL_USE_PYWEBVIEW") == "1":
        try:
            import webview  # pywebview
            webview.create_window("Journey-Forge Local", URL, width=1200, height=820)
            webview.start()
            return
        except Exception as e:  # noqa: BLE001
            print(f"[entry] native window unavailable ({e}); using the browser instead.")
    import webbrowser
    webbrowser.open(URL)
    if not keep_alive:
        return  # another instance owns the server; just open and exit
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[entry] shutting down.")


def main() -> int:
    global PORT, URL
    _load_env_local()

    # Already running? Just open it — don't start a second copy (this is the
    # usual cause of "address already in use").
    if _is_our_server(PREFERRED_PORT):
        PORT = PREFERRED_PORT
        URL = f"http://127.0.0.1:{PORT}/"
        print(f"\n[entry] Journey-Forge Local is already running at {URL} — opening it.\n")
        _open_panel(keep_alive=False)
        return 0

    PORT = PREFERRED_PORT if _port_free(PREFERRED_PORT) else _free_port()
    URL = f"http://127.0.0.1:{PORT}/"

    threading.Thread(target=_start_server, daemon=True).start()
    if not _wait_for_server():
        print(f"[entry] ERROR: server did not start on {URL}", file=sys.stderr)
        return 1

    print("\n" + "=" * 64)
    print(f"  Journey-Forge Local is running:  {URL}")
    if PORT != PREFERRED_PORT:
        print(f"  NOTE: port {PREFERRED_PORT} was busy, so we used {PORT}.")
        print(f"        The extension is preset to :{PREFERRED_PORT} — update its")
        print(f"        endpoint to {URL} in the extension settings, or free :{PREFERRED_PORT}.")
    print("  (opening it in your browser — keep this window open)")
    print("=" * 64 + "\n", flush=True)

    _open_panel()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
