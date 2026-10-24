#!/usr/bin/env bash
# Low Witch Finder installer for Linux and macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/kyoubitt/Low-Witch-Finder/master/install.sh | bash
#
# Run it again any time to update. To remove it:
#
#   curl -fsSL https://raw.githubusercontent.com/kyoubitt/Low-Witch-Finder/master/install.sh | bash -s -- --uninstall
#
# No sudo: everything goes in your home folder. What it does:
#   1. checks for git, python3 and a C compiler (and tells you the command to get any that's missing)
#   2. downloads Low Witch Finder to ~/.local/share/low-witch-finder
#   3. builds the tools (a minute or two); the NVIDIA GPU engine too if the CUDA toolkit (nvcc) is installed
#   4. puts a `low-witch-finder` command in ~/.local/bin
#
# Overrides (environment): LWF_HOME (install folder), LWF_BIN (where the command goes), LWF_CPU_ONLY=1 (skip the GPU engine).
set -euo pipefail

REPO="${LWF_REPO:-https://github.com/kyoubitt/Low-Witch-Finder.git}"
BRANCH="${LWF_BRANCH:-master}"
DEST="${LWF_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/low-witch-finder}"
BIN="${LWF_BIN:-$HOME/.local/bin}"

if [[ -t 1 ]]; then
    red()  { printf '\033[31m%s\033[0m\n' "$*"; }
    grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
    cyan() { printf '\033[36m%s\033[0m\n' "$*"; }
    dim()  { printf '\033[90m%s\033[0m\n' "$*"; }
else
    red() { echo "$*"; }; grn() { echo "$*"; }; cyan() { echo "$*"; }; dim() { echo "$*"; }
fi
die() { red "$*"; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
    cyan "removing Low Witch Finder"
    [[ -d "$DEST" ]] && { rm -rf "$DEST"; dim "removed $DEST (its hunts and finds went with it)"; }
    [[ -f "$BIN/low-witch-finder" ]] && { rm -f "$BIN/low-witch-finder"; dim "removed $BIN/low-witch-finder"; }
    grn "done"
    exit 0
fi

os="$(uname -s)"
hint_pkg() {
    if   [[ "$os" == Darwin ]];            then echo "xcode-select --install"
    elif command -v apt-get >/dev/null;    then echo "sudo apt install $1"
    elif command -v dnf >/dev/null;        then echo "sudo dnf install $2"
    elif command -v pacman >/dev/null;     then echo "sudo pacman -S $3"
    elif command -v zypper >/dev/null;     then echo "sudo zypper install $2"
    else echo "install $2 with your package manager"; fi
}
missing=0
need() {       # need <command> <apt pkgs> <dnf pkgs> <pacman pkgs>
    command -v "$1" >/dev/null && return
    red "missing: $1   ->   $(hint_pkg "$2" "$3" "$4")"
    missing=1
}
need git     git       git     git
need python3 python3   python3 python
if ! command -v gcc >/dev/null && ! command -v clang >/dev/null && ! command -v cc >/dev/null; then
    red "missing: a C compiler   ->   $(hint_pkg "build-essential" "gcc" "base-devel")"; missing=1
fi
(( missing )) && die "Install those, then run this again."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "python3 is older than 3.8 - please update it."

cyan "Low Witch Finder -> $DEST"
if [[ -d "$DEST/.git" ]]; then
    dim "updating"
    git -C "$DEST" fetch --depth 1 -q "$REPO" "$BRANCH"
    git -C "$DEST" reset --hard -q FETCH_HEAD       # your hunts and finds (untracked) stay
else
    [[ -e "$DEST" ]] && die "$DEST exists and isn't a Low Witch Finder install - move it, or set LWF_HOME."
    mkdir -p "$(dirname "$DEST")"
    git clone --depth 1 -q -b "$BRANCH" "$REPO" "$DEST"
fi

cd "$DEST"
chmod +x build.sh
export CC="${CC:-$(command -v gcc || command -v clang || command -v cc)}"
what=cpu
if [[ -z "${LWF_CPU_ONLY:-}" && "$os" == Linux ]] && { command -v nvcc >/dev/null || [[ -x /opt/cuda/bin/nvcc ]]; }; then
    what=gpu
    cyan "building, with the NVIDIA GPU engine (several minutes)"
else
    cyan "building (a minute or two)"
fi
if ! ./build.sh "$what"; then
    [[ "$what" == gpu ]] || die "build failed - see the messages above."
    red "the GPU build failed (often a CUDA / compiler version mismatch)."
    cyan "building without the GPU engine instead (the CPU modes still work)"
    ./build.sh cpu || die "build failed - see the messages above."
fi

mkdir -p "$BIN"
cat > "$BIN/low-witch-finder" <<EOF
#!/usr/bin/env bash
exec python3 "$DEST/witchhut_studio.py" "\$@"
EOF
chmod +x "$BIN/low-witch-finder"

echo
grn "Low Witch Finder is installed."
case ":$PATH:" in
    *":$BIN:"*) echo "Run:  low-witch-finder" ;;
    *) echo "Add $BIN to your PATH, then run low-witch-finder:"
       echo "    echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.profile   # (or your shell's rc file), then open a new terminal"
       echo "or run it right now with:  $BIN/low-witch-finder" ;;
esac
[[ "$os" == Darwin ]] && dim "On a Mac the map and the CPU hunt (floors -50 to -54) work; the deeper floors need an NVIDIA GPU."
exit 0
