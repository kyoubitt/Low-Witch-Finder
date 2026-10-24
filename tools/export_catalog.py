#!/usr/bin/env python3
"""export_catalog.py - writes the public list of low witch huts (-55 and deeper) after the game's own code has re-read each one.

  python3 tools/export_catalog.py OUT_DIR                          our finds: results/*.txt and data/finds.tsv
  python3 tools/export_catalog.py OUT_DIR --import FILE.jsonl ...  also another list's huts, as candidates (a line is {"seed","x","z"};
                                                                   the depth is read from a file name like y-59.jsonl)

Every candidate goes through the oracle (the game's 1.21.4 world generation, hunt.oracle); only the ones that really come out at -55
or deeper are written, at the depth the oracle read, so a wrong claim never gets in. The rest go to unconfirmed.tsv.
What is already in OUT_DIR is kept and not asked again. Needs the Minecraft files (mcfiles.py) and Java 21.

  --commit [--push]   also commit what changed in OUT_DIR (a git checkout of the catalog repo) and push it
  --readme FILE ...   refresh the counts table between <!-- huts:start --> and <!-- huts:end --> in these files too
                      (OUT_DIR/README.md always; the finder's own README.md when it has the markers)

The hunt driver runs this by itself after a run that found something, when a catalog checkout is set up (hunt.sync_catalog)."""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
import hunt as H  # noqa: E402
import witchhut_common as W  # noqa: E402

VERIFIED = "1.21.4"
DEEP = 55
OURS = "kyoubitt/Low-Witch-Finder"


def key(seed, cx, cz):
    return "%s %d %d" % (seed, cx, cz)


def ours():
    """[(seed, x, z, claimed depth, source)] from this machine's results and the hand-collected list."""
    out = [(r["seed"], r["x"], r["z"], r["depth"], OURS) for r in H.all_results() if r["depth"] >= DEEP]
    out += [(f.seed, f.x, f.z, -f.depth, "known list") for f in W.load_finds() if f.located and -f.depth >= DEEP]
    return out


def imported(path, source=None):
    m = re.search(r"y-?(\d+)", os.path.basename(path))
    depth = int(m.group(1)) if m else 0
    src = source or "imported:" + os.path.basename(os.path.dirname(os.path.abspath(path)))
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                out.append((str(d["seed"]), int(d["x"]), int(d["z"]), depth, d.get("source") or src))
    return out


def existing(out_dir):
    have = {}
    for p in glob.glob(os.path.join(out_dir, "data", "y-*.jsonl")):
        with open(p, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    d = json.loads(line)
                    have[key(d["seed"], d["x"] >> 4, d["z"] >> 4)] = d
    return have


def read_oracle(cands):
    """{key: (hutY, air)} for [(seed, cx, cz)] by the game's own code, in batches so a long list shows progress."""
    got = {}
    for i in range(0, len(cands), 40):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("".join("%s %d %d\n" % c for c in cands[i:i + 40]))
            spots = f.name
        try:
            for line in H.oracle(spots, threads=os.cpu_count() or 4):
                r = H.parse_exact(line)
                got[key(r["seed"], r["cx"], r["cz"])] = (r["hutY"], r["air"])
        finally:
            os.remove(spots)
        print("  read %d of %d" % (min(i + 40, len(cands)), len(cands)), file=sys.stderr)
    return got


CATALOG = "https://github.com/kyoubitt/low-y-witch-huts"
CREDIT = "Co-authored-by: xvyz1 <155855346+xvyz1@users.noreply.github.com>"


def table_md(by):
    """The counts table, with the hut closest to 0,0 at each depth."""
    rows = ["| Depth | Huts | Closest to 0,0 |", "|---:|---:|---|"]
    for y in sorted(by):
        c = min(by[y], key=lambda d: abs(d["x"]) + abs(d["z"]))
        rows.append("| %d | %d | `/tp %d %d %d` (seed `%s`) |" % (y, len(by[y]), c["x"], y + 2, c["z"], c["seed"]))
    n = sum(len(v) for v in by.values())
    return "\n".join(rows) + "\n\n**%d huts** at -55 or deeper, each re-read by Minecraft 1.21.4's own world generation: [all of them](%s).\n" % (n, CATALOG)


def put_block(path, text):
    """Replace what is between the markers in path. False when the file or the markers aren't there."""
    try:
        old = open(path, encoding="utf-8").read()
    except OSError:
        return False
    a, b = old.find("<!-- huts:start -->"), old.find("<!-- huts:end -->")
    if a < 0 or b < a:
        return False
    new = old[:a] + "<!-- huts:start -->\n" + text + old[b:]
    if new != old:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
    return True


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, **kw)


def commit(repo, added, push):
    """Commit what changed in the catalog checkout as the checkout's last author, with the credit line, and push it."""
    git(repo, "add", "-A")
    if not git(repo, "status", "--porcelain").stdout.strip():
        return "nothing new to commit"
    who = git(repo, "log", "-1", "--format=%an%n%ae").stdout.split("\n")
    ident = ["-c", "user.name=" + who[0], "-c", "user.email=" + who[1]] if len(who) >= 2 and who[0] else []
    msg = "Add %d hut%s found by Low Witch Finder\n\n%s\n" % (added, "" if added == 1 else "s", CREDIT)
    r = subprocess.run(["git", "-C", repo] + ident + ["commit", "-q", "-m", msg], capture_output=True, text=True)
    if r.returncode:
        return "commit failed: " + (r.stderr or r.stdout).strip()
    if push:
        r = git(repo, "push", "-q")
        return "pushed" if r.returncode == 0 else "push failed: " + r.stderr.strip()
    return "committed"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out_dir")
    ap.add_argument("--import", dest="imports", nargs="*", default=[], metavar="FILE.jsonl")
    ap.add_argument("--source", help="what to call the --import lists in the output")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--readme", nargs="*", default=[], metavar="FILE")
    ap.add_argument("--keep-going", action="store_true", help="carry on if the oracle can't read one batch")
    a = ap.parse_args()
    H.need("terrain_dump")
    have = existing(a.out_dir)
    cands, seen = [], set(have)
    for seed, x, z, depth, src in ours() + [c for p in a.imports for c in imported(p, a.source)]:
        k = key(seed, x >> 4, z >> 4)
        if k not in seen:
            seen.add(k)
            cands.append((seed, x, z, depth, src))
    print("%d already listed, %d to read" % (len(have), len(cands)), file=sys.stderr)
    got = read_oracle([(s, x >> 4, z >> 4) for s, x, z, _, _ in cands]) if cands else {}
    bad = []
    for seed, x, z, claimed, src in cands:
        k = key(seed, x >> 4, z >> 4)
        y = got.get(k, (None,))[0]
        if y is not None and y <= -DEEP:
            have[k] = dict(seed=seed, x=x, z=z, y=y, source=src, verified=VERIFIED)
        else:
            bad.append((seed, x, z, claimed, "" if y is None else y, src))
    os.makedirs(os.path.join(a.out_dir, "data"), exist_ok=True)
    by = {}
    for d in have.values():
        by.setdefault(d["y"], []).append(d)
    for old in glob.glob(os.path.join(a.out_dir, "data", "y-*.jsonl")):
        os.remove(old)
    for y in sorted(by):
        with open(os.path.join(a.out_dir, "data", "y%d.jsonl" % y), "w", encoding="utf-8") as f:
            for d in sorted(by[y], key=lambda d: (abs(d["x"]) + abs(d["z"]), d["seed"])):         # closest to 0,0 first
                f.write(json.dumps(d, separators=(",", ":")) + "\n")
    with open(os.path.join(a.out_dir, "data", "all.tsv"), "w", encoding="utf-8") as f:
        f.write("#depth\tseed\tx\ty\tz\tnote\n")
        for y in sorted(by):
            for d in sorted(by[y], key=lambda d: (abs(d["x"]) + abs(d["z"]), d["seed"])):
                f.write("%d\t%s\t%d\t%d\t%d\t%s\n" % (d["y"], d["seed"], d["x"], d["y"] + 2, d["z"], d["source"]))
    with open(os.path.join(a.out_dir, "unconfirmed.tsv"), "w", encoding="utf-8") as f:
        f.write("#claimed\tseed\tx\tz\toracle_y\tsource\n")
        for seed, x, z, claimed, y, src in bad:
            f.write("%s\t%s\t%d\t%d\t%s\t%s\n" % (-claimed if claimed else "", seed, x, z, y, src))
    table = table_md(by)
    for path in [os.path.join(a.out_dir, "README.md")] + a.readme:
        put_block(path, table)
    print("kept %d (%s), %d unconfirmed" % (len(have), ", ".join("%d: %d" % (y, len(by[y])) for y in sorted(by)), len(bad)))
    if a.commit:
        print(commit(a.out_dir, len(cands) - len(bad), a.push))


if __name__ == "__main__":
    main()
