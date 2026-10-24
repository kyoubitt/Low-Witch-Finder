<p align="center"><img src="assets/logo.png" width="420" alt="Low Witch Finder"></p>

# Low Witch Finder

A full-screen terminal app for Minecraft witch huts: a biome map of any seed, and a finder for huts whose floor is very low (y -50 to -59).
Python 3.8+ only, no installs. Linux, Windows and macOS.


## Install

**Linux and macOS** (needs git, python3 and a C compiler; builds the tools in a minute or two, no sudo):

    curl -fsSL https://raw.githubusercontent.com/kyoubitt/Low-Witch-Finder/master/install.sh | bash

Then run `low-witch-finder`. On macOS with Homebrew you can use the tap instead:

    brew install kyoubitt/low-witch-finder/low-witch-finder

**Windows** (PowerShell; a ready-made `.exe`, no Python needed, no admin):

    irm https://raw.githubusercontent.com/kyoubitt/Low-Witch-Finder/master/install.ps1 | iex

Then open it from the Start menu, or type `LowWitchFinder` in a terminal. Or take the single `LowWitchFinder.exe` from the
[latest release](https://github.com/kyoubitt/Low-Witch-Finder/releases/latest) and double-click it: nothing else to install. The first
launch unpacks its tools into `%LOCALAPPDATA%\Low Witch Finder`; building it yourself is `tools/build_exe.sh`.

Run the same command again to update. From a clone: `python3 witchhut_studio.py` (Windows: `python witchhut_studio.py`).

## Huts found so far
<!-- huts:start -->
![huts](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fkyoubitt%2Flow-y-witch-huts%2Fmain%2Fdata%2Fsummary.json&query=%24.total&label=huts&color=2ea44f&style=flat-square) ![y -59](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fkyoubitt%2Flow-y-witch-huts%2Fmain%2Fdata%2Fsummary.json&query=%24.y59&label=y%20-59&color=e0457b&style=flat-square) ![y -58](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fkyoubitt%2Flow-y-witch-huts%2Fmain%2Fdata%2Fsummary.json&query=%24.y58&label=y%20-58&color=f0883e&style=flat-square) ![updated](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fkyoubitt%2Flow-y-witch-huts%2Fmain%2Fdata%2Fsummary.json&query=%24.updated&label=updated&color=6e7781&style=flat-square)

Every -55 or deeper hut found, each re-read by the game's own code: [the list](https://github.com/kyoubitt/low-y-witch-huts), [the ranking](https://github.com/kyoubitt/low-y-witch-huts/blob/main/RANKING.md).
<!-- huts:end -->

## Map
Any seed, 1.18 to 26.3, each version's own biome tree (the game's, dumped from its own code). **Nearest hut** (or `z`) zooms to the closest hut; `r` picks a random seed.
`w` (or **World**, or zooming out past the widest map) shows the whole world with no terrain: a grid and the known low huts of that seed. Click one to go there.
`u` switches the interface size (small, medium, large).

## Hunt
Two searches, one on each button:

- **Seed Check** (-50..-54) scans seeds you give it, or ones it makes up, for huts that deep, closest to 0,0 first. **or lower** (the default) means this deep or deeper; **exactly** means only that floor. -54 is always "or lower": natural terrain stops at -54, and a seed's -55 and deeper huts sit on a mineshaft room, which Seed Check reads, so it reports the true depth instead of missing them.
- **Deep Dive** (-55..-59) looks for NEW seeds. It does not scan a seed you give it: huts that deep are far too rare to turn up on one you already have.

Pick a **Mode** and press **Start**. It keeps running if you close the window. The wait shown is measured on this PC from the start of the run (pauses and gentle mode included).

| Mode | What it does | Floors |
|---|---|---|
| GPU full / gentle | NVIDIA card; gentle works about half the time | -50..-59 |
| CPU full / gentle | all cores / half the cores at low priority | -50..-54 |

-55 and deeper is the mineshaft search and needs an NVIDIA GPU (each candidate stands for 65,536 seeds). -50..-54 scans whole seeds: type seeds, a file of them, or leave blank and it makes them up; pick how much of the world.
Stop takes a few seconds. Finds go to `results/<depth>.txt`.

## Setup
- Map and CPU scanner: `./build.sh map` / `./build.sh` (any C compiler), or `build.ps1` on Windows (the `.exe` files are shipped).
- GPU search: `./build.sh gpu` needs NVIDIA's CUDA toolkit (about 5 GB). Windows ships the mineshaft engine prebuilt; the GPU scanner needs a CUDA build (`engine/gpu/low54`, see docs/).
- -55 and deeper also need Minecraft's files (about 90 MB, downloaded from Mojang and checked) and Java 21; one button fetches both, and a small Java (Temurin, checked against its publisher's SHA-256) is fetched if the computer has none.
- Close Minecraft before GPU runs: the app refuses to start one beside it.

## Tests
`tools/test_app.py`, `tools/test_hunt.py` (stub GPU engine), `tools/test_detached.py`, `tools/test_accuracy.py` (the map against the game's own code; needs Java and the Minecraft files), `python3 hunt.py selftest`.
Third-party code: THIRD_PARTY.md.
