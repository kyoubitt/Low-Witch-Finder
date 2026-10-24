#!/usr/bin/env bash
# Builds the tools into bin/.   ./build.sh          the map engine and the finder's CPU tools (a C and C++ compiler is enough)
#                               ./build.sh gpu      also the GPU engine (needs an NVIDIA GPU's toolkit: nvcc)
#                               ./build.sh map      only the map engine
#                               ./build.sh cpu      the map engine and the CPU tools, no GPU engine
set -euo pipefail
cd "$(dirname "$0")"
WHAT="${1:-all}"
CC="${CC:-$(command -v cc || command -v gcc || command -v clang || true)}"
CXX="${CXX:-$(command -v c++ || command -v g++ || command -v clang++ || true)}"
hint() {
    if command -v apt-get >/dev/null; then echo "  sudo apt install $1"
    elif command -v dnf >/dev/null; then echo "  sudo dnf install $2"
    elif command -v pacman >/dev/null; then echo "  sudo pacman -S $3"
    elif [[ "$(uname -s)" == Darwin ]]; then echo "  xcode-select --install"
    else echo "  (your package manager's gcc and g++, or clang)"; fi
}
[[ -n "$CC" ]] || { echo "no C compiler found. Install one:"; hint build-essential gcc base-devel; exit 1; }
SRC=engine/tools/lysh/src
MS=engine/tools/mineshaft
OBJ=build/obj
mkdir -p bin "$OBJ"
# -ffp-contract=off is required: the biome and terrain maths must match the game bit for bit (no fused multiply-add)
FLAGS=(-O3 -std=gnu11 -ffp-contract=off -I"$SRC" -w)
# tuned to this machine's CPU when the compiler allows it: about 15% faster, byte-for-byte the same answers (checked on five seeds)
if echo 'int main(void){return 0;}' | "$CC" -march=native -x c - -o /dev/null 2>/dev/null; then FLAGS+=(-march=native); fi
JOBS="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"

echo "compiling the world-generation library with $CC ..."
ls $SRC/*.c | grep -v '/main\.c$' | xargs -P "$JOBS" -I{} sh -c '
    o="'"$OBJ"'/$(basename {} .c).o"
    [ "$o" -nt {} ] || "'"$CC"'" '"${FLAGS[*]}"' -c {} -o "$o"'
LIBOBJ=$(ls $OBJ/*.o)

link() {   # link TOOL SOURCE...
    local tool="$1"; shift
    echo "  bin/$tool"
    "$CC" "${FLAGS[@]}" -I"$MS" "$@" $LIBOBJ -lm -lpthread -o "bin/$tool"
}
link biome_map "$MS/biome_map.c"
if [[ "$WHAT" != map ]]; then
    link lysh "$SRC/main.c"
    for t in biome_at msroom skyscan terrain_dump; do link "$t" "$MS/$t.c"; done
    if [[ -n "$CXX" ]]; then
        echo "  bin/mstree"
        "$CXX" -O2 -std=c++17 -w -Iengine/tools/mstree engine/tools/mstree/mstree.cpp -o bin/mstree
    else
        echo "no C++ compiler (needed for mstree):"; hint build-essential gcc-c++ base-devel; exit 1
    fi
fi
if [[ "$WHAT" == gpu || "$WHAT" == all && -n "$(command -v nvcc || ls /opt/cuda/bin/nvcc 2>/dev/null)" ]]; then
    NVCC="$(command -v nvcc || echo /opt/cuda/bin/nvcc)"
    if [[ ! -x "$NVCC" && ! -f "$NVCC" ]]; then echo "nvcc (the CUDA toolkit) isn't installed, so the GPU engine can't be built here."; exit 1; fi
    echo "building the GPU engine with nvcc for this machine's GPU (about 3 minutes) ..."
    ( cd engine/gpu
      "$NVCC" -O3 -arch=native -DPHASE1_FP32_CORE -DDEV_Y0_ASSUME_NO_WRAP -diag-suppress 20011,20013,20014,20015,177 \
          -o ../../bin/hut_lookup_v4 hut_lookup_v4_source.cu
      "$NVCC" -O3 -arch=native -o ../../bin/gpu_info gpu_info.cu )
    echo "building the GPU scanner (the -50 to -54 search; several minutes) ..."
    ( cd engine/gpu/low54
      "$NVCC" -O3 -arch=native -DPHASE1_FP32_CORE -DDEV_Y0_ASSUME_NO_WRAP -diag-suppress 20011,20013,20014,20015,177 \
          -o ../../../bin/phase1_scan phase1_scan.cu )
fi
echo "done: $(ls bin | tr '\n' ' ')"
