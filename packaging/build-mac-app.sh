#!/usr/bin/env bash
# Assemble JourneyForgeLocal.app (unsigned wrapper) and zip it.
# Dependency-light: needs only bash + tar + node (no rsync, no zip binary), so it
# runs on a minimal node:20 CI image without apt. The .app runs project code
# from the bundle and keeps venv/data/config in ~/Library/Application Support.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-/tmp}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"            # absolute, safe even if under REPO
ZIP="$OUT/JourneyForgeLocal-mac.zip"

# Assemble in a temp dir OUTSIDE the repo. Building inside the repo would make
# the copy pull the half-built out/ into the bundle recursively (the old CI bug).
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
APP="$BUILD/JourneyForgeLocal.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/project"

cp "$REPO/packaging/mac-app/Info.plist" "$APP/Contents/Info.plist"
cp "$REPO/packaging/mac-app/launcher.sh" "$APP/Contents/MacOS/JourneyForgeLocal"
chmod +x "$APP/Contents/MacOS/JourneyForgeLocal"

# Copy the project into the bundle (prebuilt extension included; junk excluded).
# tar instead of rsync so no extra package is required.
tar -C "$REPO" \
  --anchored \
  --exclude='./.git' --exclude='./data' --exclude='./out' \
  --exclude='./.env.local' --exclude='./.venv' --exclude='./.pnpm-store' \
  --no-anchored \
  --exclude='node_modules' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.wxt' --exclude='.output' \
  -cf - . | ( cd "$APP/Contents/Resources/project" && tar -xf - )

# Zip with a Node-only archiver that preserves unix perms (launcher exec bit).
rm -f "$ZIP"
node "$REPO/packaging/zip.mjs" "$BUILD" "$ZIP"
echo "zip: $ZIP"
