#!/usr/bin/env python3
"""tui_core.py - the terminal layer: raw keys, SGR mouse, the alternate screen, a diffing cell buffer, and the
map as a picture (kitty graphics / Sixel). Standard library only; Windows (console API) and everything else (termios).

Lifted from SlimeByte's slime_studio.py (same author); only the env var names and the theme hooks changed.
set_base(text, bg) tells Screen the default colours; the app calls it whenever the theme changes."""
import base64
import codecs
import math
import os
import re
import sys
import time
import unicodedata
import zlib

CLEAR = -1                       # a cell left on the terminal's own background: where the map picture shows through
TEXT, BG = 0xffffff, 0x000000


def set_base(text, bg):
    global TEXT, BG
    TEXT, BG = text, bg


def mix(a, b, t):
    """Colour a blended toward b by t (0..1)."""
    ar, ag, ab = a >> 16, (a >> 8) & 255, a & 255
    return (round(ar + ((b >> 16) - ar) * t) << 16) | (round(ag + (((b >> 8) & 255) - ag) * t) << 8) | \
        round(ab + ((b & 255) - ab) * t)


# ---- text width (seeds can be any text, even CJK or emoji) -------------------------------------------------
def cwidth(c):
    o = ord(c)
    if o < 0x300:
        return 1 if o >= 32 and o != 127 else 0
    if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def twidth(s):
    return sum(cwidth(c) for c in s)


def wrap(text, w):
    w = max(1, w)
    out = []
    for para in str(text).split("\n"):
        line = ""
        for word in para.split(" "):
            while len(word) > w:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:w])
                word = word[w:]
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= w:
                line += " " + word
            else:
                out.append(line)
                line = word
        out.append(line)
    return out


# ---- the terminal --------------------------------------------------------------------------------------------
def color_mode():
    v = os.environ.get("WITCH_COLORS", "").lower()
    if v in ("256", "truecolor"):
        return v
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") or os.environ.get("WT_SESSION") or \
            os.environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm", "vscode", "ghostty", "Hyper", "Tabby"):
        return "truecolor"
    term = os.environ.get("TERM", "")
    if os.name == "nt" or any(t in term for t in ("kitty", "ghostty", "alacritty", "foot", "wezterm", "direct")):
        return "truecolor"
    return "256"            # macOS Terminal.app and anything unknown


def to256(c):
    r, g, b = c >> 16, (c >> 8) & 255, c & 255
    q = [0 if v < 48 else 1 if v < 115 else (v - 35) // 40 for v in (r, g, b)]
    cv = [0 if k == 0 else 55 + 40 * k for k in q]
    gi = max(0, min(23, round(((r + g + b) / 3 - 8) / 10)))
    gv = 8 + 10 * gi
    if (r - gv) ** 2 + (g - gv) ** 2 + (b - gv) ** 2 < sum((a - b2) ** 2 for a, b2 in zip((r, g, b), cv)):
        return 232 + gi
    return 16 + 36 * q[0] + 6 * q[1] + q[2]


_SGR = {}


def sgr(fg, bg, bold, mode):
    """The escape sequence for a cell's colours (CLEAR: the terminal's own background)."""
    k = (fg, bg, bold, mode)
    if k not in _SGR:
        if mode == "truecolor":
            b = "49" if bg == CLEAR else "48;2;%d;%d;%d" % (bg >> 16, (bg >> 8) & 255, bg & 255)
            _SGR[k] = "\x1b[0;%s38;2;%d;%d;%d;%sm" % ("1;" if bold else "", fg >> 16, (fg >> 8) & 255, fg & 255, b)
        else:
            b = "49" if bg == CLEAR else "48;5;%d" % to256(bg)
            _SGR[k] = "\x1b[0;%s38;5;%d;%sm" % ("1;" if bold else "", to256(fg), b)
    return _SGR[k]


def _sgr_color(c, base, mode):
    """One colour parameter: base 38 = foreground, 48 = background (CLEAR = the terminal's own background)."""
    if base == 48 and c == CLEAR:
        return "49"
    if mode == "truecolor":
        return "%d;2;%d;%d;%d" % (base, c >> 16, (c >> 8) & 255, c & 255)
    return "%d;5;%d" % (base, to256(c))


def sgr_delta(prev, st, mode):
    """The shortest escape that takes the terminal from style prev to style st: only the colour that changed (about 19
    bytes instead of 43). A bold change, or no previous style, uses the full reset form."""
    fg, bg, bold = st
    if prev is None or prev[2] != bold:
        return sgr(fg, bg, bold, mode)
    parts = []
    if fg != prev[0]:
        parts.append(_sgr_color(fg, 38, mode))
    if bg != prev[1]:
        parts.append(_sgr_color(bg, 48, mode))
    return "\x1b[" + ";".join(parts) + "m"


class Term:
    """Raw keyboard + SGR mouse + the alternate screen, on Windows (console API) and everywhere else (termios)."""
    WIN_KEYS = {"H": "A", "P": "B", "M": "C", "K": "D", "G": "H", "O": "F"}

    def __init__(self):
        self.win = os.name == "nt"
        self.out = sys.__stdout__.buffer
        self.colors = color_mode()
        self.dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.graphics, self.cell_q, self.stash = False, None, ""

    def start(self):
        if self.win:
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            self.k, self.hin, self.hout = k, k.GetStdHandle(-10), k.GetStdHandle(-11)
            m = wintypes.DWORD()
            if not k.GetConsoleMode(self.hin, ctypes.byref(m)):
                raise OSError("not a console")
            self.old_in = m.value
            k.GetConsoleMode(self.hout, ctypes.byref(m))
            self.old_out = m.value
            self.old_cp = (k.GetConsoleCP(), k.GetConsoleOutputCP())
            k.SetConsoleCP(65001)
            k.SetConsoleOutputCP(65001)
            # VT input (keys and mouse arrive as escape sequences), no line editing / echo / Ctrl+C signal / QuickEdit
            k.SetConsoleMode(self.hin, (self.old_in | 0x0200 | 0x0080) & ~(0x0001 | 0x0002 | 0x0004 | 0x0040))
            if not k.SetConsoleMode(self.hout, self.old_out | 0x0004 | 0x0008):
                k.SetConsoleMode(self.hout, self.old_out | 0x0004)
        else:
            import termios
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            t = termios.tcgetattr(self.fd)
            t[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
            t[2] |= termios.CS8
            t[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
            t[6][termios.VMIN], t[6][termios.VTIME] = 0, 0
            termios.tcsetattr(self.fd, termios.TCSAFLUSH, t)
        # alt screen, hide cursor, no autowrap, mouse (click, drag, motion; SGR coordinates), bracketed paste
        self.write(b"\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?2004h\x1b[2J")
        self.graphics = self.probe_graphics()

    def probe_graphics(self):
        """Can this terminal show the map as a picture, and how?
          "kitty"  Ghostty and kitty: kitty's graphics protocol, the picture under the cells' own backgrounds
                   and the words on top.
          "sixel"  any terminal whose device attributes include 4 (Sixel): Windows Terminal 1.22+, WezTerm,
                   foot, iTerm2, Konsole, ... The picture becomes part of the cells; the words go back on it.
        Asked the protocols' way: the kitty query (only where kitty is expected), the cell size, then the
        device attributes, which every terminal answers - so macOS Terminal and the old Windows console just
        answer without a 4 and keep the character map. Kitty is off inside tmux / screen, which don't pass it
        on. WITCH_MAP=text, =kitty (or =image) or =sixel overrides it."""
        v = os.environ.get("WITCH_MAP", "").lower()
        if v == "text":
            return False
        env = os.environ.get
        kitty = v in ("kitty", "image") or (not env("TMUX") and not env("STY") and bool(
            env("TERM_PROGRAM") == "ghostty" or env("GHOSTTY_RESOURCES_DIR") or env("KITTY_WINDOW_ID") or
            env("TERM", "") in ("xterm-ghostty", "xterm-kitty")))
        self.write((b"\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\" if kitty else b"") + b"\x1b[16t\x1b[c")
        buf, end, da = "", time.monotonic() + 1.0, None
        while time.monotonic() < end and not da:
            buf += self.read(0.05)
            da = re.search(r"\x1b\[\?([0-9;]*)c", buf)
        m = re.search(r"\x1b\[6;(\d+);(\d+)t", buf)
        if m and int(m[1]) and int(m[2]):
            self.cell_q = (int(m[2]), int(m[1]))
        self.stash = re.sub(r"\x1b_G[^\x1b]*\x1b\\|\x1b\[6;\d+;\d+t|\x1b\[\?[0-9;]*c", "", buf)   # keys typed meanwhile
        if v in ("kitty", "image") or (kitty and "\x1b_Gi=31;OK" in buf):
            return "kitty"
        if v == "sixel" or (da and "4" in da[1].split(";")):
            return "sixel"
        return False

    def cell_px(self):
        """(width, height) of one cell in the picture protocol's pixels, or None if the terminal won't say."""
        if self.graphics == "sixel" and self.cell_q:
            return self.cell_q                         # the size it said it draws Sixel with
        if not self.win:
            try:
                import fcntl
                import struct
                import termios
                r, c, xp, yp = struct.unpack("HHHH", fcntl.ioctl(sys.__stdout__.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
                if r and c and xp and yp:
                    return xp / c, yp / r
            except Exception:
                pass
        return self.cell_q or ((10, 20) if self.graphics == "sixel" else None)   # Windows Terminal's Sixel cell

    def stop(self):
        if self.graphics == "kitty":
            self.write(b"".join(b"\x1b_Ga=d,d=I,i=%d,q=2\x1b\\" % i for i in PIC_IDS))
        self.write(b"\x1b[0m\x1b[?2004l\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[?7h\x1b[?25h\x1b[?1049l")
        if self.win:
            self.k.SetConsoleMode(self.hin, self.old_in)
            self.k.SetConsoleMode(self.hout, self.old_out)
            self.k.SetConsoleCP(self.old_cp[0])
            self.k.SetConsoleOutputCP(self.old_cp[1])
        else:
            import termios
            termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.old)

    def write(self, data):
        self.out.write(data)
        self.out.flush()

    def size(self):
        try:
            s = os.get_terminal_size(sys.__stdout__.fileno())
            return s.columns, s.lines
        except OSError:
            return 80, 24

    def read(self, timeout):
        if self.stash:                                 # what came in while asking the terminal about pictures
            out, self.stash = self.stash, ""
            return out
        if self.win:
            import msvcrt
            end, out = time.monotonic() + timeout, []
            while True:
                while msvcrt.kbhit():
                    c = msvcrt.getwch()
                    if c in ("\x00", "\xe0"):          # a console without VT input: arrows etc. as scan codes
                        k = self.WIN_KEYS.get(msvcrt.getwch())
                        c = "\x1b[" + k if k else ""
                    out.append(c)
                if out or time.monotonic() >= end:
                    return "".join(out)
                time.sleep(0.008)
        import select
        r, _, _ = select.select([self.fd], [], [], timeout)
        return self.dec.decode(os.read(self.fd, 65536)) if r else ""


CSI = re.compile(r"\x1b\[([<?]?)([0-9;:]*)([\x40-\x7e])")
CSI_PART = re.compile(r"\x1b\[[<?]?[0-9;:]*$")
KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "btab"}
TILDE = {"1": "home", "2": "insert", "3": "delete", "4": "end", "5": "pgup", "6": "pgdn", "7": "home", "8": "end"}


def parse_input(buf):
    """Terminal input -> events: ("key", name), ("char", c), ("paste", text), ("mouse", kind, x, y, button).
    Returns (events, unparsed tail) - a tail is an escape sequence cut off mid-read."""
    ev, i, n = [], 0, len(buf)
    while i < n:
        c = buf[i]
        if c == "\x1b":
            if buf.startswith("\x1b[200~", i):
                j = buf.find("\x1b[201~", i + 6)
                if j < 0:
                    return ev, buf[i:]
                ev.append(("paste", buf[i + 6:j]))
                i = j + 6
                continue
            if i + 1 >= n:
                return ev, buf[i:]
            nx = buf[i + 1]
            if nx == "[":
                m = CSI.match(buf, i)
                if not m:
                    if CSI_PART.match(buf, i):
                        return ev, buf[i:]
                    ev.append(("key", "esc"))
                    i += 1
                    continue
                pre, params, fin = m.groups()
                i = m.end()
                if pre == "<" and fin in "Mm":
                    p = params.split(";")
                    if len(p) >= 3 and all(v.isdigit() for v in p[:3]):
                        b, x, y = int(p[0]), int(p[1]) - 1, int(p[2]) - 1
                        if b & 64:
                            if b & 3 in (0, 1):
                                ev.append(("mouse", "wheelup" if b & 3 == 0 else "wheeldown", x, y, 0))
                        elif b & 32:
                            ev.append(("mouse", "move" if b & 3 == 3 else "drag", x, y, b & 3))
                        else:
                            ev.append(("mouse", "release" if fin == "m" else "press", x, y, b & 3))
                    continue
                name = TILDE.get(params.split(";")[0]) if fin == "~" else KEYS.get(fin)
                if name:
                    ev.append(("key", name))
                continue
            if nx == "O":
                if i + 2 >= n:
                    return ev, buf[i:]
                name = KEYS.get(buf[i + 2])
                if name:
                    ev.append(("key", name))
                i += 3
                continue
            ev.append(("key", "alt"))
            i += 2
            continue
        if c == "\r" or c == "\n":
            ev.append(("key", "enter"))
            if c == "\r" and buf[i + 1:i + 2] == "\n":
                i += 1
        elif c == "\t":
            ev.append(("key", "tab"))
        elif c in "\x7f\x08":
            ev.append(("key", "backspace"))
        elif ord(c) < 32:
            ev.append(("key", "ctrl+" + chr(ord(c) + 96)))
        else:
            ev.append(("char", c))
        i += 1
    return ev, ""


# ---- the screen: a cell buffer, written as a diff against the last frame ----------------------------------------
class Screen:
    def __init__(self, w, h):
        self.w, self.h = w, h
        n = w * h
        self.ch, self.fg, self.bg, self.bd = [" "] * n, [TEXT] * n, [BG] * n, [False] * n
        self.clips = [(0, 0, w, h)]

    def push(self, x0, y0, x1, y1):
        a = self.clips[-1]
        self.clips.append((max(a[0], x0), max(a[1], y0), min(a[2], x1), min(a[3], y1)))

    def pop(self):
        self.clips.pop()

    def _set(self, i, row, c, fg, bg, bold):
        # keep wide characters consistent: overwriting either half of one blanks the other half
        if self.ch[i] == "" and i > row:
            self.ch[i - 1] = " "
        if i + 1 < row + self.w and self.ch[i + 1] == "" and c != "" and cwidth(c[0]) != 2:
            self.ch[i + 1] = " "
        self.ch[i], self.fg[i], self.bd[i] = c, fg, bold
        if bg is not None:
            self.bg[i] = bg

    def put(self, x, y, text, fg=None, bg=None, bold=False):
        fg = TEXT if fg is None else fg
        x0, y0, x1, y1 = self.clips[-1]
        if not y0 <= y < y1:
            return x + twidth(text)
        row = y * self.w
        for c in text:
            w = cwidth(c)
            if w == 0:
                if c >= " " and x0 < x <= x1:
                    self.ch[row + x - 1] += c
                continue
            if x0 <= x < x1:
                if w == 2 and x + 1 >= x1:
                    c, w = " ", 1
                self._set(row + x, row, c, fg, bg, bold)
                if w == 2:
                    self._set(row + x + 1, row, "", fg, bg, bold)
            x += w
        return x

    def fill(self, x, y, w, h, bg, c=" ", fg=None):
        fg = TEXT if fg is None else fg
        x0, y0, x1, y1 = self.clips[-1]
        for yy in range(max(y, y0), min(y + h, y1)):
            row = yy * self.w
            for xx in range(max(x, x0), min(x + w, x1)):
                self._set(row + xx, row, c, fg, bg, False)

    def tint(self, x, y, w, h, frm, to):
        x0, y0, x1, y1 = self.clips[-1]
        for yy in range(max(y, y0), min(y + h, y1)):
            row = yy * self.w
            for xx in range(max(x, x0), min(x + w, x1)):
                if self.bg[row + xx] == frm:
                    self.bg[row + xx] = to

    def hline(self, x, y, w, color, c="─"):
        self.put(x, y, c * max(0, w), color)

    def box(self, x, y, w, h, color):
        if w < 2 or h < 2:
            return
        self.put(x, y, "╭" + "─" * (w - 2) + "╮", color)
        for yy in range(y + 1, y + h - 1):
            self.put(x, yy, "│", color)
            self.put(x + w - 1, yy, "│", color)
        self.put(x, y + h - 1, "╰" + "─" * (w - 2) + "╯", color)

    def diff(self, prev, mode):
        out, cur, W = [], None, self.w
        full = prev is None or prev.w != self.w or prev.h != self.h
        if full:
            out.append("\x1b[0m\x1b[2J")

        for y in range(self.h):
            a, b = y * W, y * W + W
            if full:
                lo, hi = a, b - 1
            else:
                ch, fg, bg, bd = self.ch, self.fg, self.bg, self.bd
                pch, pfg, pbg, pbd = prev.ch, prev.fg, prev.bg, prev.bd
                lo = next((i for i in range(a, b) if ch[i] != pch[i] or fg[i] != pfg[i] or bg[i] != pbg[i]
                           or bd[i] != pbd[i]), None)
                if lo is None:
                    continue
                hi = next(i for i in range(b - 1, lo - 1, -1) if ch[i] != pch[i] or fg[i] != pfg[i] or
                          bg[i] != pbg[i] or bd[i] != pbd[i])
                if self.ch[lo] == "" and lo > a:
                    lo -= 1
                if hi + 1 < b and self.ch[hi + 1] == "":
                    hi += 1
            out.append("\x1b[%d;%dH" % (y + 1, lo - a + 1))
            i = lo
            while i <= hi:
                c = self.ch[i]
                # a blank cell's foreground can't be seen: don't spend bytes changing it
                st = (cur[0] if c == " " and cur is not None else self.fg[i], self.bg[i], self.bd[i])
                if st != cur:
                    out.append(sgr_delta(cur, st, mode))
                    cur = st
                if c == "":
                    c = " "                     # an orphaned second half
                out.append(c)
                if c and cwidth(c[0]) == 2:
                    i += 1                      # the terminal drew both halves
                i += 1
        out.append("\x1b[0m")
        return "".join(out).encode("utf-8", "replace")

    def changed(self, prev, x, y, w, h):
        """Does any cell of the rectangle differ from prev?"""
        if prev is None or (prev.w, prev.h) != (self.w, self.h):
            return True
        for yy in range(y, y + h):
            a, b = yy * self.w + x, yy * self.w + x + w
            if self.ch[a:b] != prev.ch[a:b] or self.fg[a:b] != prev.fg[a:b] or self.bg[a:b] != prev.bg[a:b] or \
                    self.bd[a:b] != prev.bd[a:b]:
                return True
        return False

    def patch(self, x, y, w, h, mode, clear_bg):
        """The rectangle's cells again, except blank ones left on the terminal's background - written over a
        Sixel picture, with clear_bg(x, y) as the background where the cell has the terminal's."""
        out, cur = [], None
        for yy in range(y, y + h):
            row, i, end = yy * self.w, x, x + w
            while i < end:
                k = row + i
                if self.ch[k] == "" or (self.ch[k] == " " and self.bg[k] == CLEAR):
                    i += 1
                    continue
                out.append("\x1b[%d;%dH" % (yy + 1, i + 1))
                while i < end and not (self.ch[row + i] == " " and self.bg[row + i] == CLEAR):
                    k = row + i
                    if self.ch[k] != "":
                        st = sgr(self.fg[k], clear_bg(i, yy) if self.bg[k] == CLEAR else self.bg[k], self.bd[k], mode)
                        if st != cur:
                            out.append(st)
                            cur = st
                        out.append(self.ch[k])
                    i += 1
        out.append("\x1b[0m")
        return "".join(out).encode("utf-8", "replace")


# ---- the map as a picture ---------------------------------------------------------------------------------------
PIC_IDS = (4242, 4243)           # two image ids, so the new picture is placed before the old one goes


_SIX = bytes((v + 63) & 255 for v in range(256))     # a 6-pixel column's bits -> its sixel character
_RUN = re.compile(rb"(.)\1{3,}")


class Picture:
    """The map in real pixels, drawn the way their canvas is: filled squares, 1-pixel grid lines, anti-aliased
    circles. One byte a pixel, an index into a palette of up to 256 colours (what Sixel needs; kitty gets
    it back as RGB), painted with whole-row slices and bytes.translate, so plain Python keeps up with
    dragging. Edges are anti-aliased in eighth steps, which keeps the palette small."""

    def __init__(self, w, h, rgb):
        self.w, self.h = w, h
        self.pal, self.ids, self.mixes, self.tabs = [], {}, {}, {}
        self.px = bytearray(self.code(rgb) * (w * h))

    def copy(self):
        p = Picture.__new__(Picture)
        p.w, p.h, p.px = self.w, self.h, bytearray(self.px)
        p.pal, p.ids, p.mixes, p.tabs = list(self.pal), dict(self.ids), dict(self.mixes), {}
        return p

    def index(self, rgb):
        i = self.ids.get(rgb)
        if i is None:
            if len(self.pal) < 256:
                i = len(self.pal)
                self.pal.append(rgb)
            else:                                          # full: the nearest colour already in it
                r, g, b = rgb >> 16, (rgb >> 8) & 255, rgb & 255
                i = min(range(256), key=lambda k: ((self.pal[k] >> 16) - r) ** 2 + (((self.pal[k] >> 8) & 255) - g) ** 2 +
                        ((self.pal[k] & 255) - b) ** 2)
            self.ids[rgb] = i
        return i

    def code(self, rgb):
        """rgb as one pixel's byte, for building rows."""
        return bytes((self.index(rgb),))

    def blend(self, i, rgb, a):
        k = (i, rgb, a)
        j = self.mixes.get(k)
        if j is None:
            j = self.mixes[k] = self.index(mix(self.pal[i], rgb, a))
        return j

    def rows(self, y0, y1, data):
        """Copy one whole pixel row (bytes, w long) to rows y0 .. y1-1."""
        w = self.w
        for y in range(max(0, y0), min(self.h, y1)):
            self.px[y * w:(y + 1) * w] = data

    def rect(self, x0, y0, x1, y1, rgb):
        x0, x1, y0, y1 = max(0, x0), min(self.w, x1), max(0, y0), min(self.h, y1)
        if x0 >= x1:
            return
        seg, w = self.code(rgb) * (x1 - x0), self.w
        for y in range(y0, y1):
            self.px[y * w + x0:y * w + x1] = seg

    def tint(self, x0, y0, x1, y1, rgb, a):
        """rgb at opacity a over the rectangle; a 1-pixel-wide one is a line."""
        x0, x1, y0, y1 = max(0, x0), min(self.w, x1), max(0, y0), min(self.h, y1)
        if x0 >= x1 or y0 >= y1 or a <= 0:
            return
        k = (rgb, round(a * 255))
        t, done = self.tabs.get(k) or self.tabs.setdefault(k, (bytearray(range(256)), set()))
        w, px = self.w, self.px
        if x1 - x0 == 1:                                   # a column: one strided slice
            parts = [slice(y0 * w + x0, (y1 - 1) * w + x0 + 1, w)]
        elif x0 == 0 and x1 == w:                          # whole rows: one slice
            parts = [slice(y0 * w, y1 * w)]
        else:
            parts = [slice(y * w + x0, y * w + x1) for y in range(y0, y1)]
        for sl in parts:
            seg = px[sl]
            for i in set(seg) - done:                      # blend only the colours that are there
                t[i] = self.blend(i, rgb, k[1] / 255)
                done.add(i)
            px[sl] = seg.translate(t)

    def dot(self, x, y, rgb, a):
        a = round(min(1.0, a) * 8) / 8
        if 0 <= x < self.w and 0 <= y < self.h and a > 0:
            o = y * self.w + x
            self.px[o] = self.blend(self.px[o], rgb, a)

    def ring(self, cx, cy, r, width, rgb, dash=None):
        """An anti-aliased circle line like canvas arc() + stroke(), dashed (on, off pixels along it) if asked."""
        hw = width / 2
        ro, ri = r + hw + 1, max(0.0, r - hw - 1)
        for y in range(max(0, math.floor(cy - ro)), min(self.h, math.ceil(cy + ro) + 1)):
            dy = y + 0.5 - cy
            o2 = ro * ro - dy * dy
            if o2 <= 0:
                continue
            xo, i2 = math.sqrt(o2), ri * ri - dy * dy
            spans = [(cx - xo, cx + xo)] if i2 <= 0 else [(cx - xo, cx - math.sqrt(i2)), (cx + math.sqrt(i2), cx + xo)]
            for lo, hi in spans:
                for x in range(max(0, math.floor(lo - 0.5)), min(self.w, math.ceil(hi + 0.5))):
                    dx = x + 0.5 - cx
                    cov = min(1.0, hw + 0.5 - abs(math.sqrt(dx * dx + dy * dy) - r))
                    if cov > 0 and not (dash and (math.atan2(dy, dx) * r) % (dash[0] + dash[1]) >= dash[0]):
                        self.dot(x, y, rgb, cov)

    def disc(self, cx, cy, r, rgb):
        for y in range(math.floor(cy - r - 1), math.ceil(cy + r + 1)):
            for x in range(math.floor(cx - r - 1), math.ceil(cx + r + 1)):
                self.dot(x, y, rgb, r + 0.5 - math.hypot(x + 0.5 - cx, y + 0.5 - cy))

    def dashed_rect(self, x0, y0, x1, y1, rgb, a, on, off):
        """A 1-pixel rectangle outline from (x0, y0) to (x1, y1) inclusive, dashed like canvas setLineDash."""
        for x in range(x0, x1 + 1, on + off):
            self.tint(x, y0, min(x + on, x1 + 1), y0 + 1, rgb, a)
            self.tint(x, y1, min(x + on, x1 + 1), y1 + 1, rgb, a)
        for y in range(y0, y1 + 1, on + off):
            self.tint(x0, y, x0 + 1, min(y + on, y1 + 1), rgb, a)
            self.tint(x1, y, x1 + 1, min(y + on, y1 + 1), rgb, a)

    def rgb(self):
        """The pixels as RGB bytes."""
        pal = self.pal + [0] * (256 - len(self.pal))
        out = bytearray(3 * len(self.px))
        for c, shift in enumerate((16, 8, 0)):
            out[c::3] = self.px.translate(bytes((v >> shift) & 255 for v in pal))
        return out

    def kitty(self, img_id, cols, rows):
        """The picture as kitty graphics: zlib-compressed RGB in 4 KB base64 chunks, placed at the cursor over
        cols x rows cells, under every cell that has its own background (so overlays like the help box cover
        it) but over the terminal's own, which is what the map cells are left on. q=2: no replies."""
        data = base64.standard_b64encode(zlib.compress(bytes(self.rgb()), 1))
        parts = [data[i:i + 4096] for i in range(0, len(data), 4096)] or [b""]
        out = [b"\x1b_Ga=T,f=24,o=z,s=%d,v=%d,i=%d,p=1,c=%d,r=%d,C=1,q=2,z=-1073741825,m=%d;%s\x1b\\" % (
            self.w, self.h, img_id, cols, rows, len(parts) > 1, parts[0])]
        out += [b"\x1b_Gm=%d;%s\x1b\\" % (k < len(parts) - 1, part) for k, part in enumerate(parts[1:], 1)]
        return b"".join(out)

    def sixel(self):
        """The picture as Sixel (Windows Terminal, WezTerm, foot, iTerm2, ...): square pixels (raster attributes
        1;1), the palette in percent, then bands of 6 rows - per colour in the band, each column's 6 bits
        (masks by translate, the rows joined as one big-integer OR), as characters from '?', repeats as !n."""
        w, h, px = self.w, self.h, self.px
        out = [b'\x1bP0;1;0q"1;1;%d;%d' % (w, h)]
        out += [b"#%d;2;%d;%d;%d" % (i, round((c >> 16) * 100 / 255), round(((c >> 8) & 255) * 100 / 255),
                                     round((c & 255) * 100 / 255)) for i, c in enumerate(self.pal)]
        masks = {}
        for y in range(0, h, 6):
            rows = [px[(y + r) * w:(y + r + 1) * w] for r in range(min(6, h - y))]
            band = []
            for k in sorted(set(px[y * w:min(h, y + 6) * w])):
                acc = 0
                for r, row in enumerate(rows):
                    if k in row:
                        t = masks.get((k, r))
                        if t is None:
                            t = bytearray(256)
                            t[k] = 1 << r
                            t = masks[(k, r)] = bytes(t)
                        acc |= int.from_bytes(row.translate(t), "big")
                line = acc.to_bytes(w, "big").translate(_SIX).rstrip(b"?")
                band.append(b"#%d" % k + _RUN.sub(lambda m: b"!%d%s" % (len(m[0]), m[1]), line))
            out.append(b"$".join(band) + (b"-" if y + 6 < h else b""))
        out.append(b"\x1b\\")
        return b"".join(out)

