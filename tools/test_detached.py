#!/usr/bin/env python3
"""test_detached.py - starts real hunts the way the app does (a detached hunt.py process) and stops them from outside.
CPU scanner only, so it needs no GPU. Runs and results go to a temporary folder."""
import json
import os
import subprocess
import sys
import tempfile
import time

tmp = tempfile.mkdtemp(prefix="witchhut-detached-")
os.environ["WITCHHUT_RUNS"], os.environ["WITCHHUT_RESULTS"] = tmp + "/runs", tmp + "/results"
ROOT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
sys.path.insert(0, ROOT)
import hunt as H  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("ok    " if ok else "FAIL  ") + name + (("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def wait(cond, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        if cond():
            return True
        time.sleep(0.1)
    return False


def lysh_running():
    return subprocess.run(["pgrep", "-x", "lysh"], capture_output=True).returncode == 0 if os.name != "nt" else False


REF = H.SCAN_REFERENCE["seed"]
r = H.make_run(-54, "cpu", str(REF), 40000)
r.start_detached()
check("the driver starts as its own process", wait(lambda: r.alive() or os.path.exists(r.f("scanned.txt")), 15))   # a small area can finish before the first look
check("... it finishes by itself, having found the hut", wait(lambda: not r.alive() and r.read_live().get("phase") == "found", 60), r.read_live())
check("... results/54.txt has it", "/tp -15792 -54 -17168" in open(tmp + "/results/54.txt").read())
st = r.status()
check("... the app's view of it: not running, found, one seed", not st["running"] and st["outcome"] == "found" and st["units_done"] == 1, st)
check("... the driver log is empty or tells how it ended", "driver finished: found" in open(r.f("driver.log")).read())
other = H.make_run(-53, "cpu", "", 5000)
r2 = H.make_run(-54, "cpu", "", 0)
r2.start_detached()
check("a whole-world scan runs", wait(lambda: r2.alive() and r2.read_live().get("phase") == "scanning", 15), r2.read_live())
try:
    other.start_detached()
    check("a second hunt is refused while one runs", False)
except H.HuntError as e:
    check("a second hunt is refused while one runs", "already running" in str(e), str(e))
check("the running hunt is found from its folder (what the app does on start)", H.running() is not None and H.running().level == -54)
time.sleep(1.5)
t0 = time.time()
r2.stop()
ended = wait(lambda: not r2.alive(), 20)
took = time.time() - t0
check("Stop from outside ends it in seconds (took %.1f s)" % took, ended and took < 8, took)
time.sleep(0.3)
check("... the scanner process is gone too", not lysh_running())
check("... and it says 'stopped'", r2.read_live().get("phase") == "stopped", r2.read_live())
r3 = H.make_run(-54, "cpu", "", 5000, max_seeds=2)
st = r3.status()
check("... the unfinished seed was not counted", H.make_run(-54, "cpu").status()["units_done"] == 1)

print("\n%s" % ("all passed" if not fails else "%d FAILED" % len(fails)))
sys.exit(1 if fails else 0)
