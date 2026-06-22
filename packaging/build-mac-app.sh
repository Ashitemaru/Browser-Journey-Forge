#!/usr/bin/env bash
# Assemble JourneyForgeLocal.app (unsigned wrapper) and zip it.
# Pure file ops — runs on any OS. The .app runs project code from the bundle
# and keeps venv/data/config in ~/Library/Application Support/JourneyForgeLocal.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-/tmp}"
APP="$OUT/JourneyForgeLocal.app"
ZIP="$OUT/JourneyForgeLocal-mac.zip"

rm -rf "$APP" "$ZIP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/project"

cp "$REPO/packaging/mac-app/Info.plist" "$APP/Contents/Info.plist"
cp "$REPO/packaging/mac-app/launcher.sh" "$APP/Contents/MacOS/JourneyForgeLocal"
chmod +x "$APP/Contents/MacOS/JourneyForgeLocal"

# Copy the project into the bundle (prebuilt extension included; junk excluded).
rsync -a \
  --exclude='.git' --exclude='data' --exclude='.env.local' --exclude='.venv' \
  --exclude='node_modules' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='extension/.wxt' --exclude='extension/.output' \
  "$REPO"/ "$APP/Contents/Resources/project/"

# Zip with stored unix perms so the launcher stays executable on macOS.
( cd "$OUT" && zip -q -9 -ry "$ZIP" "JourneyForgeLocal.app" )
echo "built: $APP"
echo "zip:   $ZIP"
