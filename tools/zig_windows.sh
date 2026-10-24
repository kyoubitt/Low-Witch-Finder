#!/usr/bin/env bash
# Cross-compiles the Windows .exe files in bin/ from Linux with zig. For whoever maintains the project: people who use
# the app never need this (the .exe files are shipped, and build.ps1 rebuilds them on Windows itself).
set -euo pipefail
cd "$(dirname "$0")/.."
SRC=engine/tools/lysh/src
MS=engine/tools/mineshaft
OBJ=build/obj-win
T=(-target x86_64-windows-gnu)
FLAGS=("${T[@]}" -O2 -std=gnu11 -ffp-contract=off -w -I"$SRC" -I"$MS")
mkdir -p "$OBJ"
ls $SRC/*.c | grep -v '/main\.c$' | xargs -P "$(nproc)" -I{} sh -c '
    o="'"$OBJ"'/$(basename {} .c).o"
    [ "$o" -nt {} ] || zig cc '"${FLAGS[*]}"' -c {} -o "$o"'
LIB=$(ls $OBJ/*.o)
zig cc "${FLAGS[@]}" "$SRC/main.c" $LIB -lm -o bin/lysh.exe
for t in biome_map biome_at msroom skyscan terrain_dump; do zig cc "${FLAGS[@]}" "$MS/$t.c" $LIB -lm -o "bin/$t.exe"; done
zig c++ "${T[@]}" -O2 -std=c++17 -w engine/tools/mstree/mstree.cpp -o bin/mstree.exe
rm -f bin/*.pdb
echo "built: $(cd bin && ls *.exe | tr '\n' ' ')"
