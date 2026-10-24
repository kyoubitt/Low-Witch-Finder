# Builds the tools into bin/ (the Windows download already has them as .exe files; this is for rebuilding).
#   ./build.ps1          the map engine and the finder's CPU tools; needs gcc or clang on PATH (cl works for the C tools)
# The GPU engine (hut_lookup_v4.exe, gpu_info.exe) ships prebuilt for RTX 40 and 50 cards. Older cards need
# a rebuild with CUDA and Visual Studio: see engine/gpu and the project's build notes.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force bin | Out-Null
$src = "engine/tools/lysh/src"
$ms = "engine/tools/mineshaft"
$lib = Get-ChildItem "$src/*.c" | Where-Object { $_.Name -ne "main.c" } | ForEach-Object { $_.FullName }
$cc = $null
foreach ($c in @("gcc", "clang")) { if (Get-Command $c -ErrorAction SilentlyContinue) { $cc = $c; break } }
$cxx = $null
foreach ($c in @("g++", "clang++")) { if (Get-Command $c -ErrorAction SilentlyContinue) { $cxx = $c; break } }
if (-not $cc) {
    Write-Host "No C compiler found. The easiest: winget install BrechtSanders.WinLibs.POSIX.UCRT  (gives gcc and g++), then open a new terminal."
    exit 1
}
# -ffp-contract=off is required: the biome and terrain maths must match the game bit for bit
$flags = @("-O2", "-std=gnu11", "-ffp-contract=off", "-w", "-I$src", "-I$ms")
function Build($name, $sources) {
    Write-Host "  bin/$name.exe"
    & $cc @flags @sources @lib -lm -o "bin/$name.exe"
    if ($LASTEXITCODE -ne 0) { Write-Host "build failed: $name"; exit 1 }
}
Write-Host "building with $cc ..."
Build "biome_map" @("$ms/biome_map.c")
Build "lysh" @("$src/main.c")
foreach ($t in @("biome_at", "msroom", "skyscan", "terrain_dump")) { Build $t @("$ms/$t.c") }
if ($cxx) {
    Write-Host "  bin/mstree.exe"
    & $cxx -O2 -std=c++17 -w "-Iengine/tools/mstree" "engine/tools/mstree/mstree.cpp" -o "bin/mstree.exe"
    if ($LASTEXITCODE -ne 0) { Write-Host "build failed: mstree"; exit 1 }
} else {
    Write-Host "No C++ compiler (needed for mstree). gcc from WinLibs includes g++."
    exit 1
}
Write-Host "done"
