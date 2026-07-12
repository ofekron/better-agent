#!/bin/bash
# Build the Better Agent macOS desktop app:
#   frontend build → PyInstaller → ad-hoc code-sign → .dmg
#
# Ad-hoc signing (`codesign --sign -`) is free and is enough to RUN the
# app on Apple Silicon and on the machine that built it. A downloaded
# copy would still hit Gatekeeper — for distribution to other people,
# add Developer ID signing + notarization (needs an Apple Developer
# account); that is intentionally out of scope here.

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"
VENV="$REPO/backend/.venv"

echo "==> Building the frontend (npm run build)"
( cd "$REPO/frontend" && npm run build )

echo "==> Installing build dependencies into the backend venv"
( cd "$REPO/backend" && "$VENV/bin/pip" install -q -r requirements.txt )
"$VENV/bin/pip" install -q pyinstaller pywebview tufup

VERSION="$(cd "$REPO" && "$VENV/bin/python" -c 'import sys; sys.path.insert(0, "desktop"); from _version import __version__; print(__version__)')"
BA_HOME="$(cd "$REPO" && "$VENV/bin/python" -c 'import sys; sys.path.insert(0, "backend"); from paths import ba_home; print(ba_home())')"
PRIMARY_UPDATE_URL="${BA_DESKTOP_UPDATE_URL:-$(cd "$REPO" && "$VENV/bin/python" -c 'import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); print(f"http://{s.getsockname()[0]}:8000/api/desktop/updates"); s.close()')}"
PRIMARY_UPDATE_URL="${PRIMARY_UPDATE_URL%/}"
UPDATE_ROOT="$BA_HOME/desktop/updates"
UPDATE_REPO="$UPDATE_ROOT/repository"
UPDATE_KEYS="$UPDATE_ROOT/keystore"
DOWNLOADS="$BA_HOME/desktop/downloads"

if [ ! -f "$UPDATE_REPO/metadata/root.json" ]; then
  echo "==> Initializing desktop update repository"
  "$VENV/bin/python" "$DIR/release.py" init "$UPDATE_REPO" "$UPDATE_KEYS"
fi

echo "==> Exporting updater trust root"
"$VENV/bin/python" "$DIR/release.py" export-root "$UPDATE_REPO" "$UPDATE_KEYS" "$DIR/tufup_root.json"
printf "%s\n" "$PRIMARY_UPDATE_URL" > "$DIR/update_url.txt"

echo "==> Running PyInstaller"
rm -rf "$DIR/build/BetterAgent"
( cd "$DIR" && "$VENV/bin/pyinstaller" --noconfirm BetterAgent.spec )

APP="$DIR/dist/Better Agent.app"
if [ ! -d "$APP" ]; then
  echo "build failed: '$APP' was not produced" >&2
  exit 1
fi

echo "==> Ad-hoc code-signing the app"
codesign --force --deep --sign - "$APP"

echo "==> Building the .dmg"
DMG="$DIR/dist/BetterAgent.dmg"
rm -f "$DMG"
hdiutil create -volname "Better Agent" -srcfolder "$APP" \
  -ov -format UDZO "$DMG"

echo "==> Publishing desktop update + primary-host download"
"$VENV/bin/python" "$DIR/release.py" publish "$UPDATE_REPO" "$UPDATE_KEYS" "$DIR/dist/Better Agent" "$VERSION"
mkdir -p "$DOWNLOADS"
cp "$DMG" "$DOWNLOADS/BetterAgent.dmg"

echo "==> Done — $DMG"
