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

Then open it from the Start menu, or type `LowWitchFinder` in a terminal. Or take `LowWitchFinder-windows.zip` from the
[latest release](https://github.com/kyoubitt/Low-Witch-Finder/releases/latest) and run `LowWitchFinder.exe`.

Run the same command again to update. From a clone: `python3 witchhut_studio.py` (Windows: `python witchhut_studio.py`).

## Map
Any seed, 1.18 to 26.3, each version's own biome tree (the game's, dumped from its own code). **Nearest hut** (or `z`) zooms to the closest hut; `r` picks a random seed.

## Hunt
Pick **Floor y** (-50..-59), a **Mode** and press **Start**. It keeps running if you close the window.

| Mode | What it does | Floors |
|---|---|---|
| GPU full / gentle | NVIDIA card; gentle works about half the time | -50..-59 |
| CPU full / gentle | all cores / half the cores at low priority | -50..-54 |

-55 and deeper is the mineshaft search and needs an NVIDIA GPU (each candidate stands for 65,536 seeds). -50..-54 scans whole seeds: type seeds, a file of them, or leave blank and it makes them up; pick how much of the world.
Stop takes a few seconds. Finds go to `results/<depth>.txt`.

## Setup
- Map and CPU scanner: `./build.sh map` / `./build.sh` (any C compiler), or `build.ps1` on Windows (the `.exe` files are shipped).
- GPU search: `./build.sh gpu` needs NVIDIA's CUDA toolkit (about 5 GB). Windows ships the mineshaft engine prebuilt; the GPU scanner needs a CUDA build (`engine/gpu/low54`, see docs/).
- -55 and deeper also need Java 21 and Minecraft's files (about 90 MB, downloaded from Mojang and checked; the app offers a button).
- Close Minecraft before GPU runs: the app refuses to start one beside it.

## Tests
`tools/test_app.py`, `tools/test_hunt.py` (stub GPU engine), `tools/test_detached.py`, `tools/test_accuracy.py` (the map against the game's own code; needs Java and the Minecraft files), `python3 hunt.py selftest`.
Third-party code: THIRD_PARTY.md.
