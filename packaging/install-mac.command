#!/bin/bash
# Journey-Forge Local — macOS launcher.
# Double-click in Finder (or run in Terminal). First run sets up a private
# virtualenv and installs dependencies; later runs just launch the app.
set -euo pipefail

# Resolve repo root = this script's parent dir (packaging/..), even with spaces.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
cd "$REPO"

echo "=============================================="
echo " Journey-Forge Local"
echo " repo: $REPO"
echo "=============================================="

# 1. Python 3
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found."
  echo "Install it: 'xcode-select --install' (Command Line Tools) or https://www.python.org/downloads/"
  read -r -p "Press Return to close."
  exit 1
fi
echo "[1/4] python3: $(python3 --version)"

# 2. venv + deps
VENV="$REPO/.venv"
if [ ! -d "$VENV" ]; then
  echo "[2/4] creating virtualenv (.venv) ..."
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
echo "[2/4] installing/updating dependencies (fastapi, uvicorn, pywebview) ..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r "$REPO/requirements.txt"

# 3. config
if [ ! -f "$REPO/.env.local" ]; then
  cp "$REPO/config.example.env" "$REPO/.env.local"
  echo "[3/4] created .env.local — set SF_LLM_KEY in it before distilling skills."
else
  echo "[3/4] .env.local present."
fi

# 4. extension build check
if [ -d "$REPO/extension/dist/chrome-mv3" ]; then
  echo "[4/4] extension build present: extension/dist/chrome-mv3"
else
  echo "[4/4] NOTE: extension not built. To record tasks, build it once:"
  echo "        cd '$REPO/extension' && pnpm install && pnpm build"
  echo "      then load extension/dist/chrome-mv3 via chrome://extensions (Load unpacked)."
fi

echo "----------------------------------------------"
echo "Launching… the control panel opens in a native window."
echo "(Panel URL: http://127.0.0.1:8099/  — close the window to quit.)"
echo "----------------------------------------------"
exec python "$REPO/entry/main.py"
