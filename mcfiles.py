"""mcfiles.py - fetches the Minecraft 1.21.4 files the exact-height oracle needs, from Mojang's own servers.

The oracle runs the game's own code, so it needs the game. Mojang's files can't be handed out with this app, so they
are downloaded here, every file checked against the SHA-1 Mojang publishes, and the jar is turned into the readable
(Mojang-named) form the oracle was written against:

  mc/minecraft-1.21.4-mojmap.jar    client.jar run through Mojang's official mapping file (SpecialSource, from Maven Central).
                                    The two inputs (client.jar, the name list) are deleted once the renamed jar exists: about 38 MB back.
  mc/lib/*.jar                      the game's own library jars (the ones that apply on every OS)
  mc/READY.json                     written last, so a half-finished download is never mistaken for a finished one

Needs Java 21 (to run SpecialSource). Standard library only. Safe to run again: finished files are kept."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.realpath(__file__))
MC_DIR = os.path.join(ROOT, "mc")
VERSION = "1.21.4"
MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
SPECIAL_SOURCE = ("https://repo1.maven.org/maven2/net/md-5/SpecialSource/1.11.4/SpecialSource-1.11.4-shaded.jar",
                  "2c61feb5ab1218de36deeafeee8550a2a702b2ef")
MOJMAP_JAR = os.path.join(MC_DIR, "minecraft-%s-mojmap.jar" % VERSION)
LIB_DIR = os.path.join(MC_DIR, "lib")
READY = os.path.join(MC_DIR, "READY.json")


def ready():
    """True when the oracle's files are all there."""
    try:
        with open(READY, encoding="utf-8") as f:
            r = json.load(f)
        return r.get("version") == VERSION and os.path.isfile(MOJMAP_JAR) and bool(os.listdir(LIB_DIR))
    except (OSError, ValueError):
        return False


def classpath():
    """The jars for the oracle's class path: the game, then its libraries."""
    return [MOJMAP_JAR] + sorted(os.path.join(LIB_DIR, n) for n in os.listdir(LIB_DIR) if n.endswith(".jar"))


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(url, dest, sha1, say, label):
    """Download url to dest unless dest already has the right hash. Raises RuntimeError on a bad download."""
    if os.path.isfile(dest) and _sha1(dest) == sha1:
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    say(label)
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    except OSError as e:
        raise RuntimeError("couldn't download %s: %s" % (label, e))
    if _sha1(tmp) != sha1:
        os.remove(tmp)
        raise RuntimeError("%s didn't match Mojang's checksum (a bad download); try again" % label)
    os.replace(tmp, dest)


def java_ok():
    """(path, major version) of the java on PATH, or (None, 0)."""
    java = shutil.which("java")
    if not java:
        return None, 0
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=20)
        text = out.stderr + out.stdout
        q = text.split('"')[1] if '"' in text else ""
        major = int(q.split(".")[0]) if q.split(".")[0].isdigit() else 0
        return java, major
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return java, 0


def fetch(say=print):
    """Download and prepare everything. say(text) reports progress. Raises RuntimeError with a plain message on failure."""
    if ready():
        say("Minecraft files ready")
        return
    java, major = java_ok()
    if major < 21:
        raise RuntimeError("Java 21 or newer is needed (found %s). Install it, then try again." %
                           ("none" if not java else "Java %d" % major))
    os.makedirs(MC_DIR, exist_ok=True)
    say("Asking Mojang which files 1.21.4 uses")
    try:
        with urllib.request.urlopen(MANIFEST, timeout=60) as r:
            manifest = json.load(r)
        entry = next(v for v in manifest["versions"] if v["id"] == VERSION)
        vjson = os.path.join(MC_DIR, "%s.json" % VERSION)
        _download(entry["url"], vjson, entry["sha1"], say, "the %s version file" % VERSION)
        with open(vjson, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError, StopIteration, KeyError) as e:
        raise RuntimeError("couldn't read Mojang's version list: %s" % e)
    d = info["downloads"]
    client = os.path.join(MC_DIR, "client.jar")
    maps = os.path.join(MC_DIR, "client_mappings.txt")
    remap = not os.path.isfile(MOJMAP_JAR)
    if remap:
        _download(d["client"]["url"], client, d["client"]["sha1"], say, "Minecraft %s (28 MB)" % VERSION)
        _download(d["client_mappings"]["url"], maps, d["client_mappings"]["sha1"], say, "Mojang's name list (10 MB)")
    libs = [l["downloads"]["artifact"] for l in info["libraries"] if "artifact" in l["downloads"] and not l.get("rules")]
    for i, a in enumerate(libs, 1):
        _download(a["url"], os.path.join(LIB_DIR, os.path.basename(a["path"])), a["sha1"], say,
                  "library %d of %d: %s" % (i, len(libs), os.path.basename(a["path"])))
    ss = os.path.join(MC_DIR, "SpecialSource.jar")
    _download(SPECIAL_SOURCE[0], ss, SPECIAL_SOURCE[1], say, "the jar renamer (2 MB)")
    if remap:
        say("Turning the jar into readable names (about 10 seconds)")
        tmp = MOJMAP_JAR + ".part"
        r = subprocess.run([java, "-jar", ss, "--in-jar", client, "--out-jar", tmp, "--srg-in", maps],
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not os.path.isfile(tmp):
            raise RuntimeError("renaming the jar failed: %s" % (r.stderr or r.stdout).strip()[-300:])
        os.replace(tmp, MOJMAP_JAR)
        for name in (client, maps):                          # only inputs: the renamed jar is all the oracle reads
            if os.path.isfile(name):
                os.remove(name)
    with open(READY, "w", encoding="utf-8") as f:
        json.dump({"version": VERSION, "libs": len(libs)}, f)
    say("Minecraft files ready")


if __name__ == "__main__":
    try:
        fetch(lambda t: print(t, flush=True))
    except RuntimeError as e:
        print("FAILED:", e)
        sys.exit(1)
