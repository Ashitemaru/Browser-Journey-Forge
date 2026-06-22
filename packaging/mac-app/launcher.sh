#!/bin/bash
# Journey-Forge Local — macOS .app launcher (Contents/MacOS/JourneyForgeLocal).
# Runs project code from the (read-only) bundle; keeps venv/data/config in a
# writable Application Support dir. First run happens INSIDE a Terminal window so
# setup progress is visible, and any failure shows a dialog instead of vanishing.
set -uo pipefail

EXEC="$0"
EXEC_DIR="$(cd "$(dirname "$EXEC")" && pwd)"               # Contents/MacOS
RES="$(cd "$EXEC_DIR/../Resources/project" && pwd)"        # bundled repo
WORK="$HOME/Library/Application Support/JourneyForgeLocal" # writable state
LOG="$WORK/launch.log"
mkdir -p "$WORK"

dialog() { /usr/bin/osascript -e "display dialog \"$1\" with title \"Journey-Forge Local\" buttons {\"OK\"} default button 1" >/dev/null 2>&1 || true; }

# ── Re-launch inside Terminal.app on double-click, so setup is visible ─────────
if [ -z "${JFL_IN_TERMINAL:-}" ] && ! [ -t 1 ]; then
  CMD=$(printf 'JFL_IN_TERMINAL=1 %q; echo; echo "[window stays open — close it to quit the app]"' "$EXEC")
  /usr/bin/osascript >/dev/null 2>&1 <<OSA || { dialog "Could not open Terminal. Open it manually and run: '$EXEC'"; exit 1; }
tell application "Terminal"
  activate
  do script "$CMD"
end tell
OSA
  exit 0
fi

# ── In Terminal from here on: visible, step-by-step ───────────────────────────
exec > >(tee -a "$LOG") 2>&1
echo "==================================================================="
echo " Journey-Forge Local — starting"
echo " bundle : $RES"
echo " state  : $WORK   (venv, data, .env.local, logs live here)"
echo " $(date)"
echo "==================================================================="

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found."
  dialog "python3 not found. Install Command Line Tools:  xcode-select --install  (or python.org), then reopen."
  echo "Press Return to close."; read -r _ || true; exit 1
fi

# Apple Silicon: force every python invocation to run native arm64. If this
# Terminal session is under Rosetta (x86_64), pip would otherwise install
# x86_64 wheels (pydantic_core etc.) that fail to dlopen on arm64.
ARM=0
if [ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then ARM=1; fi
runpy() { if [ "$ARM" = 1 ]; then arch -arm64 "$@"; else "$@"; fi; }
WANT_ARCH=$([ "$ARM" = 1 ] && echo arm64 || echo x86_64)
echo "[1/4] python3: $(runpy python3 --version 2>&1)  (target arch: $WANT_ARCH)"

export JFL_HOME="$WORK"
export JFL_DATA_DIR="$WORK/data"
[ -f "$WORK/.env.local" ] || { cp "$RES/config.example.env" "$WORK/.env.local"; echo "[*] created $WORK/.env.local — set SF_LLM_KEY in it to enable distillation."; }

VENV="$WORK/.venv"
# Recreate the venv if it was built for the wrong architecture (e.g. an earlier
# Rosetta run left an x86_64 venv on an arm64 Mac).
if [ -d "$VENV" ] && [ "$(cat "$WORK/.venv.arch" 2>/dev/null || true)" != "$WANT_ARCH" ]; then
  echo "[*] existing venv has wrong architecture — rebuilding for $WANT_ARCH"; rm -rf "$VENV" "$WORK/.req.hash"
fi
if [ ! -d "$VENV" ]; then
  echo "[2/4] creating virtualenv ($WANT_ARCH, first run)…"
  runpy python3 -m venv "$VENV" || { dialog "Failed to create virtualenv. See $LOG"; read -r _ || true; exit 1; }
  echo "$WANT_ARCH" > "$WORK/.venv.arch"
else
  echo "[2/4] virtualenv present ($WANT_ARCH)."
fi
PY="$VENV/bin/python"

REQ_HASH="$(shasum "$RES/requirements.txt" | awk '{print $1}')"
if [ "$(cat "$WORK/.req.hash" 2>/dev/null || true)" != "$REQ_HASH" ]; then
  echo "[3/4] installing dependencies ($WANT_ARCH wheels; ~20–40s first time)…"
  runpy "$PY" -m pip install --upgrade pip || true
  if ! runpy "$PY" -m pip install -r "$RES/requirements.txt"; then
    echo "ERROR: dependency install failed."
    dialog "Dependency install failed. See log:  $LOG"
    echo "Press Return to close."; read -r _ || true; exit 1
  fi
  echo "$REQ_HASH" > "$WORK/.req.hash"
else
  echo "[3/4] dependencies already installed."
fi

# (The distillation harness is pure Python and runs in this same venv — no Node
#  needed at runtime. The extension is prebuilt in the bundle.)

echo "[4/4] launching server + opening the control panel in your browser…"
echo "      panel → http://127.0.0.1:${JFL_PORT:-8099}/"
echo "-------------------------------------------------------------------"
runpy "$PY" "$RES/entry/main.py"
RC=$?
echo "-------------------------------------------------------------------"
echo "server exited (code $RC)."
[ $RC -ne 0 ] && dialog "The server stopped (code $RC). See log:  $LOG"
echo "Press Return to close."; read -r _ || true
