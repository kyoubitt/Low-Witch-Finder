#!/usr/bin/env bash
# Builds the one-file LowWitchFinder.exe on Linux, with PyInstaller under Wine (set up once by ~/.cache/lwf-wine/setup.sh).
# The Windows tools, the layout data, the oracle's classes and the logo go in as one archive (payload.zip); the exe unpacks
# it into %LOCALAPPDATA%\Low Witch Finder on the first launch (see home.py). Needs bin/*.exe built first (tools/zig_windows.sh).
set -e
cd "$(dirname "$0")/.."
SRC=$PWD
W=${LWF_WINE:-$HOME/.cache/lwf-wine}
export WINEPREFIX=$W/prefix WINEDEBUG=-all WINEARCH=win64
[ -f "$W/py/python.exe" ] || { echo "run $W/setup.sh first (Wine, embeddable Python, PyInstaller)"; exit 1; }
ls bin/*.exe >/dev/null
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/bin" "$stage/oracle" "$stage/assets"
cp bin/*.exe "$stage/bin/"
cp -r data "$stage/data"
cp -r oracle/classes "$stage/oracle/classes"
cp assets/logo.png "$stage/assets/"
cp README.md THIRD_PARTY.md "$stage/" 2>/dev/null || true
rm -f "$W/payload.zip"
(cd "$stage" && zip -qr9X "$W/payload.zip" .)
cd "$W/out"
nice wine ../py/python.exe -m PyInstaller --noconfirm --clean --onefile --console --name LowWitchFinder \
  --icon "Z:$(echo "$SRC/assets/icon.ico" | tr / '\\')" \
  --add-data "Z:$(echo "$W/payload.zip" | tr / '\\');." \
  --paths "Z:$(echo "$SRC" | tr / '\\')" --paths "Z:$(echo "$SRC/tools" | tr / '\\')" --hidden-import export_catalog \
  "Z:$(echo "$SRC/witchhut_studio.py" | tr / '\\')" 2>&1 | tail -5
ls -la dist/LowWitchFinder.exe
cp dist/LowWitchFinder.exe "$SRC/dist-LowWitchFinder.exe"
