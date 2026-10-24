#!/usr/bin/env python3
"""export_catalog.py - writes the public list of low witch huts (-55 and deeper) after the game's own code has re-read each one.

  python3 tools/export_catalog.py OUT_DIR                          our finds: results/*.txt and data/finds.tsv
  python3 tools/export_catalog.py OUT_DIR --import FILE.jsonl ...  also another list's huts, as candidates (a line is {"seed","x","z"};
                                                                   the depth is read from a file name like y-59.jsonl)

Every candidate goes through the oracle (the game's 1.21.4 world generation, hunt.oracle); only the ones that really come out at -55
or deeper are written, at the depth the oracle read, so a wrong claim never gets in. The rest go to unconfirmed.tsv.
What is already in OUT_DIR is kept and not asked again. Needs the Minecraft files (mcfiles.py) and Java 21.

  --commit [--push]   also commit what changed in OUT_DIR (a git checkout of the catalog repo) and push it
  --readme FILE ...   refresh the short block (live badges and a link) between <!-- huts:start --> and <!-- huts:end --> in these files too;
                      OUT_DIR/README.md always gets the full block (badges, depth table, top 10), plus RANKING.md and data/ranked.tsv
  --version-lists DIR set each hut's "versions" from lists DIR/<version group>/y-NN.jsonl (lines with seed, x, z)

The ranking is the order the finder itself uses: deepest first, then closest to 0,0.

The hunt driver runs this by itself after a run that found something, when a catalog checkout is set up (hunt.sync_catalog)."""
import argparse
import glob
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time

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


RAW = "https://raw.githubusercontent.com/kyoubitt/low-y-witch-huts/main/data/summary.json"
DEPTH_COLOUR = {-59: "e0457b", -58: "f0883e", -57: "d4a72c", -56: "8cb34a", -55: "4c9f70"}


def rank_huts(by):
    """Every hut once, ranked: deepest first, then closest to 0,0, then seed. Adds rank and dist (blocks from 0,0)."""
    rows = [dict(d, dist=int(math.hypot(d["x"], d["z"]))) for v in by.values() for d in v]
    rows.sort(key=lambda d: (d["y"], d["dist"], d["seed"]))
    for i, d in enumerate(rows, 1):
        d["rank"] = i
    return rows


def short_dist(n):
    return "%d" % n if n < 100_000 else "%.1fM" % (n / 1e6) if n >= 1_000_000 else "%dk" % (n / 1000)


def badge(label, query, colour):
    return "![%s](https://img.shields.io/badge/dynamic/json?url=%s&query=%s&label=%s&color=%s&style=flat-square)" % (
        label, RAW.replace(":", "%3A").replace("/", "%2F"), query, label.replace(" ", "%20"), colour)


def badges(by):
    return " ".join([badge("huts", "%24.total", "2ea44f"), badge("y -59", "%24.y59", DEPTH_COLOUR[-59]),
                     badge("y -58", "%24.y58", DEPTH_COLOUR[-58]), badge("updated", "%24.updated", "6e7781")])


def vers(d, groups):
    v = d.get("versions") or []
    return "%d/%d" % (len(v), groups) if groups else ""


def depth_table(by):
    top = max(len(v) for v in by.values())
    rows = ["| Depth | Huts | | Closest to 0,0 |", "|---:|---:|:---|---|"]
    for y in sorted(by):
        c = min(by[y], key=lambda d: abs(d["x"]) + abs(d["z"]))
        rows.append("| **%d** | %d | `%s` | `/tp %d %d %d` |" % (y, len(by[y]), "\u2588" * max(1, round(12 * len(by[y]) / top)), c["x"], y + 2, c["z"]))
    return "\n".join(rows)


def top_table(ranked, groups, n=10):
    rows = ["| # | Depth | Teleport | From 0,0 | Seed |", "|---:|---:|---|---:|---|"]
    for d in ranked[:n]:
        rows.append("| %d | **%d** | `/tp %d %d %d` | %s | `%s` |" % (d["rank"], d["y"], d["x"], d["y"] + 2, d["z"], short_dist(d["dist"]), d["seed"]))
    return "\n".join(rows)


def table_md(by, ranked, groups):
    """The README block of the catalog: live badges, the depth table, the ten best."""
    return "%s\n\n%s\n\n### Top 10\nDeepest first, then closest to 0,0. [The full ranking](RANKING.md) lists all %d.\n\n%s\n" % (
        badges(by), depth_table(by), len(ranked), top_table(ranked, groups))


def short_md(n):
    """What the finder's README shows: the badges (they read the catalog live, so this never goes stale) and the links."""
    return "%s\n\nEvery -55 or deeper hut found, each re-read by the game's own code: [the list](%s), [the ranking](%s/blob/main/RANKING.md).\n" % (
        "BADGES", CATALOG, CATALOG)


def ranking_md(ranked, groups):
    rows = ["# Ranking", "", "Deepest first, then closest to 0,0. %d huts. `Versions` is how many of the %d tracked version groups give the same depth." % (len(ranked), groups),
            "", "| # | Depth | Teleport | From 0,0 | Seed | Versions | Source |", "|---:|---:|---|---:|---|---:|---|"]
    for d in ranked:
        rows.append("| %d | **%d** | `/tp %d %d %d` | %s | `%s` | %s | %s |" % (d["rank"], d["y"], d["x"], d["y"] + 2, d["z"], short_dist(d["dist"]),
                                                                              d["seed"], vers(d, groups), d["source"]))
    return "\n".join(rows) + "\n"


def version_lists(root):
    """{key: [version group, ...]} from root/<group>/y-NN.jsonl, and the number of groups."""
    out, groups = {}, []
    for g in sorted(os.listdir(root)):
        files = glob.glob(os.path.join(root, g, "y-*.jsonl"))
        if not files:
            continue
        groups.append(g)
        for p in files:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        d = json.loads(line)
                        out.setdefault(key(str(d["seed"]), int(d["x"]) >> 4, int(d["z"]) >> 4), []).append(g)
    return out, len(groups)


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


def share_url(huts):
    """A GitHub 'new issue' link that carries the new huts, for a person who can't push to the list themselves."""
    from urllib.parse import quote
    body = "New -55 or deeper huts from Low Witch Finder (seed x z):\n\n" + "\n".join("%s %d %d" % (s, x, z) for s, x, z in huts[:40])
    return "%s/issues/new?title=%s&body=%s" % (CATALOG, quote("New huts: %d" % len(huts)), quote(body))


def commit(repo, added, push, huts=()):
    """Commit what changed in the catalog checkout as the checkout's last author, with the credit line, and push it."""
    git(repo, "add", "-A")
    if not git(repo, "status", "--porcelain").stdout.strip():
        return "nothing new to commit"
    who = git(repo, "log", "-1", "--format=%an%n%ae").stdout.split("\n")
    ident = ["-c", "user.name=" + who[0], "-c", "user.email=" + who[1]] if len(who) >= 2 and who[0] else []
    msg = ("Add %d hut%s found by Low Witch Finder" % (added, "" if added == 1 else "s") if added > 0 else "Update the list") + "\n\n%s\n" % CREDIT
    r = subprocess.run(["git", "-C", repo] + ident + ["commit", "-q", "-m", msg], capture_output=True, text=True)
    if r.returncode:
        return "commit failed: " + (r.stderr or r.stdout).strip()
    if push:
        r = git(repo, "push", "-q")
        if r.returncode == 0:
            return "pushed"
        if huts:                                              # nobody but the owner can push: leave a link that shares them instead
            with open(os.path.join(H.RUNS, "catalog_share.url"), "w", encoding="utf-8") as f:
                f.write(share_url(list(huts)))
        return "push failed: " + (r.stderr.strip().splitlines() or ["no access"])[-1]
    return "committed"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out_dir")
    ap.add_argument("--import", dest="imports", nargs="*", default=[], metavar="FILE.jsonl")
    ap.add_argument("--source", help="what to call the --import lists in the output")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--readme", nargs="*", default=[], metavar="FILE")
    ap.add_argument("--version-lists", metavar="DIR")
    ap.add_argument("--keep-going", action="store_true", help="carry on if the oracle can't read one batch")
    a = ap.parse_args(argv)
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
            have[k] = dict(seed=seed, x=x, z=z, y=y, source=src, verified=VERIFIED, versions=[VERIFIED])
        else:
            bad.append((seed, x, z, claimed, "" if y is None else y, src))
    groups = 0
    if a.version_lists:
        lists, groups = version_lists(a.version_lists)
        for k, d in have.items():
            if k in lists:
                d["versions"] = sorted(set(lists[k]), key=lambda g: [int(n) for n in re.findall(r"\d+", g)])
            elif not d.get("versions"):
                d["versions"] = [VERIFIED]
    else:
        groups = max((len(d.get("versions") or []) for d in have.values()), default=0)
        for d in have.values():
            d.setdefault("versions", [VERIFIED])
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
    ranked = rank_huts(by)
    with open(os.path.join(a.out_dir, "RANKING.md"), "w", encoding="utf-8") as f:
        f.write(ranking_md(ranked, groups))
    with open(os.path.join(a.out_dir, "data", "ranked.tsv"), "w", encoding="utf-8") as f:
        f.write("#rank\tdepth\tseed\tx\ty\tz\tdistance\tsource\n")
        for d in ranked:
            f.write("%d\t%d\t%s\t%d\t%d\t%d\t%d\t%s\n" % (d["rank"], d["y"], d["seed"], d["x"], d["y"] + 2, d["z"], d["dist"], d["source"]))
    summary = dict(total=len(ranked), updated=time.strftime("%Y-%m-%d"), **{"y%d" % -y: len(v) for y, v in by.items()})
    sp = os.path.join(a.out_dir, "data", "summary.json")
    try:
        old = json.load(open(sp))
        if {k: v for k, v in old.items() if k != "updated"} == {k: v for k, v in summary.items() if k != "updated"}:
            summary["updated"] = old.get("updated", summary["updated"])          # only a real change moves the date
    except (OSError, ValueError):
        pass
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
        f.write("\n")
    put_block(os.path.join(a.out_dir, "README.md"), table_md(by, ranked, groups))
    for path in a.readme:
        put_block(path, short_md(len(ranked)).replace("BADGES", badges(by)))
    print("kept %d (%s), %d unconfirmed" % (len(have), ", ".join("%d: %d" % (y, len(by[y])) for y in sorted(by)), len(bad)))
    if a.commit:
        new = [(s, x, z) for s, x, z, _, _ in cands if key(s, x >> 4, z >> 4) in have]
        print(commit(a.out_dir, len(new), a.push, new))


if __name__ == "__main__":
    main()
