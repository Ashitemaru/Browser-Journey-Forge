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

# Stamp the build commit so the panel can show exactly which build is running
# (kills the "did my changes ship?" guesswork after a CI download).
BUILD_SHA="${CI_COMMIT_SHORT_SHA:-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)}"
printf '{"sha":"%s"}\n' "$BUILD_SHA" > app/dist/build.json
echo "    build stamp: $BUILD_SHA"

echo "[3/4] Freezing the sidecar (jfl-server)…"
pyinstaller --noconfirm --clean --onefile --name jfl-server \
  --paths "$REPO" --paths "$REPO/server" \
  --add-data "app/dist:app/dist" \
  --add-data "extension/dist/chrome-mv3:extension/dist/chrome-mv3" \
  --collect-submodules harness \
  --collect-submodules uvicorn \
  --collect-submodules fastapi \
  --hidden-import server \
  --hidden-import skill_mcp \
  packaging/sidecar_main.py
TRIPLE="$(rustc -Vv | sed -n 's/^host: //p')"
mkdir -p desktop/src-tauri/binaries
cp "dist/jfl-server" "desktop/src-tauri/binaries/jfl-server-$TRIPLE"
echo "    sidecar → desktop/src-tauri/binaries/jfl-server-$TRIPLE"

echo "[4/4] Building the Tauri app…"
( cd desktop && npm install && npm run tauri build )

APP="desktop/src-tauri/target/release/bundle/macos/Journey-Forge Local.app"
# Re-sign the PyInstaller sidecar with library-validation disabled, otherwise
# its embedded Python.framework (a different Team ID) fails to load under the
# hardened runtime. Then re-seal the bundle over the new sidecar signature.
SIDECAR="$(/usr/bin/find "$APP/Contents" -type f -name 'jfl-server*' | head -1)"
if [ -n "$SIDECAR" ]; then
  echo "Re-signing sidecar with library-validation disabled: $SIDECAR"
  codesign --force --options runtime \
    --entitlements packaging/sidecar.entitlements --sign - "$SIDECAR"
  codesign --force --sign - "$APP"
  codesign --verify --deep --strict "$APP" && echo "codesign verify OK" || echo "WARN: codesign verify failed"
fi

# Package the (re-signed) .app into a .dmg so the download is a single
# double-clickable installer — no nested zip-inside-zip. Built AFTER re-signing
# so the app inside the image carries the correct signature.
DMG="$(dirname "$APP")/JourneyForgeLocal-native-mac.dmg"
echo "[5/5] Creating DMG → $DMG"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"      # drag-to-install affordance
rm -f "$DMG"
hdiutil create -volname "Journey-Forge Local" -srcfolder "$STAGE" \
  -ov -format UDZO "$DMG"
rm -rf "$STAGE"

echo
echo "Done."
echo "  .app: $APP"
echo "  .dmg: $DMG"
