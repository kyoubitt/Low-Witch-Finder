#!/usr/bin/env python3
"""hunt.py - the low-Y witch hut finder, as one Python program (it replaces the project's bash and PowerShell scripts).

You ask for a floor level, "a hut at y -58 or lower", and a mode, "GPU full" or "CPU gentle". Two searches stand behind that:

  -55 .. -59  the SOLVER (NVIDIA GPU only). It works in chunks of work. For each chunk:
                1 solver    (GPU)  lists every place a chosen mineshaft layout lines up with a swamp hut: "matches"
                2 terrain   (GPU)  keeps the matches whose ground is a deep enough pit (each match stands for 65,536 seeds)
                3 finisher  (CPU)  rebuilds the mineshaft and the hut on exact terrain, rejects what can't reach the depth
                4 oracle    (Java) runs the game's own 1.21.4 code on the survivors and reads the true hut height
              The seeds are made by the search itself; it starts somewhere random and carries on where it stopped.
  -50 .. -54  the SCANNER (CPU or GPU). It takes one world seed at a time, checks every swamp-hut region of the area you pick
              (the whole +-30M world by default) and keeps the huts whose floor really is that low. Seeds you type, or a
              file of them, or ones it makes up as it goes.

Every hut found goes into results/<depth>.txt. A run keeps its state in runs/<TAG>/, so it resumes where it stopped; the app
watches those files. The hunt runs as its own detached process: closing the app doesn't stop it.

  python3 hunt.py run [--tag TAG]            run in the foreground (the app starts this, detached, for the settings in runs/TAG)
  python3 hunt.py scan --seed S [...]        scan one seed on the CPU and print the huts
  python3 hunt.py stop [TAG]                 stop within seconds                      status [TAG]   what a run has done
  python3 hunt.py preflight --level L --mode M    can this machine run it             tune           time this GPU once
  python3 hunt.py selftest [scan|solver|cpu]      reproduce the known reference huts
"""
import argparse
import bisect
import concurrent.futures
import json
import math
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))     # the driver runs as python -I, which leaves its own folder off the path
from home import ROOT  # noqa: E402  (the project folder, or where the .exe unpacked its tools)
sys.path.insert(0, ROOT)
import mcfiles  # noqa: E402
import witchhut_common as W  # noqa: E402

EXE = ".exe" if os.name == "nt" else ""
BIN = os.path.join(ROOT, "bin")
ORACLE = os.path.join(ROOT, "oracle")
DATA = os.path.join(ROOT, "data", "hunt")
RUNS = os.environ.get("WITCHHUT_RUNS") or os.path.join(ROOT, "runs")
RESULTS = os.environ.get("WITCHHUT_RESULTS") or os.path.join(ROOT, "results")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ---- what can be asked ------------------------------------------------------------------------------------------------------
LEVELS = tuple(range(-50, -60, -1))
SOLVER_LEVELS = (-55, -56, -57, -58, -59)
MODES = {
    "gpu": dict(label="GPU full", gpu=True, duty=1.0, share=1.0, blurb="NVIDIA card flat out: fastest, runs hot"),
    "gpu-gentle": dict(label="GPU gentle", gpu=True, duty=0.5, share=1.0, blurb="about half the time: slower, cooler, quieter"),
    "cpu": dict(label="CPU full", gpu=False, duty=1.0, share=1.0, blurb="all cores; no NVIDIA card or Java needed"),
    "cpu-gentle": dict(label="CPU gentle", gpu=False, duty=1.0, share=0.5, blurb="half the cores, low priority: keep working"),
}
AREAS = ((5000, "5,000 blocks"), (20000, "20,000 blocks"), (100000, "100,000 blocks"), (0, "the whole world"))
# Matches per find, measured on an RTX 4090 over ~135M matches (-58) and an ~5 hour run (-59). The other levels have no
# measurement yet, so they get no estimate rather than an invented one.
PER_FIND = {-58: 6.4e6, -59: 7.0e7}
MEASURE_CHUNKS, MEASURE_SECONDS = 3, 90      # a speed is only quoted after this many chunks and seconds of this run
SELF_FINDS = 3                               # finds on this machine before its own matches-per-find replaces the measured figure
SEEDS_PER_SPOT = 65536                      # each solver spot stands for every choice of the seed's top 16 bits
SCRAMBLE = 6364136223846793005              # the project's seed scrambler: seed = index * SCRAMBLE (mod 2^64)
WORLD = (-58594, 58594)                     # hut regions of the +-30M world, as [low, high)
HOT_PAUSE, HOT_RESUME = 88, 82              # GPU degrees C: no new GPU work above the first, carry on below the second

# The shipped defaults of the project's config.conf (engine v4: the exact rollback engine, one terrain process per chunk).
DEFAULTS = dict(regime=22, chunk=64, batch=64, stop_on_find=1, min_depth=55, depth=2,
                ero_t1=-0.110, ero_c0=0.171, ero_t3=0.274, ero_c1=0.459,
                fused_blk=64, fused_grid=0, fused_gpb=128, fused_slots=4, fused_threads=4, task_batch=16384,
                ap=4, min_free_vram=3000, min_free_ram=800, min_free_disk=1000,
                solver_retries=5, max_host_bad=3, terrain_retries=3, ymax=128,
                limit=None, start=None, tune=True, slab_cells=250_000_000, version="1.19.x~26.1", seed_start=None, max_seeds=None,
                retry_wait=20, terrain_wait=10)
REFERENCE = dict(seed=3159335117276934358, cx=-1852878, cz=-1380275, layout=254754461452089, hi=329, lo=323)
TERRAIN_REFERENCE = ["-5359549684776256167 -1549312 1202688", "-5842228900837151037 84992 981504",
                     "-7671590099420590308 1509888 1081344", "-2118812217737330443 197632 1145344"]
SCAN_REFERENCE = dict(seed=1473884015873122059, hit=(-15792, -54, -17168))      # low54: 698 GPU candidates, 1 confirmed


class HuntError(Exception):
    """Something a person can fix; the message says what."""


def tool(name):
    p = os.path.join(BIN, name + EXE)
    return p if os.path.isfile(p) else None


def need(*names):
    missing = [n for n in names if not tool(n)]
    if missing:
        raise HuntError("missing tools: %s (build them first)" % ", ".join(missing))


def run_text(cmd, input=None, env=None, timeout=None):
    """(returncode, stdout) of a command, or (None, '') when it can't be started."""
    try:
        r = subprocess.run(cmd, input=input, capture_output=True, text=True, errors="replace", env=env, timeout=timeout,
                           creationflags=NO_WINDOW)
        return r.returncode, r.stdout.replace("\r", "")
    except (OSError, subprocess.SubprocessError):
        return None, ""


def atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def seed_of_index(i):
    """The i-th seed of the scrambled stream, as the signed 64-bit number the game shows."""
    v = (i * SCRAMBLE) & 0xFFFFFFFFFFFFFFFF
    return v - (1 << 64) if v >= (1 << 63) else v


def parse_seeds(text):
    """Seeds typed in the box: numbers or words (the game turns words into numbers), or the path of a file that lists them.
    Returns [(seed, note)] in order; raises HuntError when the text names a file that can't be read."""
    t = text.strip()
    if not t:
        return []
    if os.path.isfile(os.path.expanduser(t)):
        try:
            with open(os.path.expanduser(t), encoding="utf-8", errors="replace") as f:
                t = f.read()
        except OSError as e:
            raise HuntError("couldn't read %s: %s" % (text.strip(), e))
    out = []
    for line in t.replace(",", "\n").splitlines():
        line = line.split("#")[0].strip()
        toks = line.split()
        if toks and not all(x.lstrip("-").isdigit() for x in toks):
            toks = [line]                                         # words: one text seed for the whole line
        for tok in toks:
            v, note = W.parse_seed(tok)
            if v is not None:
                out.append((v, note))
    return out


# ---- the machine ------------------------------------------------------------------------------------------------------------
def gpu_stats():
    """{name, temp, util, mem_used, mem_total, watts} of the first NVIDIA GPU, or None. A field the card won't report is None."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    rc, out = run_text([smi, "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
                        "--format=csv,noheader,nounits"], timeout=10)
    try:
        p = [x.strip() for x in out.strip().splitlines()[0].split(",")]
    except IndexError:
        return None
    if rc != 0 or len(p) < 6:
        return None

    def num(x, cast=int):
        try:
            return cast(float(x))
        except ValueError:
            return None
    return dict(name=p[0], temp=num(p[1]), util=num(p[2]), mem_used=num(p[3]), mem_total=num(p[4]), watts=num(p[5], float))


def gpu_free_mib():
    """(name, free VRAM in MiB) of the first NVIDIA GPU, or (None, None)."""
    g = gpu_stats()
    if not g or g["mem_total"] is None or g["mem_used"] is None:
        return (g["name"], None) if g else (None, None)
    return g["name"], g["mem_total"] - g["mem_used"]


def ram_free_mib():
    try:
        if os.path.isfile("/proc/meminfo"):
            for l in open("/proc/meminfo"):
                if l.startswith("MemAvailable:"):
                    return int(l.split()[1]) // 1024
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("p", ctypes.c_ulong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tf", ctypes.c_ulonglong), ("af", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                            ("ae", ctypes.c_ulonglong)]
            m = MS()
            m.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.ap // 2 ** 20)
    except (OSError, ValueError, AttributeError):
        pass
    return None


def minecraft_running():
    """A java process whose command line mentions minecraft. CUDA work next to Minecraft has hard-reset this kind of laptop."""
    try:
        if os.path.isdir("/proc"):
            for pid in os.listdir("/proc"):
                if pid.isdigit():
                    try:
                        with open("/proc/%s/cmdline" % pid, "rb") as f:
                            a = f.read().split(b"\0")
                    except OSError:
                        continue
                    if a and os.path.basename(a[0]).startswith(b"java") and any(b"minecraft" in s.lower() for s in a[1:]):
                        return True
            return False
        if os.name == "nt":
            r = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Process -Filter \"Name like 'java%'\").CommandLine"],
                               capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
            return "minecraft" in r.stdout.lower()
        r = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True, timeout=10)
        return any("java" in l and "minecraft" in l.lower() for l in r.stdout.splitlines())
    except (OSError, subprocess.SubprocessError):
        return False


def gpu_processes():
    """How many hut_lookup / phase1_scan (GPU) processes are running."""
    names = ("hut_lookup", "phase1_scan")
    try:
        if os.path.isdir("/proc"):
            n = 0
            for pid in os.listdir("/proc"):
                if pid.isdigit():
                    try:
                        with open("/proc/%s/comm" % pid, encoding="utf-8") as f:
                            if f.read().startswith(names):
                                n += 1
                    except OSError:
                        pass
            return n
        if os.name == "nt":
            r = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
            return sum(1 for l in r.stdout.splitlines() if l.lower().startswith(names))
        r = subprocess.run(["pgrep", "-f", "hut_lookup|phase1_scan"], capture_output=True, text=True, timeout=10)
        return len(r.stdout.split())
    except (OSError, subprocess.SubprocessError):
        return 0


def lower_priority():
    """Run this process (and what it starts) at low priority, so the computer stays usable."""
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL
        else:
            os.nice(10)
    except (OSError, AttributeError):
        pass


# ---- the finisher: one terrain survivor -> an exact answer, or nothing --------------------------------------------------
def deepdark(ws, cx, cz, floor, run=run_text):
    """0 no deep dark at the mineshaft start, 1 in one of the two biome data sets, 2 in both (then there is no mineshaft)."""
    def biome(extra=None):
        env = dict(os.environ, **(extra or {}))
        _, out = run([tool("biome_at"), "--seed", str(ws), "--cx", str(cx), "--cz", str(cz), "--ms", "--y", str(floor)], env=env)
        t = out.splitlines()[0].split() if out.strip() else []
        return t[1] if len(t) > 1 else ""
    a, b = biome(), biome({"BIOME_26": "1"})
    return 2 if (a == "183" and b == "183") else 1 if (a == "183" or b == "183") else 0


def finish_row(ws, cx, cz, layout, hi, lo, min_r=999, run=run_text):
    """The finisher for one survivor: the result line (the format the project always used) or None if it is rejected.
    Raises HuntError when a tool gives no answer at all."""
    hx, hz = 16 * cx, 16 * cz
    rc, out = run([tool("lysh"), "hut", "--seed", str(ws), "--x", str(hx), "--z", str(hz), "--max-y", "100",
                        "--phase2-max-y", "100", "--version", "1.19.x~26.1"])
    if not out.strip():
        raise HuntError("lysh produced no output for seed %s chunk %s %s" % (ws, cx, cz))
    verdict = next((l for l in out.splitlines() if "verdict" in l), "")
    if "ACCEPT" not in verdict:
        return None
    rc, tree = run([tool("mstree"), str(ws), str(cx), str(cz)])
    if rc != 0 or not tree.strip():
        return None                                           # not a mineshaft start
    fl = tree.splitlines()[0].split()[2]
    _, bout = run([tool("biome_at"), "--seed", str(ws), "--cx", str(cx), "--cz", str(cz), "--ms", "--y", fl])
    bt = bout.splitlines()[0].split() if bout.strip() else []
    bid = bt[1] if len(bt) > 1 else ""
    if deepdark(ws, cx, cz, fl, run) == 2:
        return None
    env = dict(os.environ, MC_SHAPE="79", MC_TRIALS="0")
    _, mout = run([tool("msroom"), "--seed", str(ws), "--x", str(hx), "--z", str(hz)], input=tree, env=env)
    ex = next((l for l in mout.splitlines() if "EXACT" in l), None)
    if not ex:
        return None
    t = ex.split()
    nat = t[5] if len(t) > 5 else "0"
    r1 = rf = ng = None
    for k, w in enumerate(t):
        if w == "top1" and k + 1 < len(t):
            r1 = t[k + 1].split("=")[-1]
        elif w == "refused" and rf is None and k + 1 < len(t):
            rf = t[k + 1]
        elif w == "ng1" and k + 1 < len(t):
            ng = t[k + 1].split("=")[-1]
    if r1 is None or int(r1) > min_r:
        return None
    S, u = ws & 0xFFFFFFFFFFFF, (ws >> 48) & 0xFFFF
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write("X,u,wx,wz,fw,fd,dx,dz,bound\n%d,%d,%d,%d,7,9,%d,%d,0\n" % (S, u, hx, hz, cx, cz))
        tmp = f.name
    try:
        _, sout = run([tool("skyscan"), "--list", tmp])
    finally:
        os.remove(tmp)
    mx = nw = lv = ""
    for tok in sout.split("\n"):
        w = tok.split()
        for k, x in enumerate(w):
            if x == "max" and k + 1 < len(w):
                mx = w[k + 1]
            elif x == "NW" and k + 1 < len(w):
                nw = w[k + 1]
            elif x == "lava" and k + 1 < len(w):
                lv = w[k + 1]
    return ("%4d  terrain %4s  refused %2s  if-none-refused %4s  NW %4s  max %4s  lava %4s  air %s/%s  biome %s  layout %s  "
            "seed %s  /tp %d %d %d" % (int(r1), nat, rf or "0", ng or "0", nw, mx, lv, hi, lo, bid, layout, ws, hx, int(r1), hz))


def accepted_seeds(lines):
    out = set()
    for l in lines:
        t = l.split()
        for k, w in enumerate(t):
            if w == "seed" and k + 1 < len(t):
                out.add(t[k + 1])
    return out


# ---- the oracle: the game's own code reads the true hut height ---------------------------------------------------------
def java_cmd(ymax=128):
    java, major = mcfiles.java_ok()
    if major < 21:
        raise HuntError("Java 21 isn't there yet (Get Minecraft files fetches it)")
    if not mcfiles.ready():
        raise HuntError("the Minecraft files aren't downloaded yet")
    need("terrain_dump")
    cp = os.pathsep.join([os.path.join(ORACLE, "classes")] + mcfiles.classpath())
    return [java, "-Xmx2g", "-Dterrain.dump=" + tool("terrain_dump"), "-Dcoin.ymax=%d" % ymax, "-cp", cp, "Oracle21"]


def oracle(spots_path, threads=8, ymax=128, run=run_text):
    """The oracle's 'S' lines for the 'seed chunkX chunkZ' lines in spots_path."""
    rc, out = run(java_cmd(ymax) + ["batchmt", "1", spots_path, "0", str(threads)])
    if rc is None:
        raise HuntError("couldn't run Java")
    return [l for l in out.splitlines() if l.startswith("S ")]


def parse_exact(line):
    t = line.split()
    nb, rb = "?", ""
    for k, w in enumerate(t):
        if w == "neighborStarts" and k + 1 < len(t):
            nb = t[k + 1]
        if w.startswith("room+"):
            rb = w
    return dict(hutY=int(t[10]), air=int(t[6]), seed=t[1], cx=int(t[3]), cz=int(t[4]), nb=nb, room=rb)


def make_results(exact_lines):
    """The result lines 'hutY air seed cx cz nb=N room+N', deepest first, then most air."""
    rows = [parse_exact(l) for l in exact_lines if l.startswith("S ")]
    rows.sort(key=lambda r: (r["hutY"], -r["air"]))
    return ["%d %d %s %d %d nb=%s %s" % (r["hutY"], r["air"], r["seed"], r["cx"], r["cz"], r["nb"], r["room"]) for r in rows]


# ---- the results files ---------------------------------------------------------------------------------------------------
def _append_unique(path, key, line):
    """Append line to path unless a line there already starts with key. True when it was added."""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            if any(x.startswith(key) for x in f):
                return False
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return True


def record(result_lines, tag, results_dir=None, find_y=-58, min_depth=55):
    """Append every hut at -min_depth or deeper to results/<depth>.txt (no duplicates). Returns the new lines."""
    results_dir = results_dir or RESULTS
    os.makedirs(results_dir, exist_ok=True)
    new = []
    for l in result_lines:
        t = l.split()
        y = int(t[0])
        if y > -min_depth:
            continue
        depth, air, seed, cx, cz, nb, room = -y, t[1], t[2], int(t[3]), int(t[4]), t[5], t[6] if len(t) > 6 else ""
        line = "%s %d %d /tp %d -%d %d air=%s %s %s run=%s inside=/tp %d %d %d" % (
            seed, cx, cz, 16 * cx, depth, 16 * cz, air, nb, room, tag, 16 * cx + 3, 2 - depth, 16 * cz + 7)
        if not _append_unique(os.path.join(results_dir, "%d.txt" % depth), "%s %d %d " % (seed, cx, cz), line):
            continue
        new.append(line)
        if y <= find_y:                                      # the short list: "seed   /tp x y z"
            short = "%s   /tp %d %d %d" % (seed, 16 * cx + 3, 2 - depth, 16 * cz + 7)
            _append_unique(os.path.join(results_dir, "%d_seeds.txt" % abs(find_y)), short, short)
    return new


def record_scan(seed, x, y, z, tag, results_dir=None):
    """A hut the scanner found. The /tp is the one the project's scanner gives (the hut's footprint average height)."""
    results_dir = results_dir or RESULTS
    os.makedirs(results_dir, exist_ok=True)
    cx, cz = x >> 4, z >> 4
    line = "%s %d %d /tp %d %d %d scan run=%s inside=/tp %d %d %d" % (seed, cx, cz, x, y, z, tag, x, y, z)
    return line if _append_unique(os.path.join(results_dir, "%d.txt" % -y), "%s %d %d " % (seed, cx, cz), line) else None


def record_deep(seed, cx, cz, depth, tag, results_dir=None):
    """A hut the scanner found sitting on a mineshaft room, deeper than the terrain alone goes (-55 to -59). Same line as the solver's."""
    results_dir = results_dir or RESULTS
    os.makedirs(results_dir, exist_ok=True)
    line = "%s %d %d /tp %d -%d %d scan run=%s inside=/tp %d %d %d" % (seed, cx, cz, 16 * cx, depth, 16 * cz, tag, 16 * cx + 3, 2 - depth, 16 * cz + 7)
    return line if _append_unique(os.path.join(results_dir, "%d.txt" % depth), "%s %d %d " % (seed, cx, cz), line) else None


def all_results(results_dir=None):
    """Every recorded hut, deepest first: [dict(depth, seed, cx, cz, x, y, z, run)] with the /tp position inside the hut."""
    results_dir = results_dir or RESULTS
    out = []
    if not os.path.isdir(results_dir):
        return out
    for fn in os.listdir(results_dir):
        if not fn.endswith(".txt") or fn.endswith("_seeds.txt") or not fn[:-4].isdigit():
            continue
        for l in open(os.path.join(results_dir, fn), encoding="utf-8"):
            t = l.split()
            if len(t) < 3 or "inside=/tp" not in l:
                continue
            ins = l.split("inside=/tp")[1].split()
            run = next((w[4:] for w in t if w.startswith("run=")), "")
            out.append(dict(depth=int(fn[:-4]), seed=t[0], cx=int(t[1]), cz=int(t[2]), x=int(ins[0]), y=int(ins[1]), z=int(ins[2]), run=run))
    for r in out:
        r["dist"] = int(math.hypot(r["x"], r["z"]))              # blocks from 0,0
    out.sort(key=lambda r: (-r["depth"], r["dist"], r["seed"]))   # deepest first; at the same depth, the closest to 0,0 first
    return out


# ---- what a mode and level need ------------------------------------------------------------------------------------------------
def combo_problem(level, mode):
    """Why this level can't run in this mode, or None."""
    if level not in LEVELS:
        return "pick a floor between -50 and -59"
    if level <= -55 and not MODES[mode]["gpu"]:
        return ("-55 and deeper needs an NVIDIA card: each candidate stands for %s world seeds, which a GPU checks at about "
                "94 million a second and a CPU at about 50 thousand" % format(SEEDS_PER_SPOT, ","))
    return None


def preflight_rows(level, mode, cfg=None):
    """[(ok, text, fix)]: everything this level in this mode needs, each with what fixes it when it isn't so.
    fix is None, or one of "build", "gpu-build", "minecraft", "java", "close-minecraft"."""
    c = dict(DEFAULTS, **(cfg or {}))
    gpu = MODES[mode]["gpu"]
    solver = level <= -55
    rows = []

    def add(ok, good, bad, fix=None):
        rows.append((bool(ok), good if ok else bad, None if ok else fix))
    why = combo_problem(level, mode)
    if why:
        add(False, "", why)
        return rows
    if gpu:
        g = gpu_stats()
        add(g, (g["name"].replace("NVIDIA ", "") if g else ""), "no NVIDIA card found: pick a CPU mode (it covers -50 to -54), or hunt on a machine with one")
        if g:
            free = None if g["mem_total"] is None else g["mem_total"] - (g["mem_used"] or 0)
            add(free is None or free >= c["min_free_vram"], "%.1f GB video memory free" % ((free or 0) / 1024.0),
                "only %s MiB of video memory free (needs %d): close programs that use the GPU" % (free, c["min_free_vram"]))
        add(not minecraft_running(), "Minecraft isn't running",
            "Minecraft is running: close it first (a GPU hunt beside it can crash a laptop's graphics card)", "close-minecraft")
    if solver:
        names = ("hut_lookup_v4", "mstree", "lysh", "msroom", "skyscan", "biome_at", "terrain_dump")
    elif gpu:
        names = ("phase1_scan", "lysh")
    else:
        names = ("lysh",)
    miss = [n for n in names if not tool(n)]
    if miss:
        gpu_part = [n for n in miss if n in ("hut_lookup_v4", "phase1_scan")]
        cuda = shutil.which("nvcc") or os.path.isfile("/opt/cuda/bin/nvcc")
        if gpu_part and os.name != "nt" and not cuda:
            add(False, "", "the GPU engine isn't built, and building it needs NVIDIA's CUDA toolkit (about 5 GB): %s" %
                W.distro_hint("nvidia-cuda-toolkit", "cuda-toolkit", "cuda", "Nvidia.CUDA"), "gpu-build")
        else:
            add(False, "", "tools not built yet: %s" % ", ".join(miss), "build")
    else:
        add(True, "tools built", "")
    if solver:
        java, major = mcfiles.java_ok()
        add(major >= 21, "Java %d" % major, "Java 21 comes with the Minecraft files download (about 50 MB)", "minecraft")
        add(mcfiles.ready(), "Minecraft files", "the Minecraft files aren't downloaded yet (they come from Mojang, about 90 MB)", "minecraft")
        ds = dataset_of(level)
        add(os.path.isfile(os.path.join(DATA, ds, "layout_catalog.txt")) and os.path.isfile(os.path.join(DATA, ds, "plan.txt")),
            "layout data", "data/hunt/%s is missing" % ds)
    ram = ram_free_mib()
    add(ram is None or ram >= c["min_free_ram"], "%.1f GB memory free" % ((ram or 0) / 1024.0), "only %s MiB of memory free" % ram)
    disk = shutil.disk_usage(ROOT).free // 2 ** 20
    add(disk >= c["min_free_disk"], "%d GB disk free" % (disk // 1024), "only %s MiB of disk free (needs %d)" % (format(disk, ","), c["min_free_disk"]))
    return rows


def dataset_of(level):
    return "59" if level <= -59 else "58"


def machine():
    """What this computer can do, for the first-run defaults and the status line."""
    g = gpu_stats()
    java, major = mcfiles.java_ok()
    return dict(gpu=g, cores=os.cpu_count() or 1, java=major, minecraft_files=mcfiles.ready(),
                gpu_engine=bool(tool("hut_lookup_v4")), cpu_tools=bool(tool("lysh")))


def default_settings(m=None):
    """The best first choice for this computer: the GPU search if there is a card and its engine, otherwise the CPU scanner."""
    m = m or machine()
    if m["gpu"] and m["gpu_engine"]:
        return dict(level=-58, mode="gpu", seeds="", area=0)
    return dict(level=-54, mode="cpu", seeds="", area=0)


# ---- a run ------------------------------------------------------------------------------------------------------------------
def covered(ranges, i, n):
    """True when [i, i+n) lies inside one of the sorted, merged (start, end) ranges."""
    k = bisect.bisect_right(ranges, (i, float("inf"))) - 1
    return k >= 0 and ranges[k][0] <= i and i + n <= ranges[k][1]


def merge_ranges(pairs):
    out = []
    for a, b in sorted(pairs):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def live_path(tag):
    return os.path.join(RUNS, tag, "live.json")


class Run:
    """What the two searches share: the run folder, starting and stopping, the live file, the GPU's temperature."""
    kind = ""

    def __init__(self, level, mode, seeds="", area=0, **cfg):
        self.level, self.mode, self.seeds_text, self.area = int(level), mode, seeds or "", int(area or 0)
        self.or_lower = bool(cfg.pop("or_lower", True))
        if mode not in MODES:
            raise HuntError("unknown mode %r" % mode)
        self.cfg = dict(DEFAULTS, **cfg)
        self.tag = self.make_tag()
        self.out = os.path.join(RUNS, self.tag)
        self.stopping = False
        self.stop_evt, self.done_evt = threading.Event(), threading.Event()
        self.lock, self.procs_lock, self.gpulock = threading.Lock(), threading.Lock(), threading.Lock()
        self.procs = []
        self.live, self.live_lock = {}, threading.Lock()
        self.hot = False
        self.gpu = None
        self.started = time.time()

    # files
    def f(self, name):
        return os.path.join(self.out, name)

    def lines(self, name):
        try:
            with open(self.f(name), encoding="utf-8") as fh:
                return [l.rstrip("\n") for l in fh]
        except OSError:
            return []

    def note(self, name, text):
        with open(self.f(name), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))

    def settings(self):
        return dict(level=self.level, mode=self.mode, seeds=self.seeds_text, area=self.area, or_lower=self.or_lower)

    # state
    def pid(self):
        try:
            return int(open(self.f("driver.pid")).read().strip())
        except (OSError, ValueError):
            return None

    def alive(self):
        pid = self.pid()
        if not pid:
            return False
        try:
            if os.name == "nt":
                r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid], capture_output=True, text=True, creationflags=NO_WINDOW)
                return str(pid) in r.stdout
            os.kill(pid, 0)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def read_live(self):
        try:
            with open(self.f("live.json"), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    # the driver's own bookkeeping
    def set_phase(self, phase, detail="", **kw):
        with self.live_lock:
            self.live.update(phase=phase, detail=detail, **kw)
        self.write_live()

    def write_live(self):
        with self.live_lock:
            d = dict(self.live, pid=os.getpid(), started=self.started, now=time.time(), level=self.level, mode=self.mode,
                     stopping=self.stopping, hot=self.hot, gpu=self.gpu)
            try:
                atomic_write(self.f("live.json"), json.dumps(d))
            except OSError:
                pass

    def sample_gpu(self):
        g = gpu_stats()
        self.gpu = g
        if g and g["temp"] is not None:
            self.hot = g["temp"] >= HOT_PAUSE or (self.hot and g["temp"] > HOT_RESUME)

    def monitor(self):
        """Every half second: has Stop been pressed? Every two: how is the GPU, and write the live file."""
        n = 0
        while not self.done_evt.wait(0.5):
            if not self.stopping and os.path.exists(self.f("STOP")):
                self.request_stop()
            n += 1
            if n % 4 == 0:
                if MODES[self.mode]["gpu"]:
                    self.sample_gpu()
                self.write_live()

    def request_stop(self, *_):
        """Stop within seconds: end whatever is running now. Work that isn't finished is simply done again next time."""
        self.stopping = True
        self.stop_evt.set()
        with self.live_lock:
            self.live.update(phase="stopping", detail="")
        self.write_live()
        self.kill_procs()

    def popen(self, cmd, stdout, stderr=subprocess.STDOUT, stdin=None):
        """Run a command to its end; -1 at once if the run is stopping. Stop ends it (the monitor kills what is running)."""
        if self.stopping:
            return -1
        p = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, stdin=stdin, creationflags=NO_WINDOW)
        with self.procs_lock:
            self.procs.append(p)
        if self.stopping:
            self.kill_procs()
        try:
            return p.wait()
        finally:
            with self.procs_lock:
                if p in self.procs:
                    self.procs.remove(p)

    def run_tracked(self, cmd, input=None, env=None, timeout=None):
        """run_text, but Stop can end it: (returncode, stdout), or (None, '') when it can't be started or the run is stopping."""
        if self.stopping:
            return None, ""
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, errors="replace", env=env, creationflags=NO_WINDOW)
        except OSError:
            return None, ""
        with self.procs_lock:
            self.procs.append(p)
        if self.stopping:
            self.kill_procs()
        try:
            out, _ = p.communicate(input, timeout=timeout)
            return p.returncode, (out or "").replace("\r", "")
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            return None, ""
        finally:
            with self.procs_lock:
                if p in self.procs:
                    self.procs.remove(p)

    def kill_procs(self):
        with self.procs_lock:
            ps = list(self.procs)
        for p in ps:
            try:
                p.terminate()
            except OSError:
                pass
        end = time.time() + 3
        for p in ps:
            try:
                p.wait(max(0.1, end - time.time()))
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                except OSError:
                    pass
            except OSError:
                pass

    def wait_cool(self):
        """Hold off new GPU work while the card is above HOT_PAUSE, until it is back under HOT_RESUME."""
        while self.hot and not self.stopping:
            t = (self.gpu or {}).get("temp")
            self.set_phase("cooling", "the GPU is at %s C: waiting for %d C" % (t, HOT_RESUME))
            self.stop_evt.wait(2.0)

    def rest(self, busy_s):
        """GPU gentle: after working for busy_s, idle as long again (duty 0.5)."""
        duty = MODES[self.mode]["duty"]
        if duty < 1.0 and busy_s > 0 and not self.stopping:
            self.stop_evt.wait(busy_s * (1.0 / duty - 1.0))

    # starting and stopping from outside (the app, the command line)
    def preflight(self):
        return preflight_rows(self.level, self.mode, self.cfg)

    def start_detached(self):
        """Start the run as its own process (it survives this one) and return at once."""
        other = running()
        if other:
            raise HuntError("a hunt is already running (%s at %d); stop it first" % (MODES[other.mode]["label"], other.level))
        bad = [t for ok, t, _ in self.preflight() if not ok]
        if bad:
            raise HuntError(bad[0])
        os.makedirs(self.out, exist_ok=True)
        for stale in ("STOP", "FAILED"):
            if os.path.exists(self.f(stale)):
                os.remove(self.f(stale))
        atomic_write(self.f("settings.json"), json.dumps(self.settings()))
        log = open(self.f("driver.log"), "a", encoding="utf-8")
        kw = dict(creationflags=0x00000008 | 0x00000200 | NO_WINDOW) if os.name == "nt" else dict(start_new_session=True)
        cmd = [sys.executable, "--hunt", "run", "--tag", self.tag] if getattr(sys, "frozen", False) \
            else [sys.executable, "-I", os.path.join(ROOT, "hunt.py"), "run", "--tag", self.tag]      # the .exe runs itself as the driver
        subprocess.Popen(cmd,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=ROOT, **kw)

    def stop(self, now=False):
        """Ask the driver to stop; it ends what it is doing within a few seconds. now: also kill the driver itself."""
        os.makedirs(self.out, exist_ok=True)
        open(self.f("STOP"), "w").close()
        if now:
            pid = self.pid()
            if pid:
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
                    else:
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                except (OSError, subprocess.SubprocessError):
                    pass
            for stale in ("driver.pid", "STOP"):
                if os.path.exists(self.f(stale)):
                    os.remove(self.f(stale))

    # the driver
    def run(self):
        """Run in this process until the work is done, a hut is found, or Stop. Returns the exit code."""
        os.makedirs(self.out, exist_ok=True)
        for stale in ("STOP", "FAILED"):
            if os.path.exists(self.f(stale)):
                os.remove(self.f(stale))
        with open(self.f("driver.pid"), "w") as fh:
            fh.write(str(os.getpid()))
        atomic_write(self.f("settings.json"), json.dumps(self.settings()))
        if MODES[self.mode]["share"] < 1.0:
            lower_priority()
        if hasattr(signal, "SIGTERM"):
            try:
                signal.signal(signal.SIGTERM, self.request_stop)
            except ValueError:
                pass                                              # not the main thread (tests)
        self.started = time.time()
        mon = threading.Thread(target=self.monitor, daemon=True)
        mon.start()
        outcome, failed = "stopped", None
        try:
            if MODES[self.mode]["gpu"]:
                self.sample_gpu()
            outcome = self.work()
        except HuntError as e:
            outcome, failed = "failed", str(e)
        except BaseException as e:                                # noqa: BLE001  (the reason goes to the log, never lost)
            outcome, failed = "failed", "the hunt crashed: %s: %s (details in driver.log)" % (type(e).__name__, e)
            traceback.print_exc()
        finally:
            self.kill_procs()
            self.done_evt.set()
            mon.join(timeout=3)
            if self.stopping and outcome != "failed":
                outcome = "stopped"
            with self.live_lock:
                self.live.update(phase=outcome, detail=failed or "", unit_t0=None)
            self.write_live()
            if os.path.exists(self.f("driver.pid")):
                os.remove(self.f("driver.pid"))
            if os.path.exists(self.f("STOP")):
                os.remove(self.f("STOP"))
        if failed:
            with open(self.f("FAILED"), "w", encoding="utf-8") as fh:
                fh.write(failed)
            print("driver aborted:", failed, flush=True)
            return 1
        if outcome == "found":
            sync_catalog()
        print("driver finished:", outcome, flush=True)
        return 0

    def work(self):
        raise NotImplementedError

    def new_finds(self, base):
        return len(self.found()) - base


# ---- the solver: -55 .. -59, GPU only ------------------------------------------------------------------------------------------
class SolveRun(Run):
    kind = "solver"

    def __init__(self, level=-58, mode="gpu", seeds="", area=0, **cfg):
        self.ds = dataset_of(int(level))
        super().__init__(level, mode, seeds, 0, **cfg)
        self.cat = os.path.join(DATA, self.ds, "layout_catalog.txt")
        self.plan = os.path.join(DATA, self.ds, "plan.txt")
        self.chunk = int(self.cfg["chunk"])

    def make_tag(self):
        return "hunt" + self.ds

    # the search space: a "bundle" is 2^20 groups, one per layout in the plan (-58 has one, -59 has two)
    def bundles(self):
        try:
            with open(self.plan, encoding="utf-8") as f:
                return max(1, sum(1 for l in f if l.strip()))
        except OSError:
            return 1

    def span(self):
        total = self.bundles() << 20
        return min(total, int(self.cfg["limit"])) if self.cfg["limit"] else total

    def done_ranges(self):
        pairs = []
        for l in self.lines("done.txt"):
            t = l.split()
            if len(t) == 2 and t[0].lstrip("-").isdigit() and t[1].isdigit():
                pairs.append((int(t[0]), int(t[0]) + int(t[1])))
        return merge_ranges(pairs)

    def pick_start(self, chunk, rng=None):
        """A random chunk-aligned start. The first eighth of the first bundle is skipped: everyone's first search covers it."""
        rng = rng or random.SystemRandom()
        span = self.span()
        lo = min(((1 << 20) // 8) // chunk, max(0, span // chunk - 1))
        return rng.randrange(lo, max(lo + 1, span // chunk)) * chunk

    def start_group(self, chunk):
        """Where the search begins, as a group index. Random the first time, remembered after, and a number typed in the
        Seeds box always gives the same place."""
        span = self.span()
        typed = None
        if self.seeds_text.strip():
            v, _ = W.parse_seed(self.seeds_text.split()[0])
            typed = None if v is None else (v * 2654435761 + 12345) % span
        s = typed if typed is not None else self.cfg["start"]
        if s is None:
            try:
                s = int(open(self.f("start.txt")).read().split()[0])
            except (OSError, ValueError, IndexError):
                s = None
        if s is None or not 0 <= s < span:
            s = self.pick_start(chunk)
        os.makedirs(self.out, exist_ok=True)
        atomic_write(self.f("start.txt"), "%d\n" % s)
        return s // chunk * chunk

    def new_spot(self):
        """Forget where the search was heading and pick another random place (chunks already done stay done)."""
        if os.path.exists(self.f("start.txt")):
            os.remove(self.f("start.txt"))

    def chunks(self, chunk=None):
        """(first group, groups) of each chunk still to do, from the start to the end of the space and round again."""
        c = chunk or self.chunk
        span = self.span()
        n = (span + c - 1) // c
        s = self.start_group(c) // c
        done = self.done_ranges()
        for k in range(n):
            i = ((s + k) % n) * c
            m = min(c, span - i)
            if not covered(done, i, m):
                yield i, m

    # tuning: once per GPU
    def engine_stamp(self):
        try:
            st = os.stat(tool("hut_lookup_v4"))
            return "%d-%d" % (st.st_size, int(st.st_mtime))
        except (OSError, TypeError):
            return ""

    def load_tuned(self):
        try:
            with open(os.path.join(RUNS, "tuned.json"), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def tuned_for(self):
        name = (self.gpu or {}).get("name") or ""
        t = self.load_tuned().get(name)
        return t if t and t.get("engine") == self.engine_stamp() else None

    def solver_cmd(self, first, count, hits):
        c = self.cfg
        _, fused = self.engine_args()
        return [tool("hut_lookup_v4"), "--catalog", self.cat, "--solve-only", "--solve5", "--plan", self.plan] + fused + \
               ["--regime", str(c["regime"]), "--first-index", str(first), "--count", str(count), "--task-batch", str(c["task_batch"]),
                "--quiet", "--dump-hits", hits, "--out", self.f("dummy.csv")]

    def terrain_cmd(self, spots, out, batch):
        ero, _ = self.engine_args()
        return [tool("hut_lookup_v4"), "--catalog", self.cat, "--spots", spots, "--batch", str(batch)] + ero + \
               ["--max-height", "-54", "--quiet", "--out", out]

    def tune(self):
        """Time the GPU once: pick the batch size that is fastest for the same answers, and a chunk of about a minute.
        Returns the tuning dict, or None when it couldn't be done (then the defaults stand)."""
        d = os.path.join(self.out, "tune")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        try:
            self.set_phase("tuning", "timing this GPU once (about 30 seconds)")
            first, groups, hits = self.start_group(self.chunk), 8, os.path.join(d, "hits.csv")
            while True:                                           # enough matches to time the terrain stage on
                if self.popen(self.solver_cmd(first, groups, hits), subprocess.DEVNULL) != 0:
                    return None
                with open(hits, encoding="utf-8") as fh:
                    rows = [l.split(",") for l in fh if l.count(",") >= 2]
                if len(rows) >= 6000 or groups >= 64:
                    break
                groups *= 4
            if len(rows) < 1500:
                return None
            per_group = len(rows) / groups
            sp = {}

            def spots(n):
                p = os.path.join(d, "spots_%d.txt" % n)
                if n not in sp:
                    with open(p, "w", encoding="utf-8") as fh:
                        fh.writelines("%s %s %s\n" % (r[0], r[1], r[2]) for r in rows[:n])
                    sp[n] = p
                return sp[n]

            def timed(n, batch):
                out = os.path.join(d, "surv_%d_%d.csv" % (n, batch))
                t0 = time.time()
                if self.popen(self.terrain_cmd(spots(n), out, batch), subprocess.DEVNULL) != 0 or not os.path.isfile(out):
                    return None, None
                with open(out, encoding="utf-8") as fh:
                    return time.time() - t0, sorted(fh.read().splitlines())
            n1, n2 = 1500, min(6000, len(rows))
            t1, _ = timed(n1, self.cfg["batch"])
            t2, ref = timed(n2, self.cfg["batch"])
            if not t1 or not t2 or t2 <= t1:
                return None
            rate = (n2 - n1) / (t2 - t1)                          # spots a second, with the start-up time taken out
            best, best_t = self.cfg["batch"], t2
            for b in (32, 16):
                if self.stopping:
                    return None
                t, got = timed(n2, b)
                if t and got == ref and t < best_t * 0.93:         # only a clear win, and only for identical answers
                    best, best_t = b, t
            groups_for_minute = rate * 60.0 / per_group
            chunk = 16
            while chunk * 2 <= min(groups_for_minute, 1024):
                chunk *= 2
            res = dict(batch=best, chunk=chunk, spots_per_s=round(rate), matches_per_group=round(per_group),
                       engine=self.engine_stamp(), when=time.strftime("%Y-%m-%d"))
            allt = self.load_tuned()
            allt[(self.gpu or {}).get("name") or ""] = res
            os.makedirs(RUNS, exist_ok=True)
            atomic_write(os.path.join(RUNS, "tuned.json"), json.dumps(allt, indent=1))
            return res
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # the solver and the terrain stage (GPU)
    def engine_args(self):
        c = self.cfg
        ero = ["--ero-split", "--ero-t1", str(c["ero_t1"]), "--ero-c0", str(c["ero_c0"]), "--ero-t3", str(c["ero_t3"]), "--ero-c1", str(c["ero_c1"])]
        fused = ["--fused-blk", str(c["fused_blk"]), "--fused-grid", str(c["fused_grid"]), "--fused-gpb", str(c["fused_gpb"]),
                 "--fused-slots", str(c["fused_slots"]), "--fused-threads", str(c["fused_threads"])]
        return ero, fused

    def solve(self, i, n):
        c = self.cfg
        hits = self.f("hits_%d.csv" % i)
        cmd = self.solver_cmd(i, n, hits)
        for attempt in range(1, c["solver_retries"] + 1):
            self.wait_cool()
            if os.path.exists(hits):
                os.remove(hits)
            logp = self.f("solver_%d.log" % i)
            t0 = time.time()
            with open(logp, "w", encoding="utf-8") as lg:
                rc = self.popen(cmd, lg)
            log = open(logp, encoding="utf-8", errors="replace").read()
            os.remove(logp)
            if self.stopping:
                return False
            self.rest(time.time() - t0)
            if rc == 0:
                return True
            bad = log.count("HOST CHECK FAILED")
            if rc == 1 and 1 <= bad <= c["max_host_bad"] and os.path.isfile(hits) and os.path.getsize(hits) > 0 and "solve:" in log:
                self.note("notes.txt", "chunk %d: solver dropped %d hit(s) that failed its host check (accepted)" % (i, bad))
                return True
            self.note("problems.txt", "solver failed at chunk %d, attempt %d of %d" % (i, attempt, c["solver_retries"]))
            if attempt == c["solver_retries"]:
                break
            self.stop_evt.wait(c["retry_wait"])
        return False

    def terrain(self, i):
        c = self.cfg
        surv = self.f("surv_%d.csv" % i)
        cmd = self.terrain_cmd(self.f("spots_%d.txt" % i), surv, c["batch"])
        for attempt in range(1, c["terrain_retries"] + 1):
            if os.path.exists(surv):
                os.remove(surv)
            self.wait_cool()
            t0 = time.time()
            with self.gpulock, open(self.f("log.txt"), "a", encoding="utf-8") as lg:
                rc = self.popen(cmd, lg)
            if self.stopping:
                return False
            self.rest(time.time() - t0)
            if rc == 0 and os.path.isfile(surv) and open(surv, encoding="utf-8").readline().startswith("seed"):
                return True
            self.note("problems.txt", "chunk %d: terrain stage failed (attempt %d of %d)" % (i, attempt, c["terrain_retries"]))
            if attempt < c["terrain_retries"]:
                self.stop_evt.wait(c["terrain_wait"])
        return False

    def cleanup_chunk(self, i):
        for name in ("hits_%d.csv" % i, "spots_%d.txt" % i, "surv_%d.csv" % i, "exact_in_%d.txt" % i):
            if os.path.exists(self.f(name)):
                os.remove(self.f(name))

    # the CPU stages and the oracle, one chunk
    def cpu_stages(self, i, n, t0):
        c = self.cfg
        hits = self.f("hits_%d.csv" % i)
        nh = 0
        spots = self.f("spots_%d.txt" % i)
        with open(spots, "w", encoding="utf-8") as dst:                # one line at a time: the file can be hundreds of MB
            if os.path.isfile(hits):
                with open(hits, encoding="utf-8") as src:
                    for line in src:
                        r = line.rstrip("\n").split(",")
                        if len(r) >= 3:
                            dst.write("%s %s %s\n" % (r[0], r[1], r[2]))
                            nh += 1
        ns = nf = ne = 0
        exact = []
        if nh:
            self.set_phase("terrain", "checking %s matches against the ground" % format(nh, ","), unit_t0=time.time(),
                           unit_expect=self.expect_terrain(nh), chunk=i)
            if not self.terrain(i):
                if not self.stopping:
                    self.note("problems.txt", "chunk %d: terrain stage failed %d times; chunk NOT marked done" % (i, c["terrain_retries"]))
                return False
            with open(self.f("surv_%d.csv" % i), encoding="utf-8") as fh:
                surv = [l.rstrip("\n").split(",") for l in fh][1:]
            ns = len(surv)
            fin = []
            if surv:
                self.set_phase("finishing", "%d survivors on exact terrain" % ns, chunk=i)
                with concurrent.futures.ThreadPoolExecutor(max(1, c["ap"])) as ex:
                    futs = [ex.submit(finish_row, int(r[0]), int(r[1]), int(r[2]), r[3], r[4], r[5], 999, self.run_tracked) for r in surv]
                    try:
                        fin = [x for x in (f.result() for f in futs) if x]
                    except HuntError as e:
                        if not self.stopping:
                            self.note("problems.txt", "chunk %d: finisher tool error: %s" % (i, e))
                        return False
                if self.stopping:
                    return False
                with self.lock, open(self.f("finisher.txt"), "a", encoding="utf-8") as fh:
                    fh.writelines(l + "\n" for l in fin)
                acc = accepted_seeds(fin)
                nf = len(acc)
                keep = [r for r in surv if r[0] in acc]
                if keep:
                    inp = self.f("exact_in_%d.txt" % i)
                    with open(inp, "w", encoding="utf-8") as fh:
                        for r in keep:
                            fh.write("%s %s %s\n" % (r[0], r[1], r[2]))
                    want = len(keep)
                    self.set_phase("oracle", "Java checks %d candidates with the game's own code" % want, chunk=i)
                    for att in (1, 2):
                        try:
                            exact = oracle(inp, 8, c["ymax"], self.run_tracked)
                        except HuntError as e:
                            if not self.stopping:
                                self.note("problems.txt", "chunk %d: oracle: %s" % (i, e))
                            return False
                        if self.stopping:
                            return False
                        ne = len(exact)
                        if ne >= want:
                            break
                        self.note("problems.txt", "chunk %d: oracle answered %d of %d lines (attempt %d of 2)" % (i, ne, want, att))
                        self.stop_evt.wait(5)
                    if ne < want:
                        return False
        with self.lock:
            if exact:
                with open(self.f("exact.txt"), "a", encoding="utf-8") as fh:
                    fh.writelines(l + "\n" for l in exact)
            if os.path.isfile(self.f("exact.txt")):
                res = make_results(self.lines("exact.txt"))
                atomic_write(self.f("results.txt"), "".join(l + "\n" for l in res))
                record(res, self.tag, RESULTS, self.level, c["min_depth"])
            self.cleanup_chunk(i)
            now = int(time.time())
            with open(self.f("funnel.txt"), "a", encoding="utf-8") as fh:
                fh.write("%d %d %d %d %d %d %d %d\n" % (i, n, nh, nh, ns, nf, ne, now - int(t0)))
            with open(self.f("done.txt"), "a", encoding="utf-8") as fh:
                fh.write("%d %d\n" % (i, n))
            with open(self.f("chunk_times.txt"), "a", encoding="utf-8") as fh:
                fh.write("%d\n" % now)
        return True

    def expect_terrain(self, matches):
        t = self.tuned_for()
        rate = (t or {}).get("spots_per_s")
        if rate:
            return max(5.0, matches / float(rate))
        funnel = [list(map(int, l.split())) for l in self.lines("funnel.txt")[-3:] if l.strip()]
        if funnel:
            return max(5.0, sum(r[7] for r in funnel) / len(funnel) * 0.9)
        return max(20.0, matches / 1400.0)

    def found(self):
        return [l for l in self.lines("results.txt") if l.split() and int(l.split()[0]) <= self.level]

    def work(self):
        c = self.cfg
        need("hut_lookup_v4")
        open(self.f("done.txt"), "a").close()
        for name in os.listdir(self.out):                          # leftovers of a chunk that was cut short
            if name.startswith(("hits_", "spots_", "surv_", "exact_in_", "solver_")):
                os.remove(self.f(name))
        tuned = self.tuned_for() if c["tune"] else None
        if c["tune"] and not tuned and not self.stopping:
            tuned = self.tune()
        if tuned:
            self.cfg["batch"], self.chunk = tuned["batch"], tuned["chunk"]
        atomic_write(self.f("config.txt"), "LEVEL=%d MODE=%s CHUNK=%d BATCH=%d SPAN=%d\n" %
                     (self.level, self.mode, self.chunk, self.cfg["batch"], self.span()))
        base = len(self.found())
        ex = concurrent.futures.ThreadPoolExecutor(max(1, c["depth"] - 1))
        pend, failed, outcome = [], None, "finished"
        try:
            for i, n in self.chunks():
                if self.stopping:
                    outcome = "stopped"
                    break
                t0 = time.time()
                if pend:
                    with self.live_lock:
                        self.live["solving"] = i
                else:
                    self.set_phase("solving", "listing the matches of chunk %d" % i, chunk=i, unit_t0=t0, unit_expect=max(4.0, self.chunk * 0.06))
                if not self.solve(i, n):
                    if not self.stopping:
                        failed = "the solver failed at chunk %d (see problems.txt in runs/%s)" % (i, self.tag)
                    break
                while len(pend) >= max(1, c["depth"] - 1):
                    pend.pop(0).result()
                if self.stopping:
                    break
                if c["stop_on_find"] and self.new_finds(base) > 0:
                    outcome = "found"
                    break
                pend.append(ex.submit(self.cpu_stages, i, n, t0))
            for fu in pend:
                fu.result()
            if outcome == "finished" and self.new_finds(base) > 0:
                outcome = "found"
            if self.stopping:
                outcome = "stopped"
        finally:
            ex.shutdown(wait=True)
        if failed:
            raise HuntError(failed)
        return outcome

    # what the app shows
    def matches_per_find(self, funnel):
        """Matches the solver needs per hut at this level or deeper: this machine's own count once it has a few finds, else the
        measured figure for -58 and -59 (matches don't depend on the machine; only how fast they come does), else None."""
        matches = sum(r[2] for r in funnel)
        finds = sum(1 for l in self.lines("results.txt") if l.split() and int(l.split()[0]) <= self.level)
        if finds >= SELF_FINDS and matches:
            return matches / finds
        return PER_FIND.get(self.level)

    def status(self):
        funnel = [list(map(int, l.split())) for l in self.lines("funnel.txt") if l.strip()]
        done = len([l for l in self.lines("done.txt") if l.strip()])
        matches = sum(r[2] for r in funnel)
        times = [int(l) for l in self.lines("chunk_times.txt") if l.strip()]
        live = self.read_live()
        running = self.alive()
        rate = None                                              # matches per second since this run began, on this machine
        begun = live.get("started") or self.started
        n = min(len(funnel), len(times))
        mine = [i for i in range(n) if times[len(times) - n + i] > begun]           # chunks finished since the run began
        if len(mine) >= MEASURE_CHUNKS:
            span = times[-1] - begun                           # wall clock: tuning, hot-GPU pauses and gentle mode all count
            rate = sum(funnel[len(funnel) - n + i][2] for i in mine) / span if span >= MEASURE_SECONDS else None
        per_find = self.matches_per_find(funnel)
        return dict(kind=self.kind, running=running, stopping=bool(running and (os.path.exists(self.f("STOP")) or live.get("stopping"))),
                    chunks_done=done, units_done=done, unit="chunk", rate=rate,
                    speed_text=("%s seeds/s" % short_count(rate * SEEDS_PER_SPOT)) if rate else None,
                    eta_s=(per_find / rate) if (per_find and rate) else None, eta_known=bool(per_find), eta_kind="find",
                    measuring=bool(running and not rate),
                    funnel=[("matches", matches), ("deep enough", sum(r[4] for r in funnel)), ("checked", sum(r[5] for r in funnel)),
                            ("huts", sum(1 for l in self.lines("results.txt") if l.split() and int(l.split()[0]) <= -self.cfg["min_depth"]))],
                    found=self.found(), best=self.lines("results.txt")[:5], problems=self.lines("problems.txt")[-5:],
                    failed=(self.lines("FAILED") or [None])[0], live=live, outcome=live.get("phase") if not running else None)


class Stopped(Exception):
    """The run was asked to stop while waiting on a tool."""


class GpuScanWorker:
    """One `phase1_scan --worker` process: the CUDA context stays alive between seeds, so a seed costs only its scan."""

    def __init__(self, run):
        self.run = run
        self.log = open(run.f("gpu.log"), "a", encoding="utf-8")
        self.p = subprocess.Popen([tool("phase1_scan"), "--worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                  text=True, bufsize=1, cwd=run.out, creationflags=NO_WINDOW)
        with run.procs_lock:
            run.procs.append(self.p)
        self.wait("===WORKER_READY===")

    def wait(self, token):
        """Read the worker's output until it prints token; its line, or an exception if it errors, dies, or the run is stopped."""
        for line in self.p.stdout:
            line = line.strip()
            if line.startswith("===SCAN_ERROR"):
                raise HuntError("the GPU scanner said: " + line.strip("="))
            if line.startswith(token):
                return line
        if self.run.stopping:
            raise Stopped()
        raise HuntError("the GPU scanner stopped unexpectedly (see %s)" % self.run.f("gpu.log"))

    def scan(self, seed, rect):
        """The candidate regions of one seed: [(seed, chunkX, chunkZ)]. Mostly not huts; lysh decides."""
        out = os.path.join(self.run.out, "cands.csv")
        if os.path.exists(out):
            os.remove(out)
        self.p.stdin.write("--seed %d --min-region-x %d --max-region-x %d --min-region-z %d --max-region-z %d --max-height -50 --out cands.csv\n"
                           % ((seed,) + tuple(rect[:2]) + tuple(rect[2:])))
        self.p.stdin.flush()
        done = self.wait("===SCAN_DONE")
        if "rc=0" not in done:
            raise HuntError("the GPU scanner failed on seed %d (%s)" % (seed, done.strip("=")))
        rows = []
        with open(out, encoding="utf-8") as fh:
            next(fh, None)
            for l in fh:
                p = l.strip().split(",")
                if len(p) >= 3:
                    rows.append((int(p[0]), int(p[1]), int(p[2])))
        return rows

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(5)
        except (OSError, subprocess.SubprocessError, ValueError):
            try:
                self.p.kill()
            except OSError:
                pass
        with self.run.procs_lock:
            if self.p in self.run.procs:
                self.run.procs.remove(self.p)
        self.log.close()


# ---- the scanner: -50 .. -54, CPU or GPU -------------------------------------------------------------------------------------------
class ScanRun(Run):
    kind = "scanner"

    def make_tag(self):
        return "scan%d%s" % (-self.level, "" if self.lower() else "x")

    def lower(self):
        """True when this search means "this deep or deeper". -54 always does: natural terrain stops at -54, so a seed's -55 and
        lower huts (a mineshaft room carved under the hut) would otherwise be missed or mistaken for something shallower."""
        return self.or_lower or self.level <= -54

    def ceiling(self):
        """The y lysh's terrain pass may report. A hut the mineshaft later deepens still shows about -50 to -54 there."""
        return -50 if self.lower() else self.level

    def rect(self):
        """The area as hut regions [rx0, rx1) x [rz0, rz1): the whole world, or a square around 0,0 about `area` blocks out."""
        if self.area <= 0:
            return WORLD[0], WORLD[1], WORLD[0], WORLD[1]
        r = int(self.area // 512) + 1
        return -r, r, -r, r

    def threads(self):
        return max(1, int((os.cpu_count() or 2) * MODES[self.mode]["share"]))

    # the seeds: typed, from a file, or made up as it goes
    def seed_source(self):
        """(seeds, label): a list of typed seeds, or None for the endless made-up stream."""
        typed = parse_seeds(self.seeds_text)
        return (typed, "your seeds") if typed else (None, "made up as it goes")

    def progress(self):
        try:
            with open(self.f("progress.json"), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def scan_seed_cpu(self, seed, on_slab=None):
        """Scan one seed with lysh, one band of the area at a time (so Stop is quick and the bar moves).
        Returns (hits, regions scanned, candidates) with hits as [(x, y, z)]; None when stopped part-way."""
        rx0, rx1, rz0, rz1 = self.rect()
        rows = rz1 - rz0
        cells = (rx1 - rx0) * rows
        bands = max(1, min(64, math.ceil(cells / float(self.cfg["slab_cells"]))))
        step = math.ceil(rows / bands)
        hits, scanned, cand = [], 0, 0
        z = rz0
        k = 0
        while z < rz1:
            if self.stopping:
                return None
            z2 = min(rz1, z + step)
            cmd = [tool("lysh"), "scan", "--seed", str(seed), "--rx0", str(rx0), "--rx1", str(rx1), "--rz0", str(z), "--rz1", str(z2),
                   "--threads", str(self.threads()), "--max-y", "-50", "--phase2-max-y", str(self.ceiling()), "--version", self.cfg["version"],
                   "--list", "--no-stats"]
            with tempfile.TemporaryFile("w+", encoding="utf-8") as buf:
                rc = self.popen(cmd, buf)
                buf.seek(0)
                out = buf.read()
            if self.stopping:
                return None
            if rc != 0:
                raise HuntError("lysh stopped with an error on seed %s: %s" % (seed, out.strip()[-200:]))
            for line in out.splitlines():
                t = line.split()
                if line.startswith("hit ") and len(t) >= 6:
                    hits.append((int(t[3]), int(t[4]), int(t[5])))
                elif line.startswith("  scanned") and t[-1].isdigit():
                    scanned += int(t[-1])
                elif line.startswith("  evaluated") and t[-1].isdigit():
                    cand += int(t[-1])
            k += 1
            z = z2
            if on_slab:
                on_slab(k / float(bands))
        return hits, scanned, cand

    def true_depth(self, seed, hits):
        """[(x, y, z)] with each hut's real depth. lysh sees the terrain only; a hut on a mineshaft room is carved deeper (as low as
        -59), which finish_row reads from the room itself. Those come back as the chunk corner and a y below -54."""
        out = []
        for x, y, z in hits:
            if self.stopping:
                break
            try:
                line = finish_row(seed, x >> 4, z >> 4, 0, 0, 0, 999, run=self.run_tracked)
            except HuntError:
                line = None
            deep = int(line.split()[0]) if line else None
            out.append((16 * (x >> 4), deep, 16 * (z >> 4)) if deep is not None and deep < min(y, -54) else (x, y, z))
        return out

    def keep(self, hits):
        """Only the huts this search asked for: the level or deeper, or exactly the level."""
        return [h for h in hits if (h[1] <= self.level if self.lower() else h[1] == self.level)]

    def finish_seed(self, pos, seed, hits, scanned, cand, t0, typed):
        """Write down one scanned seed: its huts, its line in scanned.txt, and where to carry on from."""
        if self.lower() and hits:
            hits = self.true_depth(seed, hits)
        hits = self.keep(hits)
        with self.lock:
            for x, y, z in hits:
                if y < -54:
                    record_deep(seed, x >> 4, z >> 4, -y, self.tag)
                else:
                    record_scan(seed, x, y, z, self.tag)
                _append_unique(self.f("results.txt"), "%d 0 %s %d %d " % (y, seed, x >> 4, z >> 4), "%d 0 %s %d %d scan" % (y, seed, x >> 4, z >> 4))
            with open(self.f("scanned.txt"), "a", encoding="utf-8") as fh:
                fh.write("%d %d %d %d %.1f %d\n" % (seed, scanned, cand, len(hits), time.time() - t0, int(time.time())))
            atomic_write(self.f("progress.json"), json.dumps(
                {"kind": "list", "sig": self.signature(), "pos": pos + 1} if typed is not None else {"kind": "stream", "next": pos + 1}))

    def signature(self):
        return "%s|%d|%d" % (self.seeds_text.strip(), self.level, self.area)

    def confirm(self, seed, rows):
        """lysh's exact check of the GPU's candidates: [(x, y, z)] of the huts at this level or lower."""
        def one(r):
            _, cx, cz = r
            rc, out = self.run_tracked([tool("lysh"), "hut", "--seed", str(seed), "--x", str(16 * cx), "--z", str(16 * cz), "--max-y", "100",
                                        "--phase2-max-y", str(self.ceiling()), "--version", self.cfg["version"]])
            if "ACCEPT" not in next((l for l in out.splitlines() if "verdict" in l), ""):
                return None
            for l in out.splitlines():
                t = l.split()
                if len(t) == 4 and t[0] == "/tp":
                    return int(t[1]), int(t[2]), int(t[3])
            return None
        with concurrent.futures.ThreadPoolExecutor(max(1, self.threads())) as ex:
            res = list(ex.map(one, rows))
        return sorted({r for r in res if r})

    def seeds_from(self, typed, pos):
        """(position, seed) from pos on: the typed list once through, or the endless scrambled stream."""
        while not self.stopping:
            if typed is not None:
                if pos >= len(typed):
                    return
                yield pos, typed[pos][0]
            else:
                yield pos, seed_of_index(pos)
            pos += 1

    def start_position(self, typed):
        prog = self.progress()
        if typed is not None:
            pos = prog.get("pos", 0) if prog.get("sig") == self.signature() and prog.get("kind") == "list" else 0
            return pos if pos < len(typed) else 0
        pos = prog.get("next") if prog.get("kind") == "stream" else None
        if pos is None:
            pos = self.cfg["seed_start"] if self.cfg["seed_start"] is not None else random.SystemRandom().randrange(1, 1 << 40)
        return pos

    def work(self):
        typed, _ = self.seed_source()
        gpu = MODES[self.mode]["gpu"]
        need(*(("phase1_scan", "lysh") if gpu else ("lysh",)))
        c = self.cfg
        base_found = len(self.found())
        done = 0
        outcome = "finished"
        worker = GpuScanWorker(self) if gpu else None
        ex = concurrent.futures.ThreadPoolExecutor(1)
        pending = None
        stop_now = lambda: self.stopping or (typed is None and c["stop_on_find"] and self.new_finds(base_found) > 0)   # noqa: E731
        try:
            for pos, seed in self.seeds_from(typed, self.start_position(typed)):
                if (c["max_seeds"] and done >= c["max_seeds"]) or stop_now():
                    break
                self.wait_cool()
                t0 = time.time()
                self.set_phase("scanning", "seed %d" % seed, seed=seed, unit_t0=t0, unit_frac=None if gpu else 0.0,
                               unit_expect=self.expect_seed(), seed_index=pos)
                if gpu:
                    rows = worker.scan(seed, self.rect())
                    self.rest(time.time() - t0)
                    if pending:
                        pending.result()
                    if stop_now():
                        break
                    rx0, rx1, rz0, rz1 = self.rect()
                    pending = ex.submit(self.confirm_and_finish, pos, seed, rows, (rx1 - rx0) * (rz1 - rz0), t0, typed)
                else:
                    def on_slab(frac):
                        with self.live_lock:
                            self.live["unit_frac"] = frac
                        self.write_live()
                    res = self.scan_seed_cpu(seed, on_slab)
                    if res is None:
                        break
                    self.finish_seed(pos, seed, res[0], res[1], res[2], t0, typed)
                done += 1
            if pending:
                pending.result()
        except Stopped:
            pass
        finally:
            ex.shutdown(wait=True)
            if worker:
                worker.close()
        if self.stopping:
            outcome = "stopped"
        elif self.new_finds(base_found) > 0:
            outcome = "found"
        return outcome

    def confirm_and_finish(self, pos, seed, rows, cells, t0, typed):
        hits = self.confirm(seed, rows)
        if not self.stopping:                                      # a stopped confirmation is incomplete: scan the seed again next time
            self.finish_seed(pos, seed, hits, cells, len(rows), t0, typed)

    def expect_seed(self):
        rows = [l.split() for l in self.lines("scanned.txt")[-3:] if l.strip()]
        if rows:
            return max(1.0, sum(float(r[4]) for r in rows) / len(rows))
        rx0, rx1, rz0, rz1 = self.rect()
        return max(1.0, (rx1 - rx0) * (rz1 - rz0) / (4.0e6 * self.threads()) * 1.0)

    def found(self):
        return [l for l in self.lines("results.txt") if l.split()]

    def status(self):
        rows = [l.split() for l in self.lines("scanned.txt") if l.strip()]
        seeds = len(rows)
        regions = sum(int(r[1]) for r in rows)
        cands = sum(int(r[2]) for r in rows)
        hits = sum(int(r[3]) for r in rows)
        stamped = [int(r[5]) for r in rows if len(r) > 5]
        recent = rows[-5:]
        if len(stamped) >= 2 and stamped[-1] > stamped[-min(6, len(stamped))]:
            k = min(5, len(stamped) - 1)
            spm = 60.0 * k / (stamped[-1] - stamped[-1 - k])          # seeds a minute, from the spacing of finished seeds
        else:
            spm = (60.0 * len(recent) / sum(float(r[4]) for r in recent)) if recent and sum(float(r[4]) for r in recent) > 0 else None
        live = self.read_live()
        running = self.alive()
        begun = live.get("started") or self.started
        this_run = [r for r in rows if len(r) > 5 and int(r[5]) > begun]
        sec_per_seed = None                                       # seconds a seed takes here, counted from the start of this run
        if this_run and running:
            span = max(float(live.get("now") or time.time()), float(this_run[-1][5])) - begun
            sec_per_seed = span / len(this_run) if span >= MEASURE_SECONDS or len(this_run) >= MEASURE_CHUNKS else None
        elif spm:
            sec_per_seed = 60.0 / spm
        typed = parse_seeds(self.seeds_text)
        pos = self.progress().get("pos", 0) if typed else 0
        left = max(0, len(typed) - pos) if typed else None
        eta = left * sec_per_seed if (left is not None and sec_per_seed and running) else None
        return dict(kind=self.kind, running=running, stopping=bool(running and (os.path.exists(self.f("STOP")) or live.get("stopping"))),
                    units_done=seeds, unit="seed", rate=spm, speed_text=("%.1f seeds/min" % spm) if spm else None,
                    sec_per_seed=sec_per_seed, eta_s=eta, eta_known=eta is not None, eta_kind="finish", measuring=bool(running and typed and eta is None),
                    funnel=[("regions", regions), ("candidates", cands), ("huts", hits)],
                    found=self.found(), best=self.found()[:5], problems=self.lines("problems.txt")[-5:],
                    failed=(self.lines("FAILED") or [None])[0], live=live, outcome=live.get("phase") if not running else None)


def sync_catalog():
    """After a find, put the new -55 and deeper huts into the public list (a checkout of kyoubitt/low-y-witch-huts), in the background.
    Only when a checkout is set up: WITCHHUT_CATALOG names it, else ~/src/low-y-witch-huts. Nobody else's copy pushes anywhere."""
    repo = os.environ.get("WITCHHUT_CATALOG") or os.path.join(os.path.expanduser("~"), "src", "low-y-witch-huts")
    script = os.path.join(ROOT, "tools", "export_catalog.py")
    if getattr(sys, "frozen", False) or not os.path.isdir(os.path.join(repo, ".git")) or not os.path.isfile(script):
        return
    try:
        subprocess.Popen([sys.executable, "-I", script, repo, "--commit", "--push", "--readme", os.path.join(ROOT, "README.md")],
                         stdin=subprocess.DEVNULL, stdout=open(os.path.join(RUNS, "catalog.log"), "a"), stderr=subprocess.STDOUT,
                         cwd=ROOT, start_new_session=True)
    except OSError:
        pass


# ---- opening runs ----------------------------------------------------------------------------------------------------------------
def make_run(level, mode, seeds="", area=0, **cfg):
    if level in SOLVER_LEVELS:
        return SolveRun(level, mode, seeds, area, **cfg)
    return ScanRun(level, mode, seeds, area, **cfg)


def run_from_tag(tag, **cfg):
    """The run saved in runs/<tag>/settings.json, or None."""
    try:
        with open(os.path.join(RUNS, tag, "settings.json"), encoding="utf-8") as f:
            s = json.load(f)
        return make_run(int(s["level"]), s["mode"], s.get("seeds", ""), s.get("area", 0), or_lower=s.get("or_lower", True), **cfg)
    except (OSError, ValueError, KeyError, HuntError):
        return None


def running():
    """The run that is going right now (any level), or None."""
    if not os.path.isdir(RUNS):
        return None
    for tag in sorted(os.listdir(RUNS)):
        if os.path.isfile(os.path.join(RUNS, tag, "driver.pid")):
            r = run_from_tag(tag)
            if r and r.alive():
                return r
    return None


def short_count(n):
    for lim, suf in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if n >= lim:
            return ("%.0f%s" if n >= lim * 10 else "%.1f%s") % (n / lim, suf)
    return "%d" % n


# ---- the self-test: the known reference huts must come out exactly as the project recorded them ---------------------------------
def selftest_scan(say=print):
    """The scanner on the project's reference seed: it must report the one hut at /tp -15792 -54 -17168."""
    try:
        need("lysh")
    except HuntError as e:
        say("FAIL  %s" % e)
        return False
    ref = SCAN_REFERENCE
    r = ScanRun(-54, "cpu", str(ref["seed"]), 40000)
    res = r.scan_seed_cpu(ref["seed"])
    ok = bool(res) and ref["hit"] in res[0]
    say(("ok    " if ok else "FAIL  ") + "the scanner finds the reference hut (seed %d: /tp %d %d %d)" % ((ref["seed"],) + ref["hit"]))
    return ok


def selftest(cpu_only=False, say=print):
    """Runs the project's own checks on the solver side. Returns True when all pass."""
    bad = []

    def ok(msg):
        say("ok    " + msg)

    def fail(msg):
        say("FAIL  " + msg)
        bad.append(msg)
    try:
        need("mstree", "lysh", "msroom", "skyscan", "biome_at", "terrain_dump")
    except HuntError as e:
        fail(str(e))
        return False
    ref = REFERENCE
    line = finish_row(ref["seed"], ref["cx"], ref["cz"], ref["layout"], ref["hi"], ref["lo"], 999)
    if line and int(line.split()[0]) <= -58 and "/tp -29646048 -58 -22084400" in line:
        ok("finisher accepts the -58 reference hut")
    else:
        fail("finisher did not reproduce the -58 reference hut: %r" % line)
    with tempfile.TemporaryDirectory() as d:
        spots = os.path.join(d, "ref.txt")
        with open(spots, "w") as fh:
            fh.write("%d %d %d\n" % (ref["seed"], ref["cx"], ref["cz"]))
        try:
            ex = oracle(spots)
        except HuntError as e:
            fail("oracle: %s" % e)
            ex = []
        row = parse_exact(ex[0]) if ex else None
        if row and row["hutY"] == -58 and row["air"] == 274 and row["room"] == "room+605" and row["nb"] == "2":
            ok("oracle reads the reference hut at -58 (air 274, room+605, 2 neighbour starts)")
        else:
            fail("oracle did not read the reference hut: %r" % (ex[:1],))
        res = make_results(ex)
        rd = os.path.join(d, "results")
        record(res, "selftest", rd, -58, 55)
        want = "3159335117276934358 -1852878 -1380275 /tp -29646048 -58 -22084400"
        f58 = os.path.join(rd, "58.txt")
        if os.path.isfile(f58) and open(f58).read().startswith(want):
            ok("results/58.txt receives the reference hut")
        else:
            fail("results file not written as expected")
        if not cpu_only:
            try:
                need("hut_lookup_v4")
            except HuntError as e:
                fail(str(e))
                return False
            name, free = gpu_free_mib()
            if not name:
                fail("no NVIDIA GPU: the GPU checks can't run")
                return False
            if minecraft_running():
                fail("Minecraft is running; close it before the GPU checks")
                return False
            r = SolveRun(-58, "gpu")
            sp = os.path.join(d, "spots4.txt")
            open(sp, "w").write("\n".join(TERRAIN_REFERENCE) + "\n")
            out = os.path.join(d, "surv4.csv")
            rc, _ = run_text(r.terrain_cmd(sp, out, DEFAULTS["batch"]), timeout=900)
            n = len(open(out).read().splitlines()) - 1 if os.path.isfile(out) else -1
            if n == 4:
                ok("terrain stage keeps all 4 reference huts")
            else:
                fail("terrain stage kept %s of the 4 reference huts" % n)
    say("SELFTEST OK" if not bad else "SELFTEST: %d problem(s)" % len(bad))
    return not bad


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hunt.py", description="the low-Y witch hut finder")
    ap.add_argument("cmd", choices=["run", "stop", "status", "preflight", "selftest", "scan", "tune"])
    ap.add_argument("arg", nargs="?", default="")
    ap.add_argument("--tag", default="")
    ap.add_argument("--level", type=int, default=-58)
    ap.add_argument("--mode", default="gpu", choices=sorted(MODES))
    ap.add_argument("--seed", default="")
    ap.add_argument("--area", type=int, default=0)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "selftest":
            if a.arg == "scan":
                return 0 if selftest_scan() else 1
            return 0 if selftest(cpu_only=(a.arg == "cpu")) and selftest_scan() else 1
        if a.cmd == "scan":
            r = ScanRun(a.level if a.level > -55 else -54, a.mode if not MODES[a.mode]["gpu"] else "cpu", a.seed, a.area)
            seeds = parse_seeds(a.seed) or [(seed_of_index(random.randrange(1, 1 << 40)), None)]
            for seed, _ in seeds:
                res = r.scan_seed_cpu(seed)
                print("seed %d: %s" % (seed, ", ".join("/tp %d %d %d" % h for h in res[0]) if res and res[0] else "no hut"))
            return 0
        if a.cmd == "run":
            r = run_from_tag(a.tag) if a.tag else make_run(a.level, a.mode, a.seed, a.area)
            if r is None:
                print("no saved settings for %r" % a.tag)
                return 1
            bad = [t for ok, t, _ in r.preflight() if not ok]
            if bad:
                print("can't start:", bad[0])
                return 1
            return r.run()
        r = (run_from_tag(a.tag) if a.tag else None) or make_run(a.level, a.mode, a.seed, a.area)
        if a.cmd == "stop":
            r.stop(now=(a.arg == "now"))
            print("stop requested" if a.arg != "now" else "killed")
            return 0
        if a.cmd == "preflight":
            rows = r.preflight()
            for ok, t, _ in rows:
                print(("ok    " if ok else "FAIL  ") + t)
            return 0 if all(ok for ok, _, _ in rows) else 1
        if a.cmd == "status":
            print(json.dumps(r.status(), indent=1))
            return 0
        if a.cmd == "tune":
            r = SolveRun(-58, "gpu")
            r.sample_gpu()
            print(json.dumps(r.tune(), indent=1))
            return 0
    except HuntError as e:
        print("ERROR:", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
