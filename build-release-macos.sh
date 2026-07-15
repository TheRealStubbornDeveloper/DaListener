#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run inside the future macOS 13+ VM. PyInstaller cannot build macOS apps on Windows."
  exit 1
fi

export MACOSX_DEPLOYMENT_TARGET=13.0
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ ! -x ".venv-macos/bin/python" ]]; then
  "$PYTHON_BIN" -m venv .venv-macos
fi
source .venv-macos/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[build,test,local-fallback]"

npm --prefix frontend install
npm --prefix frontend run build
python -m pytest -q
python -m PyInstaller --noconfirm --clean packaging/dalistener-macos.spec

APP="$ROOT/dist/DaListener.app"
DMG="$ROOT/dist/DaListener-0.3.0-alpha.2-macos-universal.dmg"

while IFS= read -r binary; do
  if file "$binary" | grep -q "Mach-O"; then
    archs="$(lipo -archs "$binary")"
    [[ "$archs" == *"arm64"* && "$archs" == *"x86_64"* ]] || {
      echo "Missing a universal architecture slice: $binary ($archs)"
      exit 1
    }
  fi
done < <(find "$APP" -type f)

if [[ -n "${APPLE_CODESIGN_IDENTITY:-}" ]]; then
  codesign --force --deep --options runtime --timestamp --sign "$APPLE_CODESIGN_IDENTITY" "$APP"
else
  codesign --force --deep --sign - "$APP"
  echo "Created an ad-hoc signed beta app. Gatekeeper will require Open Anyway after download."
fi

STAGING="$ROOT/build/dmg-staging"
rm -rf "$STAGING" "$DMG"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/DaListener.app"
ln -s /Applications "$STAGING/Applications"
hdiutil create -volname DaListener -srcfolder "$STAGING" -ov -format UDZO "$DMG"

if [[ -n "${APPLE_NOTARY_KEY:-}" && -n "${APPLE_NOTARY_KEY_ID:-}" && -n "${APPLE_NOTARY_ISSUER_ID:-}" ]]; then
  xcrun notarytool submit "$DMG" --key "$APPLE_NOTARY_KEY" --key-id "$APPLE_NOTARY_KEY_ID" --issuer "$APPLE_NOTARY_ISSUER_ID" --wait
  xcrun stapler staple "$DMG"
fi

shasum -a 256 "$DMG" > "$DMG.sha256"
echo "Created $DMG"
