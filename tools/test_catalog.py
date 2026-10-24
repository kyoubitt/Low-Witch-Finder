#!/usr/bin/env python3
"""test_catalog.py - the public list: a new deep hut is ranked, written, committed with the credit line, and a stopped run still triggers the sync.
The game's own check (the oracle) is replaced by a stub, and the catalog is a throwaway git repo."""
import json
import os
import subprocess
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix="witchhut-catalog-")
os.environ["WITCHHUT_RUNS"], os.environ["WITCHHUT_RESULTS"] = tmp + "/runs", tmp + "/results"
ROOT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import hunt as H  # noqa: E402
import witchhut_common as W  # noqa: E402
import export_catalog as E  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("ok    " if ok else "FAIL  ") + name + (("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


repo = tmp + "/catalog"
subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "start"], check=True)
open(repo + "/README.md", "w").write("# x\n\n<!-- huts:start -->\n<!-- huts:end -->\n")
finder = tmp + "/finder-README.md"
open(finder, "w").write("# f\n<!-- huts:start -->\n<!-- huts:end -->\n")

# two deep huts and one shallow one as hunts would record them; the oracle stub reads each at its claimed depth
os.makedirs(tmp + "/results")
H.record_deep("111", 1000, 2000, 59, "test")
H.record_deep("222", 10, 20, 57, "test")
H.record_scan(333, 5, -53, 5, "test")                       # -53 is not a deep hut: never listed
W.load_finds = lambda path=None: []
H.need = lambda *a: None
H.oracle = lambda spots, threads=8, ymax=128, run=None: [
    "S %s x %s %s 0 0 0 0 0 %d" % (l.split()[0], l.split()[1], l.split()[2], {"111": -59, "222": -57}[l.split()[0]]) for l in open(spots)]
H.parse_exact = lambda line: dict(hutY=int(line.split()[-1]), air=0, seed=line.split()[1], cx=int(line.split()[3]), cz=int(line.split()[4]))
sys.argv = ["export_catalog.py", repo, "--commit", "--readme", finder]
E.main()
d = json.load(open(repo + "/data/summary.json"))
check("only the deep huts are listed", d["total"] == 2 and d["y59"] == 1 and d["y57"] == 1, d)
rank = open(repo + "/data/ranked.tsv").read().split("\n")
check("ranked deepest first", rank[1].split("\t")[:2] == ["1", "-59"] and rank[2].split("\t")[:2] == ["2", "-57"], rank[:3])
check("RANKING.md and the README top 10 exist", "| 1 | **-59**" in open(repo + "/RANKING.md").read() and "### Top 10" in open(repo + "/README.md").read())
check("the finder's README gets the live badges, not a table", "img.shields.io" in open(finder).read() and "| Depth |" not in open(finder).read())
log = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%s%n%b"], capture_output=True, text=True).stdout
check("committed with the credit line", log.startswith("Add 2 huts") and "Co-authored-by: xvyz1" in log and "Claude" not in log, log)
check("nothing is committed twice", E.commit(repo, 0, False) == "nothing new to commit")

# same hut again: nothing new
E.main()
n = subprocess.run(["git", "-C", repo, "rev-list", "--count", "HEAD"], capture_output=True, text=True).stdout.strip()
check("a second sync with nothing new adds no commit", n == "2", n)

# the sync runs for any run that ends, and says so
ran = []
H.subprocess.Popen = lambda cmd, **kw: ran.append(cmd)
os.environ.pop("WITCHHUT_CATALOG", None)
H.sync_catalog()
check("a run with its own folders never syncs to the default checkout", not ran, ran)
os.environ["WITCHHUT_CATALOG"] = repo
H.sync_catalog()
check("a stopped run still starts the sync", len(ran) == 1 and "--commit" in ran[0] and "--push" in ran[0], ran)
os.makedirs(H.RUNS, exist_ok=True)
open(H.RUNS + "/catalog.log", "w").write("kept 2\npushed\n")
check("the Hunt tab can tell the person", H.catalog_status()[1].startswith("Sent the new huts"))
open(H.RUNS + "/catalog.log", "w").write("kept 2\nnothing new to commit\n")
check("nothing new says nothing", H.catalog_status() is None)

print("\n%s" % ("all passed" if not fails else "%d FAILED" % len(fails)))
sys.exit(1 if fails else 0)
