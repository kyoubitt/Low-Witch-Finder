#!/usr/bin/env python3
"""test_hunt.py - the hunt driver end to end, with a STUB for the GPU engine (hut_lookup_v4).

The stub answers like the real engine on the reference hut: the solver lists it as a match, the terrain stage keeps it.
Everything else is real: the CPU finisher tools, the Java oracle on Mojang's jar, the CPU scanner (lysh), the results writer,
resume, random start, Stop, the GPU temperature guard. It proves the orchestration without a GPU. It does NOT prove the GPU engine."""
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time

ROOT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
sys.path.insert(0, ROOT)
import hunt as H  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("ok    " if ok else "FAIL  ") + name + (("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


STUB = r'''#!/usr/bin/env python3
import os, sys, time
a = sys.argv[1:]
log = os.environ["STUB_LOG"]
open(log, "a").write(" ".join(a) + "\n")
open(log + ".pid", "a").write(str(os.getpid()) + "\n")
mode = os.environ.get("STUB_MODE", "ok")
get = lambda k: a[a.index(k) + 1]
REF = "3159335117276934358,-1852878,-1380275,254754461452089,329,323"
ref_group = int(os.environ.get("STUB_REF_GROUP", "5"))
if "--solve-only" in a:
    if mode == "solver_fail":
        sys.exit(2)
    first, count = int(get("--first-index")), int(get("--count"))
    with open(get("--dump-hits"), "w") as f:
        if os.environ.get("STUB_MANY"):
            for k in range(count * 1000):
                f.write("%d,%d,%d,254754461452089,300,300\n" % (1000 + k, k, k))
        elif first <= ref_group < first + count:
            f.write(REF + "\n")
            f.write("1,5,5,254754461452089,300,300\n")
    sys.exit(0)
if "--spots" in a:
    spots = [l.split() for l in open(get("--spots"))]
    time.sleep(float(os.environ.get("STUB_TERRAIN_SLEEP", "0")))
    if os.environ.get("STUB_MANY"):
        time.sleep(0.3 + len(spots) / 20000.0 * (0.5 if get("--batch") == "16" else 1.0))
    with open(get("--out"), "w") as f:
        f.write("seed,chunkX,chunkZ,layoutX,airHi,airLo\n")
        for s in spots:
            if s[0] == "3159335117276934358":
                f.write(REF + "\n")
    sys.exit(0)
sys.exit(3)
'''


def make_env():
    tmp = tempfile.mkdtemp()
    b = os.path.join(tmp, "bin")
    os.makedirs(b)
    for n in os.listdir(ROOT + "/bin"):
        if n != "hut_lookup_v4":
            os.symlink(os.path.join(os.path.realpath(ROOT + "/bin"), n), os.path.join(b, n))
    stub = os.path.join(b, "hut_lookup_v4")
    open(stub, "w").write(STUB.replace("#!/usr/bin/env python3", "#!" + sys.executable))
    os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
    H.BIN, H.RUNS, H.RESULTS = b, os.path.join(tmp, "runs"), os.path.join(tmp, "results")
    for k in ("STUB_MODE", "STUB_MANY", "STUB_TERRAIN_SLEEP"):
        os.environ.pop(k, None)
    os.environ["STUB_LOG"] = os.path.join(tmp, "stub.log")
    return tmp


def calls(tmp, what):
    p = os.path.join(tmp, "stub.log")
    return sum(1 for l in open(p) if what in l) if os.path.exists(p) else 0


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


QUICK = dict(limit=512, chunk=256, start=0, tune=False, solver_retries=1, terrain_retries=1, retry_wait=0, terrain_wait=0)

# ---- a run of two chunks -------------------------------------------------------------------------------------------------
tmp = make_env()
r = H.SolveRun(-58, "gpu", stop_on_find=0, **QUICK)
code = r.run()
check("a run of two chunks finishes with exit 0", code == 0, code)
check("both chunks are marked done", sorted(r.lines("done.txt")) == ["0 256", "256 256"], r.lines("done.txt"))
check("the solver ran once per chunk", calls(tmp, "--solve-only") == 2, calls(tmp, "--solve-only"))
check("the terrain stage ran only for the chunk that had matches", calls(tmp, "--spots") == 1, calls(tmp, "--spots"))
f = sorted(r.lines("funnel.txt"))
check("the funnel counts match -> survivor -> accepted -> exact", f and f[0].split()[:7] == ["0", "256", "2", "2", "1", "1", "1"], f)
check("results.txt has the -58 reference hut with the oracle's numbers",
      r.lines("results.txt") == ["-58 274 3159335117276934358 -1852878 -1380275 nb=2 room+605"], r.lines("results.txt"))
rec = os.path.join(H.RESULTS, "58.txt")
check("results/58.txt gets the /tp line", os.path.isfile(rec) and open(rec).read().startswith("3159335117276934358 -1852878 -1380275 /tp -29646048 -58 -22084400"))
check("results/58_seeds.txt gets the short line", "3159335117276934358   /tp -29646045 -56 -22084393" in open(os.path.join(H.RESULTS, "58_seeds.txt")).read())
left = [n for n in os.listdir(r.out) if n.startswith(("hits_", "spots_", "surv_", "exact_in_", "solver_"))]
check("a chunk's working files are deleted when it is done (no leak)", not left, left)
check("the driver pid file is removed when it ends", not os.path.exists(r.f("driver.pid")))
live = r.read_live()
check("live.json ends with the outcome: found a hut", live.get("phase") == "found" and live.get("pid"), live)
st = r.status()
check("status sees the find", st["found"] and st["chunks_done"] == 2 and st["funnel"][3] == ("huts", 1), st)
check("status carries the outcome", st["outcome"] == "found" and st["unit"] == "chunk")
check("all_results lists it with its in-hut position", H.all_results()[0]["x"] == -29646045 and H.all_results()[0]["y"] == -56)
check("the settings are saved for the app and the detached driver", json.load(open(r.f("settings.json")))["level"] == -58)

n_before = calls(tmp, "--solve-only")
r2 = H.SolveRun(-58, "gpu", stop_on_find=0, **QUICK)
check("resuming a finished run redoes nothing", r2.run() == 0 and calls(tmp, "--solve-only") == n_before)

# stop at the first find
tmp = make_env()
r = H.SolveRun(-58, "gpu", **QUICK)
code = r.run()
check("a find ends the run (stop_on_find)", code == 0 and r.read_live().get("phase") == "found", r.read_live().get("phase"))
check("... after the chunk that found it", r.lines("done.txt") == ["0 256"], r.lines("done.txt"))
r = H.SolveRun(-57, "gpu", **QUICK)
check("a level only counts finds at that depth or lower: -57 is satisfied by the -58 hut already recorded",
      len(r.found()) == 1)
r = H.SolveRun(-59, "gpu", **QUICK)
check("... and -59 is not", r.found() == [])

# ---- a random start, remembered; the order wraps; the other layout of -59 is searched -----------------------------------------
tmp = make_env()
r = H.SolveRun(-58, "gpu", limit=1 << 20, chunk=64, tune=False)
s = r.start_group(64)
check("the start is chunk-aligned and skips the first eighth", s % 64 == 0 and s >= (1 << 20) // 8, s)
check("it is remembered for the next start", H.SolveRun(-58, "gpu", limit=1 << 20, chunk=64, tune=False).start_group(64) == s)
order = list(r.chunks())
check("the order starts at the start, wraps, and covers every chunk once",
      order[0] == (s, 64) and len(order) == (1 << 20) // 64 and len({o[0] for o in order}) == len(order) and (0, 64) in order)
r.new_spot()
check("New spot forgets the remembered start", not os.path.exists(r.f("start.txt")))
a = H.SolveRun(-58, "gpu", seeds="42", limit=1 << 20, chunk=64, tune=False).start_group(64)
b = H.SolveRun(-58, "gpu", seeds="42", limit=1 << 20, chunk=64, tune=False).start_group(64)
c = H.SolveRun(-58, "gpu", seeds="43", limit=1 << 20, chunk=64, tune=False).start_group(64)
check("a typed number always gives the same start (and another number another one)", a == b and a != c, (a, b, c))
r59 = H.SolveRun(-59, "gpu", tune=False)
check("-59 has two layouts, so the space is two bundles", r59.bundles() == 2 and r59.span() == 2 << 20)
check("... and the chunks reach into the second bundle", any(i >= 1 << 20 for i, _ in r59.chunks()))
check("-58 has one bundle", H.SolveRun(-58, "gpu").span() == 1 << 20)
check("done ranges from another chunk size still count", H.covered(H.merge_ranges([(0, 256), (256, 512)]), 64, 64) and not H.covered([(0, 100)], 64, 64))

# ---- Stop is quick -----------------------------------------------------------------------------------------------------------
tmp = make_env()
os.environ["STUB_TERRAIN_SLEEP"] = "60"
r = H.SolveRun(-58, "gpu", stop_on_find=0, **QUICK)
res = {}
th = threading.Thread(target=lambda: res.update(code=r.run()))
th.start()
t0 = time.time()
while calls(tmp, "--spots") == 0 and time.time() - t0 < 30:
    time.sleep(0.1)
check("the terrain stage is running (the stub is sleeping for a minute)", calls(tmp, "--spots") == 1)
t1 = time.time()
r.stop()
th.join(30)
took = time.time() - t1
check("Stop ends the run within a few seconds, not after the chunk (took %.1f s)" % took, not th.is_alive() and took < 6, took)
check("... with exit 0 and the outcome 'stopped'", res.get("code") == 0 and r.read_live().get("phase") == "stopped", (res, r.read_live().get("phase")))
check("... nothing half-done is marked done", r.lines("done.txt") == [], r.lines("done.txt"))
time.sleep(0.3)
pids = [int(x) for x in open(os.environ["STUB_LOG"] + ".pid").read().split()]
check("... and the engine process is really gone", not any(pid_alive(p) for p in pids), pids)
check("... no FAILED file for a stop", not os.path.exists(r.f("FAILED")))
os.environ.pop("STUB_TERRAIN_SLEEP")
r = H.SolveRun(-58, "gpu", stop_on_find=0, **QUICK)
check("starting again redoes the cut-short chunk and finishes", r.run() == 0 and sorted(r.lines("done.txt")) == ["0 256", "256 256"], r.lines("done.txt"))

# ---- a failing solver aborts with a reason -----------------------------------------------------------------------------------
tmp = make_env()
os.environ["STUB_MODE"] = "solver_fail"
r = H.SolveRun(-58, "gpu", **dict(QUICK, solver_retries=2))
code = r.run()
check("a failing solver ends the run with exit 1", code == 1, code)
check("...writes FAILED naming the chunk", r.lines("FAILED") and "chunk 0" in r.lines("FAILED")[0], r.lines("FAILED"))
check("...logs each attempt", len(r.lines("problems.txt")) == 2, r.lines("problems.txt"))
check("...and nothing is marked done", r.lines("done.txt") == [])
check("status shows the failure", r.status()["failed"] and "chunk 0" in r.status()["failed"])
os.environ["STUB_MODE"] = "ok"

# ---- a crash is logged, not silent -------------------------------------------------------------------------------------------
tmp = make_env()
r = H.SolveRun(-58, "gpu", **QUICK)
orig = H.SolveRun.work


def boom(self):
    raise RuntimeError("boom")


H.SolveRun.work = boom
err = tempfile.TemporaryFile("w+")
real_stderr, sys.stderr = sys.stderr, err
try:
    code = r.run()
finally:
    sys.stderr = real_stderr
    H.SolveRun.work = orig
err.seek(0)
check("a crash ends with exit 1 and a FAILED reason", code == 1 and "boom" in (r.lines("FAILED") or [""])[0], r.lines("FAILED"))
check("... the traceback is printed for driver.log", "RuntimeError: boom" in err.read())
check("... the pid file is still cleaned up", not os.path.exists(r.f("driver.pid")))

# ---- the GPU temperature guard and GPU gentle -------------------------------------------------------------------------------------
tmp = make_env()
r = H.SolveRun(-58, "gpu", **QUICK)
seq = iter([90, 86, 83, 81, 70])
H.gpu_stats = lambda: dict(name="stub", temp=next(seq), util=0, mem_used=0, mem_total=6000, watts=None)
states = []
for _ in range(5):
    r.sample_gpu()
    states.append(r.hot)
check("above 88 it holds off, and only resumes below 82 (hysteresis)", states == [True, True, True, False, False], states)
r.hot = True
threading.Timer(0.7, lambda: setattr(r, "hot", False)).start()
t0 = time.time()
r.wait_cool()
check("wait_cool waits while hot, then goes on", 0.6 < time.time() - t0 < 4, time.time() - t0)
waits = []
g = H.SolveRun(-58, "gpu-gentle", **QUICK)
g.stop_evt.wait = lambda t=None: waits.append(t)
g.rest(10)
f_ = H.SolveRun(-58, "gpu", **QUICK)
f_.stop_evt.wait = lambda t=None: waits.append(("full", t))
f_.rest(10)
check("GPU gentle idles as long as it worked (about half duty); GPU full never idles", waits == [10.0], waits)

# ---- tuning picks a batch and a chunk, once ---------------------------------------------------------------------------------------
tmp = make_env()
os.environ["STUB_MANY"] = "1"
r = H.SolveRun(-58, "gpu", **dict(QUICK, tune=True, limit=None, start=None))
r.gpu = dict(name="Stub GPU", temp=50)
t = r.tune()
check("tuning returns a batch from 16/32/64 and a power-of-two chunk of 16..1024",
      t and t["batch"] in (16, 32, 64) and 16 <= t["chunk"] <= 1024 and (t["chunk"] & (t["chunk"] - 1)) == 0, t)
check("... it preferred the clearly faster batch (the stub is faster at 16)", t and t["batch"] == 16, t)
check("... and saved it per GPU", json.load(open(os.path.join(H.RUNS, "tuned.json")))["Stub GPU"]["chunk"] == t["chunk"])
check("... then tuned_for finds it without timing again", r.tuned_for() is not None)
before = calls(tmp, "--solve-only")
check("... and tune() cleans up after itself", not os.path.exists(os.path.join(r.out, "tune")))
os.environ.pop("STUB_MANY")

# ---- the CPU scanner (real lysh) -----------------------------------------------------------------------------------------------
tmp = make_env()
REF_SEED = H.SCAN_REFERENCE["seed"]
check("the project's reference seed gives its one -54 hut", H.selftest_scan(lambda s: None))
r = H.ScanRun(-54, "cpu", str(REF_SEED), 40000, stop_on_find=1)
code = r.run()
check("a scan of the typed seed ends with exit 0 and the outcome found", code == 0 and r.read_live().get("phase") == "found", r.read_live().get("phase"))
line = open(os.path.join(H.RESULTS, "54.txt")).read().strip()
check("results/54.txt gets the hut with the project's /tp", line.startswith("%d -987 -1073 /tp -15792 -54 -17168" % REF_SEED), line)
ar = H.all_results()[0]
check("all_results lists it", (ar["depth"], ar["x"], ar["y"], ar["z"]) == (54, -15792, -54, -17168), ar)
st = r.status()
check("status: one seed, one hut, a speed", st["units_done"] == 1 and st["funnel"][2] == ("huts", 1) and st["found"], st)
r = H.ScanRun(-54, "cpu", str(REF_SEED), 40000)
r.run()
check("scanning the same seed again doesn't duplicate the record", open(os.path.join(H.RESULTS, "54.txt")).read().count("\n") == 1)
check("a smaller area leaves the hut out", H.ScanRun(-54, "cpu", "", 5000).scan_seed_cpu(REF_SEED)[0] == [])
check("the whole-world rectangle is the +-30M world", H.ScanRun(-54, "cpu").rect() == (-58594, 58594, -58594, 58594))
check("an area of 20,000 blocks is +-40 regions", H.ScanRun(-54, "cpu", "", 20000).rect() == (-40, 40, -40, 40))
check("CPU gentle uses half the cores", H.ScanRun(-54, "cpu-gentle").threads() == max(1, (os.cpu_count() or 2) // 2))

# made-up seeds: the scrambled stream from a random (here fixed) start, remembered
tmp = make_env()
r = H.ScanRun(-54, "cpu", "", 5000, seed_start=7, max_seeds=3, stop_on_find=0)
code = r.run()
rows = [l.split() for l in r.lines("scanned.txt")]
check("made-up seeds: three seeds from the scrambled stream", code == 0 and [int(x[0]) for x in rows] == [H.seed_of_index(i) for i in (7, 8, 9)], rows)
check("... the next index is remembered", json.load(open(r.f("progress.json")))["next"] == 10)
r = H.ScanRun(-54, "cpu", "", 5000, seed_start=999, max_seeds=2, stop_on_find=0)
r.run()
check("... and the next start carries on from it, not from the start index", [int(l.split()[0]) for l in r.lines("scanned.txt")][-2:] == [H.seed_of_index(10), H.seed_of_index(11)])
check("the scrambler is the project's (index * 6364136223846793005 mod 2^64)", H.seed_of_index(6) == 1291329195661654798)

# a typed list is scanned to the end, even after a hit
tmp = make_env()
open(os.path.join(tmp, "seeds.txt"), "w").write("# my seeds\n%d\n5 6\nhello\n" % REF_SEED)
check("a file of seeds is read: numbers, several to a line, words",
      [s for s, _ in H.parse_seeds(os.path.join(tmp, "seeds.txt"))] == [REF_SEED, 5, 6, 99162322])
r = H.ScanRun(-54, "cpu", os.path.join(tmp, "seeds.txt"), 40000)
r.run()
check("every seed of the list is scanned, even after a hit", len(r.lines("scanned.txt")) == 4, r.lines("scanned.txt"))

# Stop on the scanner is quick too: a whole-world scan takes minutes
tmp = make_env()
r = H.ScanRun(-54, "cpu", str(REF_SEED), 0)
res = {}
th = threading.Thread(target=lambda: res.update(code=r.run()))
th.start()
time.sleep(2.5)
t1 = time.time()
r.stop()
th.join(30)
took = time.time() - t1
check("Stop ends a whole-world scan within seconds (took %.1f s)" % took, not th.is_alive() and took < 8 and res.get("code") == 0, took)
check("... the seed is not counted as scanned", r.lines("scanned.txt") == [])
live = r.read_live()
check("... the live file reported the bar moving by bands", "unit_frac" in live or live.get("phase") == "stopped", live)

# ---- what can be asked ----------------------------------------------------------------------------------------------------------
check("a CPU mode can't do -55 and deeper, and says why", "NVIDIA" in (H.combo_problem(-58, "cpu") or "") and H.combo_problem(-54, "cpu") is None)
check("every level has a kind", all((H.make_run(l, "gpu").kind == "solver") == (l <= -55) for l in H.LEVELS))
check("the first-run default is the GPU search when there is a card and engine, else the CPU scanner",
      H.default_settings(dict(gpu={"name": "x"}, gpu_engine=True))["mode"] == "gpu" and H.default_settings(dict(gpu=None, gpu_engine=False))["mode"] == "cpu")

print("\n%s" % ("all passed" if not fails else "%d FAILED" % len(fails)))
sys.exit(1 if fails else 0)
