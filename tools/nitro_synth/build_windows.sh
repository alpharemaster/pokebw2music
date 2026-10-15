#!/usr/bin/env bash
# Builds Nitro Synth's Windows VST3 from WSL with Visual Studio's compiler (the "Desktop development with C++"
# workload), and with --install copies it to C:\Program Files\Common Files\VST3.
#
#     tools/nitro_synth/build_windows.sh [--install]
#
# DPF is fetched at DPF_COMMIT into build/nitro_synth/dpf. MSVC can't build from a \\wsl$ path, so the sources are
# copied to %LOCALAPPDATA%\pokebw2\nitro_synth and built there; the bundle comes back to build/nitro_synth/.
set -euo pipefail

DPF_COMMIT=4238e1c7f0351bbe488d79f0899c540543ac7583
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=$ROOT/build/nitro_synth
VSWHERE="/mnt/c/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe"

if [ ! -d "$OUT/dpf/.git" ]; then
    git clone -q https://github.com/DISTRHO/DPF.git "$OUT/dpf"
fi
git -C "$OUT/dpf" fetch -q origin "$DPF_COMMIT" 2>/dev/null || true
git -C "$OUT/dpf" checkout -q "$DPF_COMMIT"
git -C "$OUT/dpf" submodule update -q --init --recursive

if [ ! -x "$VSWHERE" ]; then
    echo "error: Visual Studio isn't installed (no vswhere.exe)" >&2
    exit 1
fi
VS=$(cd /mnt/c && "$VSWHERE" -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 \
     -property installationPath | tr -d '\r')
if [ -z "$VS" ]; then
    echo "error: Visual Studio has no C++ compiler: add the \"Desktop development with C++\" workload" >&2
    exit 1
fi

LOCAL=$(cd /mnt/c && cmd.exe /c "echo %LOCALAPPDATA%" 2>/dev/null | tr -d '\r')
WIN_DIR="$LOCAL\\pokebw2\\nitro_synth"
DIR=$(wslpath -u "$WIN_DIR")
mkdir -p "$DIR"
rm -rf "$DIR/src" "$DIR/dpf"
cp -r "$ROOT/tools/nitro_synth" "$DIR/src"
cp -r "$OUT/dpf" "$DIR/dpf"
rm -rf "$DIR/dpf/.git"
python3 "$ROOT/tools/nitro_synth/patch_dpf.py" "$DIR/dpf"  # VST3 program changes

cat > "$DIR/build.bat" <<EOF
@echo off
call "$VS\\VC\\Auxiliary\\Build\\vcvars64.bat" >nul || exit /b 1
cmake -G Ninja -S "%~dp0src" -B "%~dp0build" -DCMAKE_BUILD_TYPE=Release -DDPF_DIR="%~dp0dpf" || exit /b 1
cmake --build "%~dp0build" || exit /b 1
EOF
(cd /mnt/c && cmd.exe /c "$WIN_DIR\\build.bat")

BUNDLE=$(find "$DIR/build/bin" -maxdepth 1 -name '*.vst3' | head -1)
if [ -z "$BUNDLE" ]; then
    echo "error: the build made no .vst3 bundle" >&2
    exit 1
fi
rm -rf "$OUT/NitroSynth.vst3"
cp -r "$BUNDLE" "$OUT/NitroSynth.vst3"
echo "Built $OUT/NitroSynth.vst3"
echo "  Windows path: $(wslpath -w "$OUT/NitroSynth.vst3")"

if [ "${1:-}" = "--install" ]; then
    TARGET="/mnt/c/Program Files/Common Files/VST3"
    if cp -r "$BUNDLE" "$TARGET/" 2>/dev/null; then
        echo "Installed to C:\\Program Files\\Common Files\\VST3\\$(basename "$BUNDLE")"
    else
        echo "Couldn't write to C:\\Program Files\\Common Files\\VST3 (it needs administrator rights): copy" \
             "$(wslpath -w "$BUNDLE") there by hand, or add its folder to FL Studio's plugin search paths" >&2
    fi
fi
