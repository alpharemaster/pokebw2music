#!/usr/bin/env bash
# Builds NDS Synth's Windows installer (build/nds_synth/NDSSynth-<version>-Setup.exe) from the VST3 that
# build_windows.sh made, with NSIS's makensis, fetched at NSIS_VERSION into build/nds_synth/nsis.
#
#     tools/nds_synth/make_installer.sh
set -euo pipefail

NSIS_VERSION=3.10
VERSION=1.0.0
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=$ROOT/build/nds_synth

if [ ! -d "$OUT/NDSSynth.vst3" ]; then
    echo "error: no build/nds_synth/NDSSynth.vst3: run tools/nds_synth/build_windows.sh first" >&2
    exit 1
fi
if [ ! -f "$OUT/nsis-$NSIS_VERSION.zip" ]; then
    curl -sSL -o "$OUT/nsis-$NSIS_VERSION.zip" \
        "https://downloads.sourceforge.net/project/nsis/NSIS%203/$NSIS_VERSION/nsis-$NSIS_VERSION.zip"
fi

# makensis is a Windows program: it runs on a copy in %LOCALAPPDATA%
LOCAL=$(cd /mnt/c && cmd.exe /c "echo %LOCALAPPDATA%" 2>/dev/null | tr -d '\r')
WIN_DIR="$LOCAL\\pokebw2\\nds_synth_installer"
DIR=$(wslpath -u "$WIN_DIR")
rm -rf "$DIR"
mkdir -p "$DIR"
python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" \
    "$OUT/nsis-$NSIS_VERSION.zip" "$DIR"
cp -r "$OUT/NDSSynth.vst3" "$DIR/"
cp "$ROOT/tools/nds_synth/installer.nsi" "$DIR/"
(cd "$DIR" && "./nsis-$NSIS_VERSION/makensis.exe" -V2 "-DVERSION=$VERSION" installer.nsi)
cp "$DIR/NDSSynth-$VERSION-Setup.exe" "$OUT/"
echo "Built $OUT/NDSSynth-$VERSION-Setup.exe"
