#!/usr/bin/env python3
"""dump_trees.py - dumps the game's biome tree for many versions and says which versions share one. Writes the dumps and groups.json to OUTDIR."""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import harness  # noqa: E402

VERSIONS = ["1.18", "1.18.1", "1.18.2", "1.19", "1.19.2", "1.19.3", "1.19.4", "1.20", "1.20.1", "1.20.2", "1.20.4", "1.20.5", "1.20.6",
            "1.21", "1.21.1", "1.21.2", "1.21.3", "1.21.4", "1.21.5", "1.21.8", "1.21.11", "26.1", "26.2", "26.3"]
out = sys.argv[1]
os.makedirs(out, exist_ok=True)
groups = {}
for v in VERSIONS:
    p = os.path.join(out, "tree_%s.txt" % v)
    try:
        harness.dump(v, p)
    except Exception as e:                                    # a version that can't be dumped is reported, not hidden
        print(v, "FAILED:", str(e)[:200], flush=True)
        continue
    h = hashlib.md5(open(p, "rb").read()).hexdigest()[:10]
    groups.setdefault(h, []).append(v)
    print(v, h, sum(1 for _ in open(p)), "nodes", flush=True)
json.dump(groups, open(os.path.join(out, "groups.json"), "w"), indent=1)
print(json.dumps(groups))
