#!/usr/bin/env python3
"""harness.py - runs the small Java programs in java/ against any Minecraft version's own code (see mcver.py).
build(version) finds the one that compiles and works for that version; dump() writes the game's biome tree; truth() asks the game for biomes."""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import mcver  # noqa: E402

# (file, replacements): tried in this order; the first that compiles and can dump the tree is the one for that version
VARIANTS = [
    ("A.java", {"@LOOKUP@": "createLookup", "@KEY@": "location"}),
    ("A.java", {"@LOOKUP@": "createLookup", "@KEY@": "identifier"}),
    ("A.java", {"@LOOKUP@": "createWorldLookup", "@KEY@": "identifier"}),
    ("A263.java", {}),
    ("B193.java", {}),
    ("B192.java", {}),
    ("C18.java", {}),
    ("C18old.java", {}),
]


def _compile(version, k, cp, javac):
    src, rep = VARIANTS[k]
    out = os.path.join(mcver.CACHE, version, "cls%d" % k)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    with tempfile.TemporaryDirectory() as t:
        text = open(os.path.join(HERE, "java", src), encoding="utf-8").read()
        for a, b in rep.items():
            text = text.replace(a, b)
        with open(os.path.join(t, "BiomeTruth.java"), "w", encoding="utf-8") as f:
            f.write(text)
        r = subprocess.run([javac, "-proc:none", "-nowarn", "-cp", cp, "-d", out, os.path.join(t, "BiomeTruth.java"),
                            os.path.join(HERE, "java", "TD18.java" if src == "C18old.java" else "TD.java")], capture_output=True, text=True)
    return out if r.returncode == 0 else None


def build(version):
    """(classpath with the harness, java binary) for this version."""
    cp, java, javac = mcver.fetch(version)
    pick = os.path.join(mcver.CACHE, version, "harness.txt")
    order = range(len(VARIANTS))
    if os.path.isfile(pick):
        order = [int(open(pick).read())]
    for k in order:
        cls = _compile(version, k, cp, javac)
        if not cls:
            continue
        probe = os.path.join(mcver.CACHE, version, "probe.txt")
        r = subprocess.run([java, "-cp", cp + os.pathsep + cls, "BiomeTruth", "dump", probe], capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and os.path.isfile(probe) and os.path.getsize(probe) > 1000:
            open(pick, "w").write(str(k))
            return cp + os.pathsep + cls, java
    raise RuntimeError("no harness works for " + version)


def dump(version, path):
    cp, java = build(version)
    r = subprocess.run([java, "-cp", cp, "BiomeTruth", "dump", path], capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise RuntimeError(r.stderr[-400:])


def truth(version, points):
    """The game's biome name for each (seed, x, y, z) block position."""
    cp, java = build(version)
    r = subprocess.run(["nice", java, "-cp", cp, "BiomeTruth"], input="".join("%d %d %d %d\n" % p for p in points),
                       capture_output=True, text=True, timeout=3600)
    got = [l.split("BIOME ", 1)[1].strip() for l in r.stdout.splitlines() if "BIOME " in l]
    if len(got) != len(points):
        raise RuntimeError("the game gave %d answers for %d points: %s" % (len(got), len(points), r.stderr[-300:]))
    return got


if __name__ == "__main__":
    for v in sys.argv[1:]:
        build(v)
        print(v, "harness", open(os.path.join(mcver.CACHE, v, "harness.txt")).read())
