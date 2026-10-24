#!/usr/bin/env python3
"""drive.py - runs the app in a pseudo-terminal, sends keys and mouse events, and reports what happened.
Not part of the app; a way to check it without a person at the keyboard (POSIX only)."""
import fcntl
import os
import pty
import select
import struct
import sys
import tempfile
import termios
import time

ROOT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
CONFIG = tempfile.mkdtemp(prefix="witchhut-drive-")


def run(steps, cols=110, rows=32, env=None, timeout=60, px=(1100, 640)):
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        e = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", WITCH_MAP="text")
        e["XDG_CONFIG_HOME"] = e["APPDATA"] = CONFIG       # the app remembers settings; a check must not touch yours
        e.update(env or {})
        os.execvpe(sys.executable, [sys.executable, "-I", "witchhut_studio.py"], e)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, px[0], px[1]))
    out = b""

    def pump(secs):
        nonlocal out
        end = time.time() + secs
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try:
                    d = os.read(fd, 65536)
                except OSError:
                    return False
                if not d:
                    return False
                out += d
        return True

    alive = pump(1.6)           # the terminal probe waits a second for an answer
    for step in steps:
        if not alive:
            break
        if isinstance(step, (int, float)):
            alive = pump(step)
        else:
            os.write(fd, step.encode())
            alive = pump(0.35)
    t0 = time.time()
    code = None
    while time.time() - t0 < 5:
        p, st = os.waitpid(pid, os.WNOHANG)
        if p:
            code = os.waitstatus_to_exitcode(st)
            break
        pump(0.1)
    if code is None:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        code = "killed (did not quit)"
    return out.decode("utf-8", "replace"), code


def mouse(kind, x, y):
    b = {"press": 0, "release": 0, "drag": 32, "wheelup": 64, "wheeldown": 65, "move": 35}[kind]
    return "\x1b[<%d;%d;%d%s" % (b, x + 1, y + 1, "m" if kind == "release" else "M")


if __name__ == "__main__":
    steps = ["1", 1.0, "+", "-", "[", "]", "h", "h", mouse("wheelup", 30, 12), mouse("wheeldown", 30, 12), mouse("move", 30, 12),
             mouse("press", 30, 12), mouse("drag", 20, 12), mouse("drag", 10, 8), mouse("release", 10, 8), 1.0, "\x1b[D", "\x1b[A",
             "o", "k", "\x1b[B", "\x1b[B", "\x1b[C", "\x1b[D", "o", "\x1b[6~", "\r", 0.6, "z", "-", "?", "x", "k", "\x1b", "2", 1.2,
             "\x1b[C", "\x1b[D", "\x1b[B", "\x1b[C", "\x1b[C", "\x1b[C", "\x1b[B", "\r", "1", "2", "3", "\x1b", "\x1b[A", "\x1b[D", "r", 0.8,
             mouse("wheeldown", 30, 12), mouse("press", 20, 6), mouse("release", 20, 6), "\x1b", "n", "x", "1", "t", "t", "q"]
    bad_run = False
    for mode, marker in (("text", None), ("kitty", b"\x1b_Ga=T"), ("sixel", b"\x1bP0;1;0q")):
        out, code = run(steps, env={"WITCH_MAP": mode})
        raw = out.encode("utf-8", "replace")
        ok = code == 0 and "Traceback" not in out and (marker is None or marker in raw)
        pics = raw.count(marker) if marker else 0
        print("%-6s exit=%s output=%d bytes pictures=%d %s" % (mode, code, len(raw), pics, "OK" if ok else "PROBLEM"))
        if "Traceback" in out:
            i = out.index("Traceback")
            print(out[i:i + 900])
        bad_run |= not ok
    sys.exit(1 if bad_run else 0)
