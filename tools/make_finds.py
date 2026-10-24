#!/usr/bin/env python3
"""make_finds.py ALL_FINDS.md > data/finds.tsv  -  the witch huts of the project's find list, one per line.

Columns: depth, seed, x, y, z, note. x/y/z are the /tp "inside the hut" coordinates, blank when the list has none.
Huts only: the exposed-sky pits and the other notable seeds are not huts and are left out."""
import re
import sys

TP = re.compile(r"/tp\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)")
SEED = re.compile(r"^-?\d{6,20}$")


def main(path):
    rows, depth, section = [], None, None
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        h = re.match(r"^## (.*)", line)
        if h:
            section = h.group(1)
            m = re.match(r"-(5\d) ", section)
            depth = -int(m.group(1)) if m else None
            continue
        if section is None or section.startswith(("Exposed", "Other", "-54", "-60")):
            continue
        if re.match(r"^-5\d witch huts", section) or section.startswith("-58 witch huts"):
            m = re.match(r"^(-?\d{6,20})\s+(/tp .*)$", line)
            if m and TP.search(m.group(2)):
                x, y, z = TP.search(m.group(2)).groups()
                rows.append((depth, m.group(1), x, y, z, ""))
                continue
        if section.startswith("-59") and line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and SEED.match(cells[0]):
                t = TP.search(cells[1])
                note = ""
                rows.append((-59, cells[0], *(t.groups() if t and "ideal" not in cells[1] else ("", "", "")), note))
        if section.startswith("-57 / -56") and line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 3 and re.match(r"^-5\d$", cells[0]) and SEED.match(cells[1]):
                t = TP.search(cells[2])
                rows.append((int(cells[0]), cells[1], *(t.groups() if t else ("", "", "")), ""))
    seen, out = set(), []
    for r in rows:
        if r[1] in seen:
            continue
        seen.add(r[1])
        out.append(r)
    out.sort(key=lambda r: (r[0], r[1]))
    print("#depth\tseed\tx\ty\tz\tnote")
    for r in out:
        print("\t".join(str(v) for v in r))


if __name__ == "__main__":
    main(sys.argv[1])
