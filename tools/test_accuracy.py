#!/usr/bin/env python3
"""test_accuracy.py [--quick] - the map against the game's own code, version by version.
Biomes: biome_map vs the game's biome source, for every game version the map lists (each version's own jar is downloaded from Mojang the
first time, into ~/.cache/witchhut-mc; --quick checks one version of each kind). Hut places: hut_chunk vs the game's placement code (1.21.4).
Needs Java and a network the first time; skips quietly without them. Seeded, so it repeats."""
import os
import random
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "accuracy"))
import mcfiles  # noqa: E402
import witchhut_common as W  # noqa: E402

if not (mcfiles.java_ok()[1] >= 17 and W.find_tool("biome_map")):
    print("skipped: needs Java and biome_map")
    sys.exit(0)
import harness  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("ok    " if ok else "FAIL  ") + name + (("  " + str(detail)) if detail and not ok else ""), flush=True)
    if not ok:
        fails.append(name)


ids = {W.biome_name(i).replace(" ", "_"): i for i in W._B}
quick = "--quick" in sys.argv
for code, label, _, _ in W.VERSIONS:
    members = W.VERSION_MEMBERS[code]
    for v in (members[:1] + members[-1:] if quick else members):
        rng = random.Random(11)
        tot = bad = 0
        try:
            for _ in range(2 if quick else 3):
                seed = rng.getrandbits(64) - 2 ** 63
                pts = [(rng.randint(-40000, 40000), rng.randint(-40000, 40000)) for _ in range(3000)]
                want = harness.truth(v, [(seed, cx * 16 + 8, 64, cz * 16 + 8) for cx, cz in pts])
                got = subprocess.run([W.find_tool("biome_map"), "--seed", str(seed), "--stdin", "--ver", code],
                                     input="".join("%d %d\n" % p for p in pts).encode(), capture_output=True).stdout
                tot += len(pts)
                bad += sum(ids.get(w, -2) != g for w, g in zip(want, got))
        except Exception as e:
            check("biomes in %s (table %s)" % (v, label), False, e)
            continue
        check("biomes in %s match the game (table %s): %d of %d differ" % (v, label, bad, tot), bad <= max(2, tot // 5000), bad)

# -- where a hut can start: the region's chunk (the placement rule is the same in every version)
import mcver  # noqa: E402
cp, java = harness.build("1.21.4")
src = os.path.join(ROOT, "tools", "accuracy", "PlaceTruth.java")
cls = os.path.join(mcver.CACHE, "1.21.4", "place")
os.makedirs(cls, exist_ok=True)
subprocess.run([os.path.join(os.path.dirname(java), "javac"), "-proc:none", "-nowarn", "-cp", cp, "-d", cls, src], check=True, capture_output=True)
rng = random.Random(3)
q = [(rng.getrandbits(64) - 2 ** 63, rng.randint(-900, 900), rng.randint(-900, 900)) for _ in range(20000)]
r = subprocess.run([java, "-cp", cp + os.pathsep + cls, "PlaceTruth"], input="\n".join("%d %d %d" % x for x in q) + "\n", capture_output=True, text=True)
want = [l.split("STDOUT]: R ")[1] if "STDOUT]: R " in l else l.split("R ", 1)[1] for l in r.stdout.splitlines() if "R " in l]
bad = sum("%d %d" % W.hut_chunk(s, rx, rz) != w for (s, rx, rz), w in zip(q, want))
check("hut chunks match the game (%d of %d differ)" % (bad, len(q)), len(want) == len(q) and bad == 0, bad)
print("all passed" if not fails else "%d FAILED" % len(fails))
sys.exit(1 if fails else 0)
