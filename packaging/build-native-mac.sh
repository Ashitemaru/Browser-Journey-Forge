#!/usr/bin/env bash
# Build the native macOS .app (Tauri shell + frozen Python sidecar).
# RUN THIS ON A MAC — PyInstaller and Tauri build native artifacts and cannot be
# cross-built from Linux. See docs/native-app-build.md for prerequisites.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
PYBIN="${PYTHON:-python3}"

echo "[1/4] Building the extension…"
( cd extension && npx --yes pnpm@9 install --no-frozen-lockfile && npx --yes pnpm@9 run build )

echo "[2/4] Python build venv + PyInstaller…"
"$PYBIN" -m venv .build-venv
# shellcheck disable=SC1091
source .build-venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt pyinstaller

echo "[3/4] Freezing the sidecar (jfl-server)…"
pyinstaller --noconfirm --clean --onefile --name jfl-server \
  --paths "$REPO" --paths "$REPO/server" \
  --add-data "app/dist:app/dist" \
  --add-data "extension/dist/chrome-mv3:extension/dist/chrome-mv3" \
  --collect-submodules harness \
  --collect-submodules uvicorn \
  --collect-submodules fastapi \
  --hidden-import server \
  packaging/sidecar_main.py
TRIPLE="$(rustc -Vv | sed -n 's/^host: //p')"
mkdir -p desktop/src-tauri/binaries
cp "dist/jfl-server" "desktop/src-tauri/binaries/jfl-server-$TRIPLE"
echo "    sidecar → desktop/src-tauri/binaries/jfl-server-$TRIPLE"

echo "[4/4] Building the Tauri app…"
( cd desktop && npm install && npm run tauri build )

echo
echo "Done. The .app is under:"
echo "  desktop/src-tauri/target/release/bundle/macos/Journey-Forge Local.app"
