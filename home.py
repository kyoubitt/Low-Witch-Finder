"""home.py - where the app keeps its files.

From source that is the project folder. As the single .exe, the tools (bin/), the layout data, the oracle and the logo travel
inside the exe as one archive; the first launch unpacks them into the user's app folder, and every later launch only checks a
stamp. The downloads (Minecraft files, Java) and the hunts' own folders (runs/, results/) live there too, so an update of
the exe never touches them."""
import hashlib
import os
import sys
import zipfile

FROZEN = bool(getattr(sys, "frozen", False))


def app_folder():
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Low Witch Finder")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"), "low-witch-finder")


def unpack(say=None):
    """Unpack the payload inside the exe when this version isn't unpacked yet. Returns the app folder."""
    root = app_folder()
    archive = os.path.join(getattr(sys, "_MEIPASS", ""), "payload.zip")
    if not os.path.isfile(archive):
        return root
    with zipfile.ZipFile(archive) as z:
        stamp = hashlib.sha1(repr([(i.filename, i.CRC) for i in z.infolist()]).encode()).hexdigest()
        mark = os.path.join(root, "payload.stamp")
        try:
            with open(mark) as f:
                if f.read().strip() == stamp:
                    return root
        except OSError:
            pass
        if say:
            say("Unpacking Low Witch Finder (first launch only)...")
        os.makedirs(root, exist_ok=True)
        z.extractall(root)
        with open(mark, "w") as f:
            f.write(stamp)
    return root


ROOT = unpack(lambda t: print(t, file=sys.stderr, flush=True)) if FROZEN else os.path.dirname(os.path.realpath(__file__))
