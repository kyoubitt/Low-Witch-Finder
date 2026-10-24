"""witchhut_common.py - what the app knows that is not drawing: where things are, the finds, the biome map, seeds,
Standard library only."""
import json
import math
import os
import shutil
import subprocess
import sys
import threading

ROOT = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.realpath(__file__))   # the .exe keeps its tools beside it
DATA = os.path.join(ROOT, "data")
ENGINE = os.path.join(ROOT, "engine")
EXE = ".exe" if os.name == "nt" else ""

# ---- game versions the map knows. code = a game version name for biome_map --ver (the game's own biome tree for it). Versions that share
# one tree are listed together; VERSION_MEMBERS says which game versions each entry stands for (checked against the game, tools/test_accuracy.py)
VERSIONS = [
    ("26.3", "26.3", "26.3", True),
    ("26.2", "26.2", "26.2", True),
    ("1.21.5", "1.21.5 - 26.1", "1.21.5 ... 1.21.11, 26.1", True),
    ("1.21.4", "1.21.4", "1.21.4", True),
    ("1.21.1", "1.20 - 1.21.3", "1.20 ... 1.20.6, 1.21 - 1.21.3", True),
    ("1.19.4", "1.19.3 - 1.19.4", "1.19.3, 1.19.4", True),
    ("1.19.2", "1.19 - 1.19.2", "1.19, 1.19.1, 1.19.2", True),
    ("1.18.2", "1.18", "1.18, 1.18.1, 1.18.2", True),
]
VERSION_MEMBERS = {
    "26.3": ["26.3"], "26.2": ["26.2"], "1.21.5": ["1.21.5", "1.21.8", "1.21.11", "26.1"], "1.21.4": ["1.21.4"],
    "1.21.1": ["1.20", "1.20.1", "1.20.2", "1.20.4", "1.20.5", "1.20.6", "1.21", "1.21.1", "1.21.2", "1.21.3"],
    "1.19.4": ["1.19.3", "1.19.4"], "1.19.2": ["1.19", "1.19.2"], "1.18.2": ["1.18", "1.18.1", "1.18.2"],
}
DEFAULT_VERSION = 0            # 26.3, the newest release; its biomes were checked against the game itself

# ---- biomes: cubiomes ids -> (name, 0xRRGGBB). The swamp is the one that matters, so it is the loudest colour ----
SWAMP = 6
_B = {
    0: ("ocean", 0x1f4f8f), 24: ("deep ocean", 0x16386b), 10: ("frozen ocean", 0x8fb4d8), 50: ("deep frozen ocean", 0x6f95b8),
    44: ("warm ocean", 0x2f8fc0), 45: ("lukewarm ocean", 0x276fb0), 46: ("cold ocean", 0x2a5a9a),
    47: ("deep warm ocean", 0x1f6fa0), 48: ("deep lukewarm ocean", 0x1b4f90), 49: ("deep cold ocean", 0x1a4378),
    1: ("plains", 0x8db360), 129: ("sunflower plains", 0xa5c46a), 2: ("desert", 0xe2cf8f), 130: ("desert lakes", 0xd8c47c),
    3: ("windswept hills", 0x6f7f6a), 34: ("windswept forest", 0x4f6f55), 131: ("windswept gravelly hills", 0x7a8078),
    4: ("forest", 0x2e7d32), 132: ("flower forest", 0x4f9a50), 27: ("birch forest", 0x5f9a58), 155: ("old growth birch forest", 0x6aa862),
    29: ("dark forest", 0x1f4f28), 5: ("taiga", 0x3b6f5a), 32: ("old growth pine taiga", 0x4a6f55), 160: ("old growth spruce taiga", 0x435f4d),
    30: ("snowy taiga", 0x6f9a95), 12: ("snowy plains", 0xe8f0f2), 140: ("ice spikes", 0xbfe0ee), 26: ("snowy beach", 0xe4ead8),
    11: ("frozen river", 0xa8c8e0), 7: ("river", 0x3f7fc0), 16: ("beach", 0xe8dca8), 25: ("stony shore", 0x8a8a80),
    14: ("mushroom fields", 0xa070a0), 21: ("jungle", 0x2f8f2a), 23: ("sparse jungle", 0x5faa3a), 168: ("bamboo jungle", 0x4aa82a),
    35: ("savanna", 0xb5a558), 36: ("savanna plateau", 0xa59a50), 163: ("windswept savanna", 0xa5a060),
    37: ("badlands", 0xc8683a), 38: ("wooded badlands", 0xa8683f), 165: ("eroded badlands", 0xd87a42),
    177: ("meadow", 0x7fbf7f), 178: ("grove", 0x8fb0a0), 179: ("snowy slopes", 0xd8e4ea), 180: ("jagged peaks", 0xbcc4cc),
    181: ("frozen peaks", 0xa8c0d8), 182: ("stony peaks", 0x9a9a98), 185: ("cherry grove", 0xf0a8c8), 184: ("mangrove swamp", 0x5a8f4a),
    186: ("pale garden", 0xaab0a8), 187: ("sulfur caves", 0xb8a840), 188: ("dappled forest", 0x4a8a3a), 174: ("dripstone caves", 0x806a50), 175: ("lush caves", 0x6fa04a), 183: ("deep dark", 0x10303a),
    6: ("swamp", 0xb4e03a),
}


def biome_name(i):
    return _B[i][0] if i in _B else "biome %d" % i


def biome_rgb(i):
    return _B[i][1] if i in _B else 0x7f7f7f


# ---- seeds: a number, or text hashed the way the game does (Java String.hashCode) ---------------------------------
def parse_seed(text):
    t = text.strip()
    if not t:
        return None, None
    try:
        v = int(t)
        v = (v + 2 ** 63) % 2 ** 64 - 2 ** 63        # the game keeps 64 bits
        return v, None
    except ValueError:
        h = 0
        for c in t.encode("utf-16-le").decode("utf-16-le"):
            for u in (ord(c),) if ord(c) < 0x10000 else (0xD800 + ((ord(c) - 0x10000) >> 10), 0xDC00 + ((ord(c) - 0x10000) & 0x3FF)):
                h = (31 * h + u) & 0xFFFFFFFF
        h = h - 2 ** 32 if h >= 2 ** 31 else h
        return h, "text seed, the game turns it into %d" % h


# ---- the finds ---------------------------------------------------------------------------------------------------
class Find:
    __slots__ = ("depth", "seed", "x", "y", "z", "note")

    def __init__(self, depth, seed, x, y, z, note):
        self.depth, self.seed, self.x, self.y, self.z, self.note = depth, seed, x, y, z, note

    @property
    def located(self):
        return self.x is not None

    @property
    def dist(self):
        return math.hypot(self.x, self.z) if self.located else float("inf")

    @property
    def tp(self):
        return "/tp %d %d %d" % (self.x, self.y, self.z) if self.located else ""


def load_finds(path=None):
    path = path or os.path.join(DATA, "finds.tsv")
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                c = line.rstrip("\n").split("\t")
                c += [""] * (6 - len(c))
                loc = c[2] != ""
                out.append(Find(int(c[0]), c[1], int(c[2]) if loc else None, int(c[3]) if loc else None,
                                int(c[4]) if loc else None, c[5]))
    except OSError:
        pass
    return out


# ---- the biome map -------------------------------------------------------------------------------------------------
def find_tool(name):
    """A built tool in bin/, or None if it isn't built."""
    p = os.path.join(ROOT, "bin", name + EXE)
    return p if os.path.isfile(p) else None


def biome_grid(seed, x0, z0, w, h, step, ver, ss=1):
    """w*h biome ids (bytes, row by row) for the cells of `step` blocks starting at (x0, z0), or raises RuntimeError.
    ss = K looks at K x K points in each cell and reports the dominant biome (see biome_map.c). The rows are cut into bands
    that run on every core at once; the result is the same bytes."""
    exe = find_tool("biome_map")
    if not exe:
        raise RuntimeError("biome_map isn't built yet - press b on the Hunt tab, or run ./build.sh")
    work = w * h * ss * ss                                   # samples; one is about 3 microseconds
    bands = max(1, min(os.cpu_count() or 1, h, work // 2500))
    cuts = [h * k // bands for k in range(bands + 1)]
    procs = []
    try:
        for k in range(bands):
            procs.append(subprocess.Popen(
                [exe, "--seed", str(seed), "--x0", str(x0), "--z0", str(z0 + cuts[k] * step), "--w", str(w),
                 "--h", str(cuts[k + 1] - cuts[k]), "--step", str(step), "--ss", str(ss), "--ver", str(ver)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        parts = [p.communicate(timeout=120) for p in procs]
    except (OSError, subprocess.SubprocessError) as e:
        for p in procs:
            p.kill()
        raise RuntimeError(str(e))
    out = b""
    for p, (o, e) in zip(procs, parts):
        if p.returncode != 0:
            raise RuntimeError((e or b"biome_map failed").decode("utf-8", "replace").strip()[:200])
        out += o
    if len(out) != w * h:
        raise RuntimeError("biome_map returned the wrong amount of data")
    return out


def swamp_chunks(grid):
    return grid.count(bytes((SWAMP,)))


# ---- where a witch hut can generate ------------------------------------------------------------------------------
# One candidate chunk per 32x32-chunk region (spacing 32, separation 8, salt 14357620; the same for every version this
# app maps). The hut really generates there only if the chunk is swamp, which the biome map decides.
HUT_SALT, HUT_SPACING, HUT_SEP = 14357620, 32, 8
_MASK = (1 << 48) - 1


class JavaRandom:
    def __init__(self, seed):
        self.s = (seed ^ 0x5DEECE66D) & _MASK

    def _next(self, bits):
        self.s = (self.s * 0x5DEECE66D + 0xB) & _MASK
        v = self.s >> (48 - bits)
        return v - (1 << bits) if v >= 1 << (bits - 1) and bits == 32 else v

    def next_int(self, bound):
        if bound & -bound == bound:
            return (bound * self._next(31)) >> 31
        while True:
            bits = self._next(31)
            val = bits % bound
            if bits - val + (bound - 1) < 1 << 31:
                return val


def hut_chunk(world_seed, rx, rz):
    """The chunk (cx, cz) in region (rx, rz) where a witch hut would start, if the biome allows it."""
    v = (rx * 341873128712 + rz * 132897987541 + world_seed + HUT_SALT) & ((1 << 64) - 1)
    r = JavaRandom(v - (1 << 64) if v >= 1 << 63 else v)
    span = HUT_SPACING - HUT_SEP
    return rx * HUT_SPACING + r.next_int(span), rz * HUT_SPACING + r.next_int(span)


def hut_candidates(world_seed, x0, z0, x1, z1):
    """Candidate chunks whose region touches the block rectangle [x0, x1) x [z0, z1)."""
    size = HUT_SPACING * 16
    out = []
    for rz in range(z0 // size, (z1 - 1) // size + 1):
        for rx in range(x0 // size, (x1 - 1) // size + 1):
            out.append(hut_chunk(world_seed, rx, rz))
    return out


def biome_chunks(seed, chunks, ver):
    """The biome id at the middle of each (cx, cz) chunk, as bytes. Big lists are cut up and run on every core at once."""
    exe = find_tool("biome_map")
    if not exe:
        raise RuntimeError("biome_map isn't built yet")
    if not chunks:
        return b""
    parts = max(1, min(os.cpu_count() or 1, len(chunks) // 400))
    cuts = [len(chunks) * k // parts for k in range(parts + 1)]
    cmd = [exe, "--seed", str(seed), "--stdin", "--ver", str(ver)]
    procs = []
    try:
        for k in range(parts):
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            procs.append(p)
        data = ["".join("%d %d\n" % c for c in chunks[cuts[k]:cuts[k + 1]]).encode() for k in range(parts)]
        results = [None] * parts

        def go(k):
            results[k] = procs[k].communicate(data[k], timeout=120)
        threads = [threading.Thread(target=go, args=(k,)) for k in range(parts)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    except (OSError, subprocess.SubprocessError) as e:
        for p in procs:
            p.kill()
        raise RuntimeError(str(e))
    out = b""
    for p, r in zip(procs, results):
        if r is None or p.returncode != 0:
            raise RuntimeError((r[1] if r else b"biome_map failed").decode("utf-8", "replace").strip()[:200])
        out += r[0]
    if len(out) != len(chunks):
        raise RuntimeError("biome_map gave the wrong number of answers")
    return out


def hut_spots(seed, x0, z0, x1, z1, ver, limit=12000):
    """Chunks in the rectangle where a witch hut generates (swamp at the candidate), or None if the area is too big."""
    size = HUT_SPACING * 16
    if ((x1 - x0) // size + 2) * ((z1 - z0) // size + 2) > limit:
        return None
    cand = [c for c in hut_candidates(seed, x0, z0, x1, z1)]
    ids = biome_chunks(seed, cand, ver)
    return [c for c, b in zip(cand, ids) if b == SWAMP]


WORLD_REGION = 58593                                           # hut regions run to about +-30 million blocks


def nearest_huts(seed, ver, x, z, n=10, max_rings=120):
    """The n witch huts nearest to block (x, z): [(chunkX, chunkZ, distance in blocks)], nearest first.
    Walks outward through rings of 512-block hut regions, checks each ring's candidate chunk with biome_map, and stops
    as soon as no farther ring can hold anything closer than the n-th hut found."""
    size = HUT_SPACING * 16
    rx0, rz0 = x // size, z // size
    found, batch, ring = [], [], 0

    def flush():
        if batch:
            for c, b in zip(batch, biome_chunks(seed, batch, ver)):
                if b == SWAMP:
                    found.append((c[0], c[1], math.hypot(c[0] * 16 + 8 - x, c[1] * 16 + 8 - z)))
            batch.clear()
    while ring <= max_rings:
        cells = [(rx0, rz0)] if ring == 0 else (
            [(rx0 + dx, rz0 + ring) for dx in range(-ring, ring + 1)] + [(rx0 + dx, rz0 - ring) for dx in range(-ring, ring + 1)] +
            [(rx0 + ring, rz0 + dz) for dz in range(-ring + 1, ring)] + [(rx0 - ring, rz0 + dz) for dz in range(-ring + 1, ring)])
        for rx, rz in cells:
            if abs(rx) <= WORLD_REGION and abs(rz) <= WORLD_REGION:
                batch.append(hut_chunk(seed, rx, rz))
        ring += 1
        if len(batch) >= 300 or ring > max_rings:
            flush()
            found.sort(key=lambda f: f[2])
            if len(found) >= n and (ring - 1) * size >= found[n - 1][2]:
                break
    flush()
    found.sort(key=lambda f: f[2])
    return found[:n]


# ---- remembered settings ---------------------------------------------------------------------------------------------------
def prefs_path():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "WitchHut" if os.name == "nt" or sys.platform == "darwin" else "witchhut", "prefs.json")


def load_prefs():
    try:
        with open(prefs_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(d):
    try:
        p = prefs_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(p + ".tmp", p)
    except OSError:
        pass                                                     # settings are a convenience, never a reason to fail


# ---- helpers ----------------------------------------------------------------------------------------------------------
def distro_hint(pkg_apt, pkg_dnf, pkg_pacman, pkg_brew=None):
    """The one-line install command for this system."""
    if sys.platform == "darwin":
        return "brew install %s" % (pkg_brew or pkg_apt)
    if os.name == "nt":
        return "winget install %s" % (pkg_brew or pkg_apt)
    for exe, cmd in (("apt-get", "sudo apt install %s" % pkg_apt), ("dnf", "sudo dnf install %s" % pkg_dnf),
                     ("pacman", "sudo pacman -S %s" % pkg_pacman), ("zypper", "sudo zypper install %s" % pkg_dnf),
                     ("apk", "sudo apk add %s" % pkg_apt)):
        if shutil.which(exe):
            return cmd
    return "install %s with your package manager" % pkg_apt
