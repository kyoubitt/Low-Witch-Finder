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

from home import ROOT  # noqa: E402  (the project folder, or where the .exe unpacked its tools)
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


JRE_DIR = os.path.join(ROOT, "jre")             # the Java this app fetched for itself when the computer has none
ADOPTIUM = "https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=x64&image_type=jre&os=%s&vendor=eclipse"


def _major(java):
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=20)
        text = out.stderr + out.stdout
        q = text.split('"')[1] if '"' in text else ""
        return int(q.split(".")[0]) if q.split(".")[0].isdigit() else 0
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0


def java_ok():
    """(path, major version) of the best Java: the one this app fetched, else the one on PATH; (None, 0) when there is none."""
    own = os.path.join(JRE_DIR, "bin", "java.exe" if os.name == "nt" else "java")
    best = (None, 0)
    for java in (own if os.path.isfile(own) else None, shutil.which("java")):
        if java:
            m = _major(java)
            if m >= 21:
                return java, m
            if best[0] is None:
                best = (java, m)
    return best


def ensure_java(say):
    """A Java 21 to run the oracle: the computer's own, or Eclipse Temurin's JRE (about 50 MB, checked against the SHA-256 its
    publisher lists) unpacked into jre/ beside the app. Returns the path to java."""
    java, major = java_ok()
    if major >= 21:
        return java
    osname = {"nt": "windows", "posix": "mac" if sys.platform == "darwin" else "linux"}[os.name]
    say("Java 21 isn't on this computer: fetching a small one (about 50 MB)")
    try:
        with urllib.request.urlopen(ADOPTIUM % osname, timeout=60) as r:
            pkg = json.load(r)[0]["binary"]["package"]
        url, want = pkg["link"], pkg["checksum"]
        os.makedirs(MC_DIR, exist_ok=True)
        part = os.path.join(MC_DIR, "jre.download")
        h = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
            for block in iter(lambda: r.read(1 << 20), b""):
                f.write(block)
                h.update(block)
    except (OSError, ValueError, KeyError, IndexError) as e:
        raise RuntimeError("couldn't fetch Java 21: %s. Install it yourself (Temurin 21 JRE), then try again." % e)
    if h.hexdigest() != want:
        os.remove(part)
        raise RuntimeError("the Java download didn't match its checksum (a bad download); try again")
    say("Unpacking Java")
    tmp = JRE_DIR + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    if part and url.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(part) as z:
            z.extractall(tmp)
    else:
        import tarfile
        with tarfile.open(part) as t:
            t.extractall(tmp)
    os.remove(part)
    inner = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
    src = os.path.join(tmp, inner[0]) if len(inner) == 1 else tmp
    shutil.rmtree(JRE_DIR, ignore_errors=True)
    os.replace(src, JRE_DIR)
    shutil.rmtree(tmp, ignore_errors=True)
    for name in ("java",):                                    # a tar keeps the mode, a zip doesn't
        try:
            os.chmod(os.path.join(JRE_DIR, "bin", name), 0o755)
        except OSError:
            pass
    java, major = java_ok()
    if major < 21:
        raise RuntimeError("the Java that was fetched does not run on this computer")
    return java


def fetch(say=print):
    """Download and prepare everything. say(text) reports progress. Raises RuntimeError with a plain message on failure."""
    if ready():
        say("Minecraft files ready")
        return
    java = ensure_java(say)
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
