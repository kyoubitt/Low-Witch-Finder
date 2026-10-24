#!/usr/bin/env python3
"""mcver.py - gets any Minecraft version's own game code from Mojang, for checking the map against the game (never shipped with the app).
mcver.py VERSION... downloads each into ~/.cache/witchhut-mc/VERSION (client jar, run through Mojang's name list when it has one, plus
its libraries, every file checked against Mojang's SHA-1). Used by test_accuracy.py and dump_trees.py. Standard library only."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request

CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "witchhut-mc")
MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
SPECIAL = ("https://repo1.maven.org/maven2/net/md-5/SpecialSource/1.11.4/SpecialSource-1.11.4-shaded.jar",
           "2c61feb5ab1218de36deeafeee8550a2a702b2ef")


def _sha1(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _get(url, dest, sha1):
    if os.path.isfile(dest) and _sha1(dest) == sha1:
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as r, open(dest + ".part", "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    if _sha1(dest + ".part") != sha1:
        os.remove(dest + ".part")
        raise RuntimeError("bad download: " + url)
    os.replace(dest + ".part", dest)


def java_for(info, version):
    """The java binary this version needs (its own version file says which major version)."""
    major = (info.get("javaVersion") or {}).get("majorVersion", 17)
    for cand in ("/usr/lib/jvm/java-%d-openjdk/bin/java" % major, "/usr/lib/jvm/java-%d/bin/java" % major):
        if os.path.isfile(cand):
            return cand
    j = shutil.which("java")
    if j:
        return j
    raise RuntimeError("no Java found")


def fetch(version):
    """Returns (classpath string, java binary, javac binary)."""
    out = os.path.join(CACHE, version)
    os.makedirs(out, exist_ok=True)
    vj = os.path.join(out, "version.json")
    if not os.path.isfile(vj):
        with urllib.request.urlopen(MANIFEST, timeout=60) as r:
            m = json.load(r)
        e = next(v for v in m["versions"] if v["id"] == version)
        _get(e["url"], vj, e["sha1"])
    info = json.load(open(vj))
    d = info["downloads"]
    jar = os.path.join(out, "game.jar")
    java = java_for(info, version)
    if not os.path.isfile(jar):
        client = os.path.join(out, "client.jar")
        _get(d["client"]["url"], client, d["client"]["sha1"])
        if "client_mappings" in d:
            maps = os.path.join(out, "maps.txt")
            _get(d["client_mappings"]["url"], maps, d["client_mappings"]["sha1"])
            ss = os.path.join(CACHE, "SpecialSource.jar")
            _get(SPECIAL[0], ss, SPECIAL[1])
            r = subprocess.run([shutil.which("java") or java, "-jar", ss, "--in-jar", client, "--out-jar", jar + ".part", "--srg-in", maps],
                               capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError("renaming failed: " + r.stderr[-300:])
            os.replace(jar + ".part", jar)
            os.remove(maps)
        else:
            shutil.copy(client, jar)                          # 26.x ships with its own names
        os.remove(client)
    cp = [jar]
    for lib in info["libraries"]:
        a = lib["downloads"].get("artifact")
        if a and not lib.get("rules"):
            p = os.path.join(CACHE, "libs", a["path"].replace("/", "__"))
            _get(a["url"], p, a["sha1"])
            cp.append(p)
    return os.pathsep.join(cp), java, os.path.join(os.path.dirname(java), "javac")


if __name__ == "__main__":
    for v in sys.argv[1:]:
        print(v, "ready", fetch(v)[1])
