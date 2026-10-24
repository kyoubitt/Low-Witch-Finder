#!/usr/bin/env python3
"""witchhut_studio.py - a biome map for any seed, and a finder for low-Y witch huts, as a full-screen app in your terminal.

  python3 witchhut_studio.py            (Windows: python witchhut_studio.py)

Two tabs:
  1 Map    a biome map of any seed, for 1.18 up to 26.2 (each version's own biome data): swamps lit up, every place a
           witch hut generates marked, the known low huts starred. "Nearest hut" zooms to the closest hut of any seed.
           Drag to pan, scroll to zoom.
  2 Hunt   look for a hut of your own: pick how low (-50 to -59), how hard the computer works (GPU or CPU, full or
           gentle) and, for the shallower floors, which seeds and how much of the world. One button starts it; it keeps
           running if you close the window. What it finds is listed here and can be shown on the map. If something is
           missing (the tools, Minecraft's files) the big button becomes the thing that fixes it.

Runs in any modern terminal; the mouse works wherever the terminal reports it, the keyboard everywhere. The map is a real
picture in terminals that show them (kitty graphics: Ghostty, kitty; Sixel: Windows Terminal 1.22+, WezTerm, iTerm2, foot,
Konsole) and half-block characters elsewhere. WITCH_MAP=text|kitty|sixel overrides the choice.
Standard library only. The map needs bin/biome_map: ./build.sh builds it with any C compiler (or press b on the Hunt tab).

  --selftest                  checks the data and the map engine, exit 0 when all pass
  --frame W H [tab] [theme]   draws one frame as plain text, no terminal needed (tab 1-2, theme light|dark)
"""
import base64
import math
import os
import shutil
import signal
import random
import subprocess
import sys
import threading
import time
import types

ROOT = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, ROOT)
import tui_core as T                                                   # noqa: E402
from tui_core import CLEAR, PIC_IDS, Picture, Screen, Term, mix, parse_input, twidth, wrap   # noqa: E402
import witchhut_common as W                                            # noqa: E402
import hunt                                                            # noqa: E402
import mcfiles                                                         # noqa: E402

NAME = "WitchHut"        # working title; one place to rename

# ---- colours: a swamp. Moss and mud for the page, a witch-purple accent, a lit-up lime for the swamp itself -----------
THEMES = {
    "light": dict(
        PAGE=0xeef0e6, CARD=0xfbfcf7, CARD2=0xe4e9d9, LINE=0xcdd5be, TEXT=0x222a1b, MUTED=0x646e56,
        ACCENT=0x5a4690, ON_ACCENT=0xffffff, SWAMPC=0x5f8a12, WARN=0x8a5a14, DANGER=0xa63a32, OKC=0x3a7a2c,
        PICK=0xe6dff4, PICKEDGE=0x8f7fc8, HOVER=0xd9d1ee, FIELD_EDGE=0xb4bda4, TOAST_BG=0x3a2f5e, TOAST_TEXT=0xffffff,
        MAPBG=0x1b2217),
    "dark": dict(
        PAGE=0x11140e, CARD=0x181c13, CARD2=0x222819, LINE=0x2f3724, TEXT=0xe0e6d2, MUTED=0x9aa58a,
        ACCENT=0xb7a2ee, ON_ACCENT=0x15101f, SWAMPC=0xb4e03a, WARN=0xe5b56a, DANGER=0xf0877c, OKC=0x8fd27a,
        PICK=0x2b2441, PICKEDGE=0x6c5aa8, HOVER=0x332a52, FIELD_EDGE=0x424b33, TOAST_BG=0x2e2650, TOAST_TEXT=0xe0d6ff,
        MAPBG=0x0e120b),
}
THEME = "light"


def apply_theme(name):
    global THEME
    THEME = name if name in THEMES else "light"
    globals().update(THEMES[THEME])
    T.set_base(THEMES[THEME]["TEXT"], THEMES[THEME]["PAGE"])


apply_theme("light")
HUT_COL = 0xffd23f                          # a place a hut generates
DEPTH_COL = {-59: 0xff5fa2, -58: 0xff9a3a, -57: 0xffd23f}   # known low huts, by depth
SWAMP_COL = 0xb4e03a
ORIGIN_COL = 0xffffff
HELP = [("Everywhere", None), ("1  2", "Map, Hunt"), ("t", "light or dark"), ("?", "this help"), ("q", "quit (a hunt keeps going)"),
        ("", None), ("Map", None), ("z", "nearest hut"), ("r", "random seed"), ("e", "type a seed"), ("drag, scroll", "pan, zoom"),
        ("k", "known low huts"), ("[  ]", "game version"), ("click a hut", "select it; c copies its /tp"),
        ("", None), ("Hunt", None), ("up down, left right", "pick a setting, change it"), ("s  x", "start, stop"),
        ("c  m", "copy / show on the map the selected hut"), ("n", "new random start (-55 and deeper)"),
        ("", None), ("Known huts popup", None), ("up down, left right", "move, filter by depth"), ("o", "closest or deepest first"),
        ("enter", "zoom to it"), ("esc", "close")]


def fmt(n):
    return format(round(n), ",")


def short_dist(d):
    return fmt(d) if d < 1_000_000 else "%.1fM" % (d / 1e6)


# ---- clipboard (the system one when there is one, OSC 52 otherwise) -------------------------------------------------
def _pipe(cmd, text):
    r = subprocess.run(cmd, input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    return r.returncode == 0


def _win_clipboard(text):
    import ctypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GlobalAlloc.restype = k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    data = text.encode("utf-16-le") + b"\0\0"
    if not u.OpenClipboard(None):
        return False
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(0x0002, len(data))
        p = k.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k.GlobalUnlock(h)
        return bool(u.SetClipboardData(13, h))
    finally:
        u.CloseClipboard()


def copy_text(term, text):
    """The system clipboard when there is one here; always also OSC 52, which most terminals turn into a copy."""
    ok = False
    try:
        if os.name == "nt":
            ok = _win_clipboard(text)
        elif sys.platform == "darwin":
            ok = _pipe(["pbcopy"], text)
        elif not os.environ.get("SSH_CONNECTION"):
            if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
                ok = _pipe(["wl-copy"], text)
            elif os.environ.get("DISPLAY") and shutil.which("xclip"):
                ok = _pipe(["xclip", "-selection", "clipboard"], text)
            elif os.environ.get("DISPLAY") and shutil.which("xsel"):
                ok = _pipe(["xsel", "-ib"], text)
    except (OSError, subprocess.SubprocessError):
        ok = False
    if term is not None:
        term.write(b"\x1b]52;c;" + base64.b64encode(text.encode("utf-8")) + b"\x07")
    return ok


# ---- the map's data, fetched off the drawing thread ------------------------------------------------------------------
class MapJob:
    kind = "map"

    def __init__(self, seed, ver, step, x0, z0, w, h, ss):
        self.seed, self.ver, self.step, self.x0, self.z0, self.w, self.h, self.ss = seed, ver, step, x0, z0, w, h, ss
        self.key = (seed, ver, step, x0, z0, w, h, ss)


class NearJob:
    kind = "near"

    def __init__(self, seed, ver, x, z):
        self.seed, self.ver, self.x, self.z = seed, ver, x, z
        self.key = (seed, ver, x // 256, z // 256)


def ss_for(step):
    """Sample points per cell side: one is exact up close; farther out a cell is mostly a mix, so look at 2 x 2, then 3 x 3."""
    return 1 if step <= 64 else 2 if step <= 256 else 3


def compute_map(job):
    out = dict(key=job.key, seed=job.seed, ver=job.ver, x0=job.x0, z0=job.z0, step=job.step, w=job.w, h=job.h,
               grid=None, huts=[], far=False, err=None)
    try:
        out["grid"] = W.biome_grid(job.seed, job.x0, job.z0, job.w, job.h, job.step, job.ver, job.ss)
        huts = W.hut_spots(job.seed, job.x0, job.z0, job.x0 + job.w * job.step, job.z0 + job.h * job.step, job.ver)
        out["far"], out["huts"] = huts is None, huts or []
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        out["err"] = str(e) or "the map engine failed"
    return out


def compute_near(job):
    out = dict(key=job.key, huts=[], err=None)
    try:
        out["huts"] = W.nearest_huts(job.seed, job.ver, job.x, job.z, 12)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        out["err"] = str(e) or "the map engine failed"
    return out


class MapWorker(threading.Thread):
    """Computes map windows and nearest-hut lists off the drawing thread; each kind keeps only its latest request."""
    def __init__(self, done):
        super().__init__(daemon=True)
        self.done, self.ev, self.lock, self.jobs = done, threading.Event(), threading.Lock(), {}

    def submit(self, job):
        with self.lock:
            self.jobs[job.kind] = job
        self.ev.set()

    def run(self):
        while True:
            self.ev.wait()
            self.ev.clear()
            while True:
                with self.lock:
                    kind = next((k for k in ("map", "near") if k in self.jobs), None)
                    job = self.jobs.pop(kind) if kind else None
                if job is None:
                    break
                self.done(kind, compute_map(job) if kind == "map" else compute_near(job))


def paint_layer(canvas, r, x0, z0, step, pw, ph):
    """Copy what a fetched window r knows into the view's rows (bytearrays, pw wide, ph of them) with row slices, not per cell.
    The view's cell (i, j) is the block square starting at (x0 + i*step, z0 + j*step). A window as fine as the view or finer
    gives its middle sample of each cell; a coarser one is repeated over the cells it covers. Steps are all 16 * 2^n, so the
    mapping is exact integers: this is the same answer as asking each cell for its sample, only fast."""
    g, rw, rh, rs = r["grid"], r["w"], r["h"], r["step"]
    if g is None:
        return
    if rs <= step:
        ratio = step // rs
        half = ratio // 2
        c0, r0 = (x0 - r["x0"]) // rs + half, (z0 - r["z0"]) // rs + half     # the window's column / row for view cell (0, 0)
        i_lo = max(0, -(-(-c0) // ratio))
        i_hi = min(pw, (rw - 1 - c0) // ratio + 1) if rw - 1 - c0 >= 0 else 0
        if i_lo >= i_hi:
            return
        for j in range(ph):
            rr = r0 + j * ratio
            if 0 <= rr < rh:
                b = rr * rw + c0
                canvas[j][i_lo:i_hi] = g[b + i_lo * ratio: b + (i_hi - 1) * ratio + 1: ratio]
    else:
        ratio = rs // step
        off, zoff = (x0 - r["x0"]) // step, (z0 - r["z0"]) // step           # view cell 0 in window-cell units
        i_lo, i_hi = max(0, -off), min(pw, rw * ratio - off)
        if i_lo >= i_hi:
            return
        c_first, c_last = (off + i_lo) // ratio, (off + i_hi - 1) // ratio
        phase = (off + i_lo) - c_first * ratio
        last_rr, seg = None, None
        for j in range(ph):
            rr = (zoff + j) // ratio
            if not 0 <= rr < rh:
                continue
            if rr != last_rr:
                part = g[rr * rw + c_first: rr * rw + c_last + 1]
                seg = bytearray(len(part) * ratio)
                for t in range(ratio):
                    seg[t::ratio] = part
                last_rr = rr
            canvas[j][i_lo:i_hi] = seg[phase: phase + (i_hi - i_lo)]


# ---- the app ---------------------------------------------------------------------------------------------------------
TABS = [("map", "Map", "1"), ("hunt", "Hunt", "2")]
DEPTH_FILTERS = [None, -59, -58, -57, -56, -55]
MAX_ZOOM = 5          # 512 blocks per column: past that, biome patches (a few hundred blocks) are smaller than a pixel and the map is static


class App:
    def __init__(self, term=None, sync=False):
        self.term, self.sync = term, sync
        self.w, self.h = 100, 30
        self.tab, self.help, self.dirty, self.known_open = "map", False, True, False
        self.hits, self.mouse, self.toast = [], None, ("", 0)
        # the known huts (a popup on the map)
        self.base_finds = W.load_finds()
        self.finds = list(self.base_finds)        # plus whatever the finder finds on this machine
        # the finder (Hunt tab): what is picked, what the last check found, a build or download in progress
        self.h_set = dict(level=-58, mode="gpu", seeds="", area=len(hunt.AREAS) - 1)
        self.h_focus, self.h_edit, self.h_before, self.h_gen = "level", False, "", 0
        self.h_info, self.h_rows, self.h_sel, self.h_top = None, [], 0, 0
        self.h_next, self.h_busy, self.h_machine, self.h_mach_t = 0.0, False, None, 0.0
        self.h_log, self.h_step_name = [], ""
        self.f_filter, self.f_order, self.f_sel, self.f_top, self.f_rows = 0, "close", 0, 0, 10
        # map
        self.m_text, self.m_seed, self.m_note = "795374283637013279", 795374283637013279, ""
        self.m_ver = W.DEFAULT_VERSION
        self.m_cx, self.m_cz, self.m_zoom, self.m_hl = 5811, 4423, 2, False
        self.m_edit, self.m_rect = False, (0, 0, 10, 10)
        self.pic_req, self.pic_on, self.pic_sent, self.pic_cache, self.prev, self.scr = None, None, None, (None, None), None, None
        self.pending = ""                  # input cut off mid-sequence (a lone Esc waits here a moment)
        self.m_drag, self.m_sel, self.m_marks = None, None, []
        self.m_pic, self.m_near, self.m_near_req, self.m_near_busy = None, None, None, False
        self.m_res, self.m_cache, self.m_req, self.m_busy = None, {}, None, False
        self.worker = None if sync else MapWorker(self.map_done)
        if self.worker:
            self.worker.start()
        self.f_pick()
        self.sorted_finds()

    # ---- helpers -----------------------------------------------------------------------------------------------------
    def say(self, text, secs=2.5):
        self.toast, self.dirty = (text, time.monotonic() + secs), True

    def copy(self, text, what):
        ok = copy_text(self.term, text)
        self.say("Copied %s" % what if ok else "Copied %s (sent to the terminal's clipboard)" % what)

    def ver(self):
        return W.VERSIONS[self.m_ver]

    # ---- finds -------------------------------------------------------------------------------------------------------
    def sorted_finds(self):
        d = DEPTH_FILTERS[self.f_filter]
        rows = [f for f in self.finds if d is None or f.depth == d]
        if self.f_order == "close":
            rows.sort(key=lambda f: (f.dist, f.depth))
        else:
            rows.sort(key=lambda f: (f.depth, f.dist))
        self.rows = rows
        self.f_sel = max(0, min(self.f_sel, len(rows) - 1))
        return rows

    def f_pick(self):
        self.f_sel = 0
        self.f_top = 0

    def selected_find(self):
        rows = self.sorted_finds()
        return rows[self.f_sel] if rows else None

    def f_move(self, d):
        rows = self.sorted_finds()
        if rows:
            self.f_sel = max(0, min(len(rows) - 1, self.f_sel + d))
        self.dirty = True

    def f_set_filter(self, i):
        self.f_filter = i % len(DEPTH_FILTERS)
        self.f_pick()
        self.sorted_finds()
        self.dirty = True

    def show_on_map(self, f):
        if f is None:
            return
        if not f.located:
            self.say("That one has no coordinates in the list")
            return
        self.m_text, self.m_seed, self.m_note = f.seed, int(f.seed), ""
        self.m_ver = next(i for i, v in enumerate(W.VERSIONS) if v[0] == "1.21.4")   # the version the huts were verified on
        self.m_cx, self.m_cz, self.m_zoom = f.x, f.z, 0
        self.m_sel = (f.x >> 4, f.z >> 4)
        self.tab, self.known_open, self.dirty = "map", False, True

    def open_known(self):
        self.known_open, self.m_edit, self.dirty = True, False, True

    def close_known(self):
        self.known_open, self.dirty = False, True

    def known_here(self):
        """The known low huts of the seed on the map, deepest first."""
        if self.m_seed is None:
            return []
        return sorted((f for f in self.finds if f.located and f.seed == str(self.m_seed)), key=lambda f: (f.depth, f.dist))

    # ---- map: state, geometry and data -----------------------------------------------------------------------------------
    # The view is a grid of SAMPLES. In characters a sample is one column by half a row (zoomed in) or two columns by one row
    # (zoomed out: bigger pixels, a quarter of the data to describe); in a picture it is a fraction of a cell. A sample covers
    # `step` blocks; the map always shows the same number of blocks per column whatever the sample size.
    def map_done(self, kind, res):
        if kind == "near":
            self.m_near, self.m_near_busy, self.dirty = res, False, True
            return
        self.m_cache[res["key"]] = res
        if len(self.m_cache) > 24:
            self.m_cache.pop(next(iter(self.m_cache)))
        self.m_res, self.dirty = res, True
        if res["key"] == self.m_req:
            self.m_busy = False

    def m_apply_seed(self):
        v, note = W.parse_seed(self.m_text)
        if v is None:
            self.say("Type a seed first")
            return
        self.m_seed, self.m_note, self.m_edit = v, note or "", False
        self.m_res, self.m_sel, self.m_near, self.dirty = None, None, None, True

    def m_random(self):
        """A fresh random seed, straight to its nearest witch hut."""
        v = random.SystemRandom().getrandbits(64)
        v -= (1 << 64) if v >= 1 << 63 else 0
        self.m_text, self.m_seed, self.m_note, self.m_edit = str(v), v, "random seed", False
        self.m_res, self.m_sel, self.m_near, self.m_cx, self.m_cz = None, None, None, 0, 0
        try:
            near = W.nearest_huts(v, self.ver()[0], 0, 0, 1)
        except RuntimeError:
            near = []
        if near:
            self.m_goto_hut(near[0][:2], 1)
        else:
            self.m_zoom = 3
        self.dirty = True

    def m_params(self, zoom):
        """(samples per column, samples per row, columns per sample) at this zoom."""
        if self.m_pic and zoom <= 3:
            return 2, self.m_pic[2], 1
        return 1, 2, (2 if zoom >= 4 and not self.m_pic else 1)

    def m_scale(self, zoom=None):
        """(blocks per column, blocks per row) at this zoom."""
        z = self.m_zoom if zoom is None else zoom
        sx, sy, _ = self.m_params(z)
        return (16 << z) * sx, (16 << z) * sy

    def m_view(self):
        """(blocks per sample, block x and z of sample 0,0, samples across, samples down)."""
        ox, oy, mw, mh = self.m_rect
        sx, sy, ps = self.m_params(self.m_zoom)
        step = (16 << self.m_zoom) * ps
        pw, ph = -(-mw * sx // ps), -(-mh * sy // ps)
        return step, ((self.m_cx - (pw // 2) * step) // step) * step, ((self.m_cz - (ph // 2) * step) // step) * step, pw, ph

    def m_window(self):
        """The window to fetch: the view plus a margin, snapped to a grid so a small pan reuses what is already there."""
        step, x0, z0, pw, ph = self.m_view()
        g = 8 * step
        lx, lz = ((x0 - 16 * step) // g) * g, ((z0 - 16 * step) // g) * g
        hx, hz = -(-(x0 + (pw + 16) * step) // g) * g, -(-(z0 + (ph + 16) * step) // g) * g
        return step, lx, lz, min(4096, (hx - lx) // step), min(4096, (hz - lz) // step)

    def m_request(self):
        if self.m_seed is None:
            return
        have = self.m_covering()
        if have is not None:                                    # a fetched window still covers the view with room to spare
            self.m_res = have
        else:
            step, x0, z0, w, h = self.m_window()
            job = MapJob(self.m_seed, self.ver()[0], step, x0, z0, w, h, ss_for(step))
            if job.key in self.m_cache:
                self.m_res = self.m_cache[job.key]
            elif not (self.m_req == job.key and self.m_busy):
                self.m_req, self.m_busy = job.key, True
                if self.sync:
                    self.map_done("map", compute_map(job))
                else:
                    self.worker.submit(job)
        near = NearJob(self.m_seed, self.ver()[0], self.m_cx, self.m_cz)
        if not ((self.m_near and self.m_near["key"] == near.key) or (self.m_near_req == near.key and self.m_near_busy)):
            self.m_near_req, self.m_near_busy = near.key, True
            if self.sync:
                self.map_done("near", compute_near(near))
            else:
                self.worker.submit(near)

    def m_covering(self, margin=4):
        """A fetched window at this zoom that covers the view with at least `margin` samples to spare on every side, or None.
        While one does, panning needs no new fetch; the new one is asked for when the view nears the edge."""
        step, x0, z0, pw, ph = self.m_view()
        seed, ver = self.m_seed, self.ver()[0]
        for r in reversed(list(self.m_cache.values())):
            if (r["seed"] == seed and r["ver"] == ver and r["step"] == step and r["grid"] is not None
                    and r["x0"] <= x0 - margin * step and r["x0"] + r["w"] * step >= x0 + (pw + margin) * step
                    and r["z0"] <= z0 - margin * step and r["z0"] + r["h"] * step >= z0 + (ph + margin) * step):
                return r
        return None

    def m_layers(self):
        """Every fetched window for this seed and version, coarsest first, so the finest paints on top."""
        seed, ver = self.m_seed, self.ver()[0]
        return sorted((r for r in self.m_cache.values() if r["seed"] == seed and r["ver"] == ver and r["grid"] is not None),
                      key=lambda r: -r["step"])

    def m_cell_sample(self, mx, my):
        """The sample under screen cell (mx, my)."""
        ox, oy = self.m_rect[:2]
        sx, sy, ps = self.m_params(self.m_zoom)
        return (((mx - ox) * 2 + 1) * sx) // (2 * ps), (((my - oy) * 2 + 1) * sy) // (2 * ps)

    def m_sample_cell(self, i, j):
        """The screen cell where sample (i, j) starts."""
        ox, oy = self.m_rect[:2]
        sx, sy, ps = self.m_params(self.m_zoom)
        return ox + (i * ps) // sx, oy + (j * ps) // sy

    def m_block_at(self, mx, my):
        """The block coordinates at the middle of the sample under screen cell (mx, my), or None off the map."""
        ox, oy, mw, mh = self.m_rect
        if not (ox <= mx < ox + mw and oy <= my < oy + mh):
            return None
        step, x0, z0, _, _ = self.m_view()
        i, j = self.m_cell_sample(mx, my)
        return x0 + i * step + step // 2, z0 + j * step + step // 2

    def m_biome_at(self, bx, bz):
        for r in reversed(self.m_layers()):                     # the finest window that covers the block
            ci, ri = (bx - r["x0"]) // r["step"], (bz - r["z0"]) // r["step"]
            if 0 <= ci < r["w"] and 0 <= ri < r["h"]:
                return r["grid"][ri * r["w"] + ci]
        return None

    def m_zoom_to(self, z, mx=None, my=None):
        z = max(0, min(MAX_ZOOM, z))
        if z == self.m_zoom:
            return
        ox, oy, mw, mh = self.m_rect
        if mx is not None and ox <= mx < ox + mw and oy <= my < oy + mh:     # keep the block under the pointer still
            bc, br = self.m_scale()
            dc, dr = (mx - ox + 0.5) - mw / 2, (my - oy + 0.5) - mh / 2
            nc, nr = self.m_scale(z)
            self.m_cx, self.m_cz = round(self.m_cx + dc * (bc - nc)), round(self.m_cz + dr * (br - nr))
        self.m_zoom, self.dirty = z, True

    def m_pan(self, dcols, drows):
        bc, br = self.m_scale()
        lim = 30_000_000
        self.m_cx = max(-lim, min(lim, self.m_cx + round(dcols * bc)))
        self.m_cz = max(-lim, min(lim, self.m_cz + round(drows * br)))
        self.dirty = True

    def m_goto_hut(self, c, zoom=0):
        """Centre on a hut (chunk coordinates) and zoom in to it."""
        self.m_cx, self.m_cz, self.m_zoom, self.m_sel, self.dirty = c[0] * 16 + 8, c[1] * 16 + 8, zoom, c, True

    def m_zoom_hut(self):
        """Zoom to the selected hut, or to the nearest one to the middle of the view. Works for any seed."""
        c = self.m_sel
        if c is None:
            if self.m_seed is None:
                self.say("Type a seed first")
                return
            try:
                near = W.nearest_huts(self.m_seed, self.ver()[0], self.m_cx, self.m_cz, 1)
            except RuntimeError as e:
                self.say(str(e), 5)
                return
            if not near:
                self.say("No witch hut within 60,000 blocks of here")
                return
            c = near[0][:2]
        self.m_goto_hut(c)

    def m_near_list(self):
        """The nearest huts to the middle of the view: [(chunkX, chunkZ, distance, known low hut or None)], nearest first."""
        if not self.m_near:
            return []
        known = {(f.x >> 4, f.z >> 4): f for f in self.known_here()}
        out = [(cx, cz, math.hypot(cx * 16 + 8 - self.m_cx, cz * 16 + 8 - self.m_cz), known.get((cx, cz)))
               for cx, cz, _ in self.m_near["huts"]]
        return sorted(out, key=lambda t: t[2])

    # ---- drawing: pieces ------------------------------------------------------------------------------------------------
    def button(self, s, x, y, label, act, primary=False):
        w = twidth(label) + 2
        over = self.mouse is not None and self.mouse[1] == y and x <= self.mouse[0] < x + w
        bg = (mix(ACCENT, 0xffffff, 0.12) if over else ACCENT) if primary else (HOVER if over else CARD2)
        s.put(x, y, " " + label + " ", ON_ACCENT if primary else TEXT, bg, True)
        self.hits.append((x, y, w, 1, act))
        return x + w + 1

    def chip(self, s, x, y, label, on, act):
        w = twidth(label) + 2
        over = self.mouse is not None and self.mouse[1] == y and x <= self.mouse[0] < x + w
        if on:
            s.put(x, y, " " + label + " ", ON_ACCENT, ACCENT, True)
        else:
            s.put(x, y, " " + label + " ", TEXT if over else MUTED, HOVER if over else CARD2)
        self.hits.append((x, y, w, 1, act))
        return x + w + 1

    def card(self, s, x, y, w, h, title=""):
        s.fill(x, y, w, h, CARD)
        s.box(x, y, w, h, LINE)
        if title:
            s.put(x + 2, y, " " + title + " ", ACCENT, CARD, True)

    def label_value(self, s, x, y, w, label, value, vcol=None, bold=False):
        s.put(x, y, label, MUTED, CARD)
        s.put(x + twidth(label) + 1, y, value[:max(0, w - twidth(label) - 1)], vcol or TEXT, CARD, bold)

    # ---- drawing: frame ---------------------------------------------------------------------------------------------------
    def draw(self, s):
        self.hits = []
        w, h = s.w, s.h
        self.w, self.h = w, h
        s.fill(0, 0, w, h, PAGE)
        if w < 66 or h < 16:
            s.put(2, 1, "Make the window a bit bigger (66 x 16 or more).", TEXT, PAGE)
            return
        self.draw_header(s)
        top, bottom = 2, h - 1
        {"map": self.draw_map, "hunt": self.draw_hunt}[self.tab](s, top, bottom)
        self.draw_footer(s)
        if self.known_open:
            self.draw_known(s)
        if self.help:
            self.draw_help(s)

    def draw_header(self, s):
        w = s.w
        s.put(2, 0, "⌂", HUT_COL, PAGE, True)
        s.put(4, 0, NAME, ACCENT, PAGE, True)
        x = 2 + twidth(NAME) + 6
        for tid, name, key in TABS:
            label = " %s %s " % (key, name)
            over = self.mouse is not None and self.mouse[1] == 0 and x <= self.mouse[0] < x + twidth(label)
            if tid == self.tab:
                s.put(x, 0, label, ON_ACCENT, ACCENT, True)
            else:
                s.put(x, 0, label, TEXT if over else MUTED, HOVER if over else PAGE)
            self.hits.append((x, 0, twidth(label), 1, (lambda tid=tid: self.set_tab(tid))))
            x += twidth(label) + 1
        hint = "t theme   ? help   q quit"
        if w - 2 - twidth(hint) > x:
            s.put(w - 2 - twidth(hint), 0, hint, MUTED, PAGE)
        s.hline(0, 1, w, LINE)

    def set_tab(self, tid):
        self.tab, self.m_edit, self.dirty = tid, False, True

    def draw_footer(self, s):
        y = s.h - 1
        s.fill(0, y, s.w, 1, PAGE)
        text, until = self.toast
        if text and time.monotonic() < until:
            t = " " + text + " "
            s.put(2, y, t[:s.w - 4], TOAST_TEXT, TOAST_BG, True)
            return
        hints = {"map": ["drag pan", "scroll zoom", "z nearest hut", "k known huts", "e seed", "[ ] version"],
                 "hunt": ["up/down field", "left/right change", "s start", "x stop", "c copy a /tp", "m show on the map", "r check again"]}[self.tab]
        if self.known_open:
            hints = ["up/down move", "left/right depth", "o order", "enter open on the map", "esc close"]
        s.put(2, y, "   ".join(hints)[:s.w - 4], MUTED, PAGE)

    def draw_help(self, s):
        bw, bh = 62, len(HELP) + 4
        bw, bh = min(bw, s.w - 4), min(bh, s.h - 2)
        x, y = (s.w - bw) // 2, (s.h - bh) // 2
        self.card(s, x, y, bw, bh, "Keys")
        for i, (a, b) in enumerate(HELP[:bh - 3]):
            if b is None:
                s.put(x + 2, y + 1 + i, a, ACCENT, CARD, True)
            else:
                s.put(x + 4, y + 1 + i, a, TEXT, CARD, True)
                s.put(x + 22, y + 1 + i, b[:bw - 24], MUTED, CARD)
        s.put(x + 2, y + bh - 1, " any key closes this ", MUTED, CARD)

    # ---- drawing: the known huts popup ----------------------------------------------------------------------------------------
    def draw_known(self, s):
        w, h = s.w, s.h
        bw, bh = min(w - 4, 88), min(h - 2, 34)
        x, y = (w - bw) // 2, (h - bh) // 2
        self.hits.append((0, 0, w, h, self.close_known))               # a click outside closes it (the popup's own hits win)
        rows = self.sorted_finds()
        self.card(s, x, y, bw, bh, "Known low huts")
        self.hits.append((x, y, bw, bh, lambda: None))
        cx = x + 2
        for i, d in enumerate(DEPTH_FILTERS):
            n = sum(1 for f in self.finds if d is None or f.depth == d)
            cx = self.chip(s, cx, y + 1, ("All %d" % n) if d is None else ("%d  %d" % (d, n)), i == self.f_filter,
                           (lambda i=i: self.f_set_filter(i)))
        self.button(s, x + 2, y + 2, "order: " + ("closest first" if self.f_order == "close" else "deepest first"), self.toggle_order)
        cnt = "%d huts" % len(rows)
        s.put(x + bw - twidth(cnt) - 3, y + 2, cnt, MUTED, CARD)
        hy = y + 4
        cols = (x + 2, x + 8, x + 19, x + 40)
        for cxx, name in zip(cols, ("depth", "from 0,0", "seed", "x           z")):
            if cxx + 8 < x + bw:
                s.put(cxx, hy, name, MUTED, CARD, True)
        s.hline(x + 1, hy + 1, bw - 2, LINE, "─")
        first = hy + 2
        self.f_rows = max(1, y + bh - 2 - first)
        if self.f_sel < self.f_top:
            self.f_top = self.f_sel
        if self.f_sel >= self.f_top + self.f_rows:
            self.f_top = self.f_sel - self.f_rows + 1
        if not rows:
            s.put(x + 3, first, "No huts at this depth.", MUTED, CARD)
        for k in range(self.f_rows):
            i = self.f_top + k
            if i >= len(rows):
                break
            f, yy = rows[i], first + k
            sel = i == self.f_sel
            over = self.mouse is not None and self.mouse[1] == yy and x + 1 <= self.mouse[0] < x + bw - 1
            bg = PICK if sel else (HOVER if over else CARD)
            s.fill(x + 1, yy, bw - 2, 1, bg)
            if sel:
                s.put(x + 1, yy, "▌", ACCENT, bg)
            s.put(cols[0], yy, str(f.depth), DEPTH_COL.get(f.depth, TEXT), bg, True)
            s.put(cols[1], yy, short_dist(f.dist) if f.located else "-", TEXT, bg)
            s.put(cols[2], yy, f.seed, TEXT, bg)
            s.push(x + 1, yy, x + bw - 1, yy + 1)
            s.put(cols[3], yy, ("%d" % f.x).ljust(12) + ("%d" % f.z) if f.located else "no coordinates in the list",
                  TEXT if f.located else MUTED, bg)
            s.pop()
            self.hits.append((x + 1, yy, bw - 2, 1, (lambda i=i: self.known_pick(i))))
        if len(rows) > self.f_rows:
            thumb = max(1, self.f_rows * self.f_rows // len(rows))
            pos = first + (self.f_rows - thumb) * self.f_top // max(1, len(rows) - self.f_rows)
            s.fill(x + bw - 2, first, 1, self.f_rows, CARD)
            s.fill(x + bw - 2, pos, 1, thumb, LINE)
        s.put(x + 2, y + bh - 1, " click or enter: zoom to it on the map ", MUTED, CARD)

    def known_pick(self, i):
        rows = self.sorted_finds()
        if 0 <= i < len(rows):
            self.f_sel = i
            self.show_on_map(rows[i])

    def toggle_order(self):
        self.f_order = "deep" if self.f_order == "close" else "close"
        self.f_pick()
        self.sorted_finds()
        self.dirty = True

    def on_known(self, ev):
        """Keys while the popup is open."""
        if ev[0] == "char":
            c = ev[1].lower()
            if c == "o":
                self.toggle_order()
            elif c in ("k", "q"):
                self.close_known()
            elif c == "?":
                self.help, self.dirty = True, True
            return
        k, n = ev[1], self.f_rows
        if k == "ctrl+c":
            raise SystemExit
        steps = {"up": -1, "down": 1, "pgup": -n, "pgdn": n, "home": -10 ** 6, "end": 10 ** 6}
        if k in steps:
            self.f_move(steps[k])
        elif k == "left":
            self.f_set_filter(self.f_filter - 1)
        elif k == "right":
            self.f_set_filter(self.f_filter + 1)
        elif k == "enter":
            self.show_on_map(self.selected_find())
        elif k == "esc":
            self.close_known()

    # ---- drawing: map ----------------------------------------------------------------------------------------------------
    def draw_toolbar(self, s, y):
        w = s.w
        x = 2
        s.put(x, y, "Seed", MUTED, PAGE)
        x += 5
        fw = max(10, min(24, w - 66))                       # the seed field gives way first on a narrow window
        edge = ACCENT if self.m_edit else FIELD_EDGE
        s.fill(x, y, fw, 1, CARD)
        shown = self.m_text
        while twidth(shown) > fw - 3:
            shown = shown[1:]
        s.put(x, y, " " + shown + ("▏" if self.m_edit else ""), TEXT, CARD)
        s.put(x + fw, y, "▕", edge, PAGE)
        s.put(x - 1, y, "▏", edge, PAGE)
        self.hits.append((x, y, fw, 1, self.m_focus))
        x += fw + 2
        if w >= 90:
            x = self.button(s, x, y, "Random", self.m_random)
        v = self.ver()
        x = self.button(s, x, y, "‹", (lambda: self.m_next_ver(-1)))
        label = "%s%s" % (v[1], "" if v[3] else "*")
        s.put(x, y, label, TEXT, PAGE, True)
        x += twidth(label) + 1
        x = self.button(s, x, y, "›", (lambda: self.m_next_ver(1))) + 1
        x = self.button(s, x, y, "Nearest hut", self.m_zoom_hut, True)
        x = self.button(s, x, y, "–", (lambda: self.m_zoom_to(self.m_zoom + 1)))
        if w >= 110:
            lab = "%s/col" % fmt(self.m_scale()[0])
            s.put(x, y, lab, MUTED, PAGE)
            x += twidth(lab) + 1
        x = self.button(s, x, y, "+", (lambda: self.m_zoom_to(self.m_zoom - 1)))
        if x + 9 <= w - 1:
            self.button(s, x, y, "Known", self.open_known)

    def m_focus(self):
        self.m_edit, self.dirty = True, True

    def m_toggle_hl(self):
        self.m_hl, self.dirty = not self.m_hl, True

    def m_next_ver(self, d):
        self.m_ver = (self.m_ver + d) % len(W.VERSIONS)
        self.m_res, self.dirty = None, True

    def palette(self):
        key = (THEME, self.m_hl)
        if getattr(self, "_palkey", None) != key:
            dim = 0.72 if self.m_hl else 0.32
            self._pal = [mix(W.biome_rgb(i), MAPBG, dim) if i != W.SWAMP else SWAMP_COL for i in range(256)]
            self._empty = mix(MAPBG, 0x808080, 0.08)
            self._pal[255] = self._empty
            self._palkey = key
        return self._pal

    def pic_info(self, mw, mh):
        """(protocol, (picture w, h in pixels), samples per cell down) when this terminal can show the map as a picture right
        now, else None. Sixel can't sit under a box, so it is characters while the help or the known huts box is open."""
        t = self.term
        g = getattr(t, "graphics", False) if t else False
        if not g or (g == "sixel" and (self.help or self.known_open)):
            return None
        cell = t.cell_px()
        if not cell:
            return None
        k = min(1.0, 2400 / (mw * cell[0])) if g == "kitty" else 1.0       # kitty scales a picture of up to 2400 px wide
        size = (max(1, round(mw * cell[0] * k)), max(1, round(mh * cell[1] * k)))
        return g, size, max(2, min(6, round(2 * cell[1] / cell[0])))        # square samples: 2 across, about 4 down

    def draw_map(self, s, top, bottom):
        w = s.w
        wide = w >= 104
        self.draw_toolbar(s, top)
        panel = 34 if wide else 0
        mx, my = 1, top + 1
        mw = w - 2 - (panel + 1 if wide else 0)
        mh = bottom - my - (1 if wide else 2)
        self.m_pic = self.pic_info(mw, mh)
        self.m_rect = (mx, my, mw, mh)
        self.m_request()
        self.draw_pixels(s, mx, my, mw, mh)
        self.draw_map_status(s, 1, my + mh, mw if wide else w - 2)
        if wide:
            self.draw_map_panel(s, mx + mw + 1, top + 1, panel, bottom - top - 1)
        else:
            self.draw_map_actions(s, 1, my + mh + 1, w - 2)

    def m_rows(self, x0, z0, step, pw, ph):
        """The biome ids for the view, one bytearray per sample row (255 = nothing yet). Whatever has been fetched fills it, so
        a pan or zoom shows the old map moved while the new one is drawn."""
        canvas = [bytearray(b"\xff" * pw) for _ in range(ph)]
        for r in self.m_layers():
            if r["x0"] < x0 + pw * step and r["x0"] + r["w"] * r["step"] > x0 and r["z0"] < z0 + ph * step \
                    and r["z0"] + r["h"] * r["step"] > z0:
                paint_layer(canvas, r, x0, z0, step, pw, ph)
        return canvas

    def m_markers(self, x0, z0, step, pw, ph):
        """Where the huts are in the view: [(sample x, sample y, screen col, screen row, chunk, known find or None)]."""
        known = {}
        if self.m_seed is not None:
            for f in self.finds:
                if f.located and f.seed == str(self.m_seed):
                    known[(f.x >> 4, f.z >> 4)] = f
        spots = set(known)
        for r in self.m_layers():
            spots.update(r["huts"])
        out = []
        for (cx, cz) in spots:
            i, j = (cx * 16 + 8 - x0) // step, (cz * 16 + 8 - z0) // step
            if 0 <= i < pw and 0 <= j < ph:
                col, row = self.m_sample_cell(i, j)
                out.append((i, j, col, row, (cx, cz), known.get((cx, cz))))
        self.m_marks = [(col, row, c, f) for _, _, col, row, c, f in out]
        return out

    def draw_pixels(self, s, ox, oy, mw, mh):
        pal = self.palette()
        step, x0, z0, pw, ph = self.m_view()
        rows = self.m_rows(x0, z0, step, pw, ph)
        marks = self.m_markers(x0, z0, step, pw, ph)
        r = self.m_res
        plate = mix(MAPBG, 0x000000, 0.4)
        if self.m_pic:
            self.draw_picture(s, ox, oy, mw, mh, rows, marks, (x0, z0, step, pw, ph), plate)
        else:
            ps = self.m_params(self.m_zoom)[2]                     # 1: a sample is a column by half a row; 2: two columns by one row
            W_ = s.w
            ch, fg, bg, bd = s.ch, s.fg, s.bg, s.bd
            idx = [c // ps for c in range(mw)]
            for row in range(mh):
                ra, rb = rows[(2 * row) // ps], rows[(2 * row + 1) // ps]
                top, bot = [pal[ra[i]] for i in idx], [pal[rb[i]] for i in idx]
                base = (oy + row) * W_ + ox
                ch[base:base + mw] = ["▀" if a != b else " " for a, b in zip(top, bot)]    # a space on a colour is the cheapest cell
                fg[base:base + mw], bg[base:base + mw], bd[base:base + mw] = top, bot, [False] * mw
            for (i, j, col, row, c, f) in marks:
                k = row * W_ + col
                ch[k], fg[k], bd[k] = ("★" if f else "⌂"), (DEPTH_COL.get(f.depth, HUT_COL) if f else HUT_COL), True
                if self.m_sel == c:
                    bg[k], fg[k] = ACCENT, ON_ACCENT
                if f:
                    for q, c2 in enumerate(" %d " % f.depth):
                        if col + 1 + q < ox + mw:
                            kk = k + 1 + q
                            ch[kk], fg[kk], bd[kk], bg[kk] = c2, DEPTH_COL.get(f.depth, TEXT), True, plate
            i, j = (0 - x0) // step, (0 - z0) // step                        # the origin
            if 0 <= i < pw and 0 <= j < ph:
                col, row = self.m_sample_cell(i, j)
                k = row * W_ + col
                ch[k], fg[k], bd[k] = "+", ORIGIN_COL, True
        if self.m_seed is None:
            self.map_message(s, ox, oy, mw, mh, "Type a seed above (press e), or press Random.")
        elif r is None and not any(m for m in self.m_layers()):
            self.map_message(s, ox, oy, mw, mh, "Drawing the map...")
        elif r is not None and r["err"]:
            self.map_message(s, ox, oy, mw, mh, r["err"] if not W.find_tool("biome_map") else "The map engine failed: " + r["err"],
                             bad=True, help2="Open the Hunt tab and press Build tools" if not W.find_tool("biome_map") else "")
        elif self.m_busy:
            s.put(ox + mw - 12, oy, " drawing... ", TEXT, MAPBG, True)
        if r and r.get("far") and not r["err"]:
            msg = " zoom in to see where huts generate "
            s.put(ox + max(0, (mw - twidth(msg)) // 2), oy + mh - 1, msg, TEXT, MAPBG, True)

    # ---- the map as a picture (kitty graphics / Sixel) -------------------------------------------------
    def draw_picture(self, s, ox, oy, mw, mh, rows, marks, view, plate):
        """The map cells are left on the terminal's own background so the picture shows through; the depth labels are
        ordinary text on top. The picture itself is built (and sent) only when something it shows has changed."""
        size = self.m_pic[1]
        x0, z0, step, pw, ph = view
        s.fill(ox, oy, mw, mh, CLEAR)
        for (i, j, col, row, c, f) in marks:
            if f and col + 4 < ox + mw:
                s.put(col + 1, row, " %d " % f.depth, DEPTH_COL.get(f.depth, TEXT), plate, True)
        key = (id(self.m_res), view, size, THEME, self.m_hl, self.m_sel, self.m_seed)
        cell_h = size[1] / mh
        self.pic_req = (ox, oy, mw, mh, key, lambda: self.paint_picture(size, rows, marks, view, cell_h))

    def paint_picture(self, size, rows, marks, view, cell_h):
        pal = self.palette()
        Wp, Hp = size
        x0, z0, step, pw, ph = view
        P = Picture(Wp, Hp, MAPBG)
        tbl = bytes(P.code(pal[i])[0] for i in range(256))
        colmap = [min(pw - 1, x * pw // Wp) for x in range(Wp)]
        for j, row in enumerate(rows):                                   # every sample as a block of pixels
            idx = row.translate(tbl)
            P.rows(j * Hp // ph, (j + 1) * Hp // ph, bytes(map(idx.__getitem__, colmap)))
        px, py = Wp / pw, Hp / ph                                        # picture pixels per sample
        reg = 512 / step                                                 # the 32-chunk regions: one hut candidate each
        if reg * px >= 40 and reg * py >= 40:
            for bx in range(-(-x0 // 512) * 512, x0 + pw * step, 512):
                x = round((bx - x0) / step * px)
                P.tint(x, 0, x + 1, Hp, 0xffffff, 0.10)
            for bz in range(-(-z0 // 512) * 512, z0 + ph * step, 512):
                y = round((bz - z0) / step * py)
                P.tint(0, y, Wp, y + 1, 0xffffff, 0.10)
        ox_, oz_ = round((0 - x0) / step * px), round((0 - z0) / step * py)    # the origin
        if 0 <= ox_ < Wp and 0 <= oz_ < Hp:
            P.tint(ox_ - 7, oz_, ox_ + 8, oz_ + 1, ORIGIN_COL, 0.9)
            P.tint(ox_, oz_ - 7, ox_ + 1, oz_ + 8, ORIGIN_COL, 0.9)
        rr = max(3.0, cell_h * 0.26)
        for (i, j, col, row, c, f) in sorted(marks, key=lambda m: m[5] is not None):
            cx, cy = (i + 0.5) * px, (j + 0.5) * py
            half = max(2, round(rr * 0.8))                               # a flat square, outlined: reads on every biome
            x0_, y0_ = round(cx) - half, round(cy) - half
            P.tint(x0_ - 1, y0_ - 1, x0_ + 2 * half + 2, y0_ + 2 * half + 2, 0xffffff if f else 0x101010, 1.0)
            P.tint(x0_, y0_, x0_ + 2 * half + 1, y0_ + 2 * half + 1, DEPTH_COL.get(f.depth, HUT_COL) if f else HUT_COL, 1.0)
            if self.m_sel == c:
                P.ring(cx, cy, rr + 5.0, 2.2, ACCENT)
        return P

    # ---- sending the picture -------------------------------------------------------------------------
    def render(self, w, h):
        """One frame as the bytes to write: the cells, then the map picture if there is one."""
        s = Screen(w, h)
        self.pic_req = None
        self.draw(s)
        self.scr = s
        g = self.term.graphics
        if g == "sixel" and self.pic_on and (self.pic_req is None or self.pic_req[:4] != self.pic_sent[:4]):
            self.prev = None                   # a Sixel picture is part of the cells: a full repaint clears it
        prev = self.prev
        full = prev is None or (prev.w, prev.h) != (w, h)
        out = s.diff(prev, self.term.colors)
        self.prev = s
        if g == "kitty":
            out += self.send_picture(full)
        elif g == "sixel":
            out += self.send_sixel(full, prev)
        return out

    def send_picture(self, full):
        """After the cells (kitty): place the new picture if it changed, or take it away if the map isn't showing."""
        req, old = self.pic_req, self.pic_on
        if req is None:
            self.pic_on, self.pic_sent = None, None
            return b"\x1b_Ga=d,d=I,i=%d,q=2\x1b\\" % old if old else b""
        ix, iy, cw, ch, key, build = req
        if old and not full and self.pic_sent == (ix, iy, cw, ch, key):
            return b""
        new = PIC_IDS[1] if old == PIC_IDS[0] else PIC_IDS[0]                # two ids: the new one goes up before the old goes
        out = b"\x1b[%d;%dH" % (iy + 1, ix + 1) + build().kitty(new, cw, ch)
        if old:
            out += b"\x1b_Ga=d,d=I,i=%d,q=2\x1b\\" % old
        self.pic_on, self.pic_sent = new, (ix, iy, cw, ch, key)
        return out

    def send_sixel(self, full, prev):
        """Sixel: the picture is drawn into the cells, so after any change over the map it goes out again and the map's
        text is written back on top of it, each cell on the picture's own colour there."""
        req = self.pic_req
        if req is None:
            self.pic_on, self.pic_sent = None, None
            return b""
        ix, iy, cw, ch, key, build = req
        if self.pic_on and not full and self.pic_sent == (ix, iy, cw, ch, key) and not self.scr.changed(prev, ix, iy, cw, ch):
            return b""
        self.pic_on, self.pic_sent = True, (ix, iy, cw, ch, key)
        pic = build()
        cpx, cpy = pic.w / cw, pic.h / ch

        def under(x, y):                                                     # the commonest colour in the cell's pixels
            ps = [pic.px[min(pic.h - 1, int((y - iy + fy) * cpy)) * pic.w + min(pic.w - 1, int((x - ix + fx) * cpx))]
                  for fy in (0.25, 0.5, 0.75) for fx in (0.25, 0.5, 0.75)]
            return pic.pal[max(set(ps), key=ps.count)]
        return b"\x1b[%d;%dH" % (iy + 1, ix + 1) + pic.sixel() + self.scr.patch(ix, iy, cw, ch, self.term.colors, under)

    def map_message(self, s, ox, oy, mw, mh, text, bad=False, help2=""):
        lines = wrap(text, min(mw - 6, 60)) + ([help2] if help2 else [])
        y = oy + mh // 2 - len(lines) // 2
        for i, line in enumerate(lines):
            x = ox + (mw - twidth(line) - 2) // 2
            s.put(x, y + i, " " + line + " ", DANGER if bad else TEXT, CARD, True)

    def hover_info(self):
        """(block x, block z, biome name or None) under the pointer."""
        if self.mouse is None:
            return None
        b = self.m_block_at(*self.mouse)
        if b is None:
            return None
        bid = self.m_biome_at(*b)
        return b[0], b[1], (W.biome_name(bid) if bid is not None else None)

    def draw_map_status(self, s, x, y, w):
        """One line under the map: what is under the pointer (or the middle of the view), and the legend."""
        s.fill(x, y, w, 1, PAGE)
        hv = self.hover_info()
        if hv:
            txt, col = "x %s  z %s  chunk %d, %d  %s" % (fmt(hv[0]), fmt(hv[1]), hv[0] >> 4, hv[1] >> 4, hv[2] or "-"), TEXT
        else:
            txt, col = "middle x %s  z %s  ·  %s blocks per column" % (fmt(self.m_cx), fmt(self.m_cz), fmt(self.m_scale()[0])), MUTED
        s.put(x + 1, y, txt[:w - 2], col, PAGE)
        legend = (("■", SWAMP_COL), (" swamp   ", MUTED), ("⌂", HUT_COL), (" hut   ", MUTED), ("★", DEPTH_COL[-58]), (" known low hut", MUTED))
        lw = sum(twidth(t) for t, _ in legend)
        if x + 1 + twidth(txt) + 4 + lw <= x + w - 1:
            lx = x + w - 1 - lw
            for t, c in legend:
                lx = s.put(lx, y, t, c, PAGE, t in ("■", "⌂", "★"))

    def sel_find(self):
        if self.m_sel is None or self.m_seed is None:
            return None
        for f in self.known_here():
            if (f.x >> 4, f.z >> 4) == self.m_sel:
                return f
        return None

    @staticmethod
    def compass(dx, dz):
        return ("E", "SE", "S", "SW", "W", "NW", "N", "NE")[round(math.atan2(dz, dx) / (math.pi / 4)) % 8]

    def sel_tp(self):
        f = self.sel_find()
        cx, cz = self.m_sel
        return f.tp if f else "/tp %d 100 %d" % (cx * 16 + 8, cz * 16 + 8)

    def draw_map_panel(self, s, x, y, w, h):
        near = self.m_near_list()
        n = max(1, min(8, h - 8, len(near)))
        h = min(h, n + (6 if self.m_sel is not None else 5))     # the card is only as tall as what it shows
        self.card(s, x, y, w, h, "Huts near here")
        iw = w - 6
        yy = y + 1
        if not near:
            s.put(x + 3, yy, "looking..." if self.m_near is None else "none within 60,000 blocks", MUTED, CARD)
        for cx, cz, d, f in near[:n]:
            sel = self.m_sel == (cx, cz)
            over = self.mouse is not None and self.mouse[1] == yy and x < self.mouse[0] < x + w - 1
            bg = PICK if sel else (HOVER if over else CARD)
            s.fill(x + 1, yy, w - 2, 1, bg)
            if sel:
                s.put(x + 1, yy, "▌", ACCENT, bg)
            col = DEPTH_COL.get(f.depth, HUT_COL) if f else HUT_COL
            s.put(x + 3, yy, "★" if f else "⌂", col, bg, True)
            if f:
                s.put(x + 5, yy, str(f.depth), col, bg, True)
            s.put(x + 9, yy, fmt(d).rjust(8), TEXT, bg)
            s.put(x + 18, yy, self.compass(cx * 16 + 8 - self.m_cx, cz * 16 + 8 - self.m_cz), MUTED, bg)
            self.hits.append((x + 1, yy, w - 2, 1, (lambda c=(cx, cz): self.m_goto_hut(c))))
            yy += 1
        if not near:
            yy += 1
        s.hline(x + 2, yy, w - 4, LINE)
        yy += 1
        if self.m_sel is None:
            s.put(x + 3, yy, "click one, or press z", MUTED, CARD)
            return
        cx, cz = self.m_sel
        f = self.sel_find()
        tp = self.sel_tp()
        s.put(x + 3, yy, ("known %d" % f.depth) if f else "depth unknown", DEPTH_COL.get(f.depth, ACCENT) if f else MUTED, CARD, True)
        s.put(x + 3, yy + 1, tp[:iw], TEXT, CARD, True)
        bx = self.button(s, x + 3, yy + 2, "Zoom in", self.m_zoom_hut, True)
        self.button(s, bx, yy + 2, "Copy /tp", (lambda: self.copy(tp, "the /tp")))

    def draw_map_actions(self, s, x, y, w):
        """The buttons that don't fit on the toolbar of a narrow window."""
        s.fill(x, y, w, 1, PAGE)
        bx = x + 1
        bx = self.button(s, bx, y, "Zoom to hut", self.m_zoom_hut, True)
        if self.m_sel is not None:
            tp = self.sel_tp()
            bx = self.button(s, bx, y, "Copy /tp", (lambda: self.copy(tp, "the /tp")))
        bx = self.button(s, bx, y, "Random", self.m_random)
        self.button(s, bx, y, "Known", self.open_known)

    # ---- the finder (Hunt tab) ---------------------------------------------------------------------------------------------------
    FIXES = {"build": ("Build the tools", "b"), "gpu-build": ("Build the tools", "b"), "minecraft": ("Get Minecraft files", "g")}

    def hunt_settings(self):
        """What is picked on the screen, in the form hunt.make_run takes."""
        s = self.h_set
        return dict(level=s["level"], mode=s["mode"], seeds=s["seeds"], area=hunt.AREAS[s["area"]][0])

    def h_fields(self):
        return ["level", "mode", "seeds"] + (["area"] if self.h_set["level"] > -55 else [])

    def h_locked(self):
        return bool(self.h_info and self.h_info["running"])

    def h_adopt(self, sets):
        """A hunt is running (maybe from an earlier session): show its settings."""
        a = next((i for i, (v, _) in enumerate(hunt.AREAS) if v == sets["area"]), len(hunt.AREAS) - 1)
        self.h_set = dict(level=sets["level"], mode=sets["mode"], seeds=sets["seeds"], area=a)

    def hunt_poll(self):
        """Refresh what the Hunt tab shows. On a thread: the checks call nvidia-smi and look at the process list."""
        gen = self.h_gen
        try:
            cur = hunt.running()
            sets = cur.settings() if cur else self.hunt_settings()
            run = cur or hunt.make_run(sets["level"], sets["mode"], sets["seeds"], sets["area"])
            if time.monotonic() >= self.h_mach_t:
                self.h_machine, self.h_mach_t = hunt.machine(), time.monotonic() + 30
            info = dict(run=run, running=bool(cur), settings=sets, status=run.status(), machine=self.h_machine,
                        pre=None if cur else run.preflight())
            res = hunt.all_results()
        except Exception as e:                                   # noqa: BLE001  (shown to the person, not swallowed)
            info, res = dict(run=None, running=False, settings=self.hunt_settings(), status=None, machine=self.h_machine,
                             pre=[(False, "couldn't check this machine: %s" % e, None)]), []
        if info["running"] and info["settings"] != self.hunt_settings():
            self.h_adopt(info["settings"])
        if gen == self.h_gen or info["running"]:
            self.h_info = info
        known = {f.seed for f in self.base_finds}
        have = {(f.seed, f.x, f.z) for f in self.base_finds}
        mine = [(W.Find(-r_["depth"], r_["seed"], r_["x"], r_["y"], r_["z"], "found on this machine"), r_["seed"] in known) for r_ in res]
        self.h_rows = mine
        self.finds = self.base_finds + [f for f, _ in mine if (f.seed, f.x, f.z) not in have]
        self.h_busy, self.dirty = False, True

    def hunt_tick(self, force=False):
        """Poll now and then: every second while a hunt runs, every 3 on the Hunt tab, otherwise not at all."""
        if self.h_busy:
            return
        running = bool(self.h_info and self.h_info["running"])
        if not force and ((self.tab != "hunt" and not running) or time.monotonic() < self.h_next):
            return
        self.h_busy, self.h_next = True, time.monotonic() + (1.0 if running else 3.0)
        if self.sync:
            self.hunt_poll()
        else:
            threading.Thread(target=self.hunt_poll, daemon=True).start()

    def h_changed(self):
        self.remember()
        self.h_gen += 1
        self.h_next, self.dirty = 0.0, True

    def h_focus_field(self, field):
        self.h_focus, self.h_edit, self.dirty = field, False, True

    def h_move_focus(self, d):
        fields = self.h_fields()
        i = fields.index(self.h_focus) if self.h_focus in fields else 0
        self.h_focus, self.dirty = fields[(i + d) % len(fields)], True

    def h_step(self, field, d):
        """Move one setting a step: d = -1 toward the easy end (-50, 5,000 blocks), +1 toward the hard end."""
        self.h_focus, self.h_edit, self.dirty = field, False, True
        if self.h_locked():
            self.say("Stop the hunt to change this", 3)
            return
        s = self.h_set
        if field == "level":
            lv = max(-59, min(-50, s["level"] - d))
            if lv == s["level"]:
                return
            s["level"] = lv
            if lv <= -55 and not hunt.MODES[s["mode"]]["gpu"]:
                s["mode"] = "gpu"
                self.say("-55 and deeper needs an NVIDIA card, so the mode is now GPU full", 5)
        elif field == "mode":
            keys = list(hunt.MODES)
            s["mode"] = keys[(keys.index(s["mode"]) + d) % len(keys)]
            if s["level"] <= -55 and not hunt.MODES[s["mode"]]["gpu"]:
                s["level"] = -54
                self.say("The CPU modes cover -50 to -54, so the floor is now -54", 5)
        elif field == "area":
            s["area"] = max(0, min(len(hunt.AREAS) - 1, s["area"] + d))
        self.h_changed()

    def h_edit_seeds(self):
        if self.h_locked():
            self.say("Stop the hunt to change this", 3)
            return
        self.h_before = self.h_set["seeds"]
        self.h_focus, self.h_edit, self.dirty = "seeds", True, True

    def h_end_edit(self, keep):
        if not keep:
            self.h_set["seeds"] = self.h_before
        self.h_edit, self.dirty = False, True
        if keep:
            self.h_changed()

    def hunt_start(self):
        sets = self.hunt_settings()
        try:
            hunt.make_run(sets["level"], sets["mode"], sets["seeds"], sets["area"]).start_detached()
        except hunt.HuntError as e:
            self.say(str(e), 7)
            return
        self.h_changed()
        self.say("Hunt started. It keeps running if you close this window.", 5)

    def hunt_stop(self):
        run = self.h_info and self.h_info["run"]
        if run is None or not self.h_locked():
            return
        run.stop()
        self.h_next, self.dirty = 0.0, True
        self.say("Stopping...", 3)

    def hunt_new_spot(self):
        if self.h_locked():
            self.say("Stop the hunt first", 3)
        elif self.h_set["level"] > -55:
            self.say("New spot is for the -55 and deeper search; the other one makes up new seeds by itself", 5)
        else:
            sets = self.hunt_settings()
            hunt.make_run(sets["level"], sets["mode"], "", sets["area"]).new_spot()
            self.h_set["seeds"] = ""
            self.say("The next start picks another random place", 4)

    def h_sel_find(self):
        return self.h_rows[self.h_sel][0] if 0 <= self.h_sel < len(self.h_rows) else None

    def h_frac(self, live):
        """How far through the current chunk (or seed) it is: a measured step for the scanner, elapsed over expected otherwise."""
        if live.get("phase") == "scanning" and live.get("unit_frac") is not None:
            return live["unit_frac"]
        t0, exp = live.get("unit_t0"), live.get("unit_expect")
        if t0 and exp:
            return max(0.0, min(0.97, (time.time() - t0) / exp))
        return None

    @staticmethod
    def wait_text(sec):
        m = sec / 60.0
        return "%d seconds" % sec if m < 1.5 else "%.0f minutes" % m if m < 90 else "%.1f hours" % (m / 60) if m < 48 * 60 else "%.0f days" % (m / 1440)

    def seed_eta(self, st, area_blocks, mode):
        """About how long one seed takes: what was measured, else a guess from the CPU's measured speed (4.2 million regions
        a second a thread)."""
        if st and st.get("sec_per_seed"):
            return st["sec_per_seed"]
        if hunt.MODES[mode]["gpu"]:
            return None
        r = 58594 * 2 if area_blocks <= 0 else 2 * (area_blocks // 512 + 1)
        threads = max(1, int((os.cpu_count() or 2) * hunt.MODES[mode]["share"]))
        return r * r / (4.2e6 * threads)

    def stepper(self, s, x, y, text, width, field, locked):
        focus = self.h_focus == field and not locked
        total = width + 6
        over = self.mouse is not None and self.mouse[1] == y and x <= self.mouse[0] < x + total and not locked
        bg = ACCENT if focus else (HOVER if over else CARD2)
        s.put(x, y, " ‹ " + text.center(width)[:width] + " › ", ON_ACCENT if focus else (MUTED if locked else TEXT), bg, True)
        self.hits.append((x, y, 3, 1, (lambda: self.h_step(field, -1))))
        self.hits.append((x + total - 3, y, 3, 1, (lambda: self.h_step(field, 1))))
        self.hits.append((x + 3, y, width, 1, (lambda: self.h_focus_field(field))))
        return x + total

    def seeds_box(self, s, x, y, width, locked, placeholder):
        text, editing = self.h_set["seeds"], self.h_edit
        focus = self.h_focus == "seeds" and not locked
        bg = PICK if editing else (HOVER if focus else CARD2)
        shown = (text[-(width - 3):] + "▏") if editing else text[:width - 2]
        s.put(x, y, " " + (shown if (text or editing) else placeholder)[:width - 1].ljust(width - 1), TEXT if (text or editing) else MUTED, bg, focus or editing)
        self.hits.append((x, y, width, 1, self.h_edit_seeds))
        return x + width

    def draw_hunt(self, s, top, bottom):
        self.hunt_tick()
        w = s.w
        info = self.h_info
        match = bool(info and info["settings"] == self.hunt_settings() and info["status"] is not None)
        c = types.SimpleNamespace(
            info=info, st=info["status"] if match else None, match=match, S=self.h_set,
            running=bool(info and info["running"]), mach=(info or {}).get("machine") or {})
        c.live = (c.st or {}).get("live") or {}
        c.solver = c.S["level"] <= -55
        c.fails = [(t, fix) for ok, t, fix in (info["pre"] or []) if not ok] if (match and info["pre"] is not None and not c.running) else []
        wide = w >= 132                                          # room to put Search and Progress side by side
        cw = min(w - 2, 150 if wide else 108)
        x0 = (w - cw) // 2
        foot = bottom - 1                                        # the machine line sits under the cards
        sw = 66 if wide else cw
        c.inline = not wide and cw >= 85                         # room for a line of explanation beside each setting?
        n = 3 if c.solver else 4
        c.fail_lines = []
        for text, _ in c.fails[:3]:
            c.fail_lines += wrap(text, sw - 6)[:2]
        c.fail_lines = c.fail_lines[:4]
        c.cpu_btn = bool(c.fails and hunt.MODES[c.S["mode"]]["gpu"] and c.mach and not c.mach.get("gpu"))
        sh = n + 5 + len(c.fail_lines) + (1 if c.cpu_btn else 0)
        compact = not wide and foot - top < sh + 6 + 5          # a short window: no Progress card, one status row in Search
        c.compact = compact
        if compact and c.running:
            sh += 1
        sh = min(sh, foot - top)
        self.h_search(s, x0, top, sw, sh, c)
        y = top + sh
        if wide:
            self.h_progress(s, x0 + sw + 1, top, cw - sw - 1, sh, c)
        elif not compact:
            ph = 3 + min(5, len(self.progress_rows(c, cw - 6)))
            if foot - y >= ph + 5:
                self.h_progress(s, x0, y, cw, ph, c)
                y += ph
        if self.h_log and foot - y >= 9:
            oh = min(10, len(self.h_log) + 3, foot - y - 5)
            self.h_output(s, x0, y, cw, oh)
            y += oh
        if foot - y >= 4:
            self.h_found(s, x0, y, cw, foot - y)
        self.h_footer(s, x0, foot, cw, info)

    def h_search(self, s, x, y, w, h, c):
        self.card(s, x, y, w, h, "Search")
        ix = x + 3
        end = y + h - 2                                          # the last row inside the card
        S, running, mach = c.S, c.running, c.mach
        hx = ix + 36
        said = {}

        def hint(field, row_y, text, col=MUTED):
            if c.inline:
                s.put(hx, row_y, text if twidth(text) <= x + w - 2 - hx else text[:x + w - 3 - hx] + "…", col, CARD)
            else:
                said[field] = (text, col)                           # narrow window: say it under the form, for the picked setting

        fl = lambda f: self.h_focus == f and not running           # noqa: E731
        yy = y + 2
        s.put(ix, yy, "Floor y", ACCENT if fl("level") else MUTED, CARD, fl("level"))
        bx = self.stepper(s, ix + 11, yy, "%d" % S["level"], 4, "level", running)
        s.put(bx + 1, yy, "or lower", MUTED, CARD)
        hint("level", yy, "needs an NVIDIA GPU" if c.solver else "CPU or GPU")
        yy += 1
        s.put(ix, yy, "Mode", ACCENT if fl("mode") else MUTED, CARD, fl("mode"))
        self.stepper(s, ix + 11, yy, hunt.MODES[S["mode"]]["label"], 10, "mode", running)
        if hunt.MODES[S["mode"]]["gpu"] and mach and not mach.get("gpu"):
            hint("mode", yy, "no NVIDIA card found", WARN)
        else:
            hint("mode", yy, hunt.MODES[S["mode"]]["blurb"])
        yy += 1
        s.put(ix, yy, "Start at" if c.solver else "Seeds", ACCENT if fl("seeds") else MUTED, CARD, fl("seeds"))
        bx = self.seeds_box(s, ix + 11, yy, 24, running, "a random place" if c.solver else "made up for you")
        if c.solver and not running:
            bx = self.button(s, bx + 1, yy, "New spot", self.hunt_new_spot)
            if c.inline and x + w - 2 - bx > 24:
                s.put(bx + 1, yy, "or type a number to start there"[:x + w - 3 - bx - 1], MUTED, CARD)
            else:
                said["seeds"] = ("or type a number to always start at the same place", MUTED)
        else:
            hint("seeds", yy, "type a number to start there" if c.solver else "or type seeds, or a file of them")
        yy += 1
        if not c.solver:
            s.put(ix, yy, "Area", ACCENT if fl("area") else MUTED, CARD, fl("area"))
            self.stepper(s, ix + 11, yy, hunt.AREAS[S["area"]][1], 15, "area", running)
            per = self.seed_eta(c.st, hunt.AREAS[S["area"]][0], S["mode"])
            hint("area", yy, ("around 0,0 · about %s a seed here" % self.wait_text(per)) if per else "around 0,0")
            yy += 1
        if not c.inline and said.get(self.h_focus):
            s.put(ix, yy, said[self.h_focus][0][:w - 6], said[self.h_focus][1], CARD)
        yy += 1
        # -- the one big button, and what to say beside it
        st, live, outcome = c.st, c.live, (c.st or {}).get("outcome")
        if yy > end:
            return
        if running:
            bx = self.button(s, ix, yy, "Stop  (x)", self.hunt_stop, True)
            if st and st["stopping"] or live.get("phase") == "stopping":
                s.put(bx + 1, yy, "Stopping...", WARN, CARD, True)
            elif live.get("phase") == "cooling":
                s.put(bx + 1, yy, ("● Paused: " + live.get("detail", "the GPU is hot"))[:x + w - 3 - bx - 1], WARN, CARD, True)
            elif live.get("phase") == "tuning":
                s.put(bx + 1, yy, "● Timing this GPU once, about 30 seconds"[:x + w - 3 - bx - 1], OKC, CARD, True)
            else:
                s.put(bx + 1, yy, "● Hunting", OKC, CARD, True)
        elif self.h_step_name:
            s.put(ix, yy, "Working: %s ..." % self.h_step_name, WARN, CARD, True)
        elif c.info is None or not c.match:
            s.put(ix, yy, "Checking this machine...", MUTED, CARD)
        elif c.fails:
            fix = next((f for _, f in c.fails if f in self.FIXES), None)
            if fix:
                label, key = self.FIXES[fix]
                self.button(s, ix, yy, "%s  (%s)" % (label, key), self.build if key == "b" else self.get_mc, True)
            else:
                self.button(s, ix, yy, "Start hunting  (s)", (lambda: self.say(c.fails[0][0], 7)))
        else:
            bx = self.button(s, ix, yy, "Start hunting  (s)", self.hunt_start, True)
            msg, col = None, MUTED
            if st and st.get("failed"):
                msg, col = "Stopped: " + st["failed"], DANGER
            elif outcome == "found":
                msg, col = "★ Found a hut! Start looks for more.", SWAMPC
            elif outcome == "stopped" and st["units_done"]:
                msg = "Stopped. Start carries on."
            elif outcome == "finished":
                msg = "Finished."
            elif mach.get("gpu") and hunt.MODES[S["mode"]]["gpu"]:
                msg = "Ready: %s" % mach["gpu"]["name"].replace("NVIDIA ", "").replace("GeForce ", "")
            else:
                msg = "Ready"
            s.put(bx + 1, yy, msg[:max(0, x + w - 3 - bx - 1)], col, CARD, col in (SWAMPC, DANGER))
        yy += 1
        for line in c.fail_lines:
            if yy > end:
                return
            s.put(ix, yy, line, DANGER, CARD)
            yy += 1
        if c.cpu_btn and yy <= end:
            self.button(s, ix, yy, "Use CPU full  (covers -50 to -54)", self.h_use_cpu)
            yy += 1
        if c.compact and running and yy <= end:
            rows = self.progress_rows(c, w - 6)
            if rows:
                self.draw_progress_row(s, ix, yy, w - 6, rows[0])

    def progress_rows(self, c, iw):
        """What the Progress card says, as rows: ("text", string, colour) or ("bar", label, fraction)."""
        st, live, S = c.st, c.live, c.S
        rows = []
        if c.running and not st:
            return [("text", "Starting...", MUTED)]
        if st and c.running:
            parts = []
            if st.get("speed_text"):
                parts.append(st["speed_text"])
            g = live.get("gpu")
            if g and hunt.MODES[S["mode"]]["gpu"] and g.get("temp") is not None:
                parts.append("GPU %d °C%s" % (g["temp"], (" · %d%%" % g["util"]) if g.get("util") is not None else ""))
            if live.get("detail") and live.get("phase") not in ("cooling", "tuning", "stopping"):
                parts.append(live["detail"])
            rows.append(("text", "   ·   ".join(parts), TEXT))
            f = self.h_frac(live)
            if f is not None:
                rows.append(("bar", "%s %s" % (st["unit"], fmt(st["units_done"] + 1)), f))
        elif st and st["units_done"]:
            u = st["unit"]
            rows.append(("text", "%s %s%s done so far" % (fmt(st["units_done"]), u, "" if st["units_done"] == 1 else "s"), MUTED))
        else:
            rows.append(("text", "Not running", MUTED))
        if st and (c.running or st["units_done"]):
            if any(n for _, n in st["funnel"]):
                rows.append(("text", "  →  ".join("%s %s" % (lab, hunt.short_count(n)) for lab, n in st["funnel"]), MUTED))
            if c.running:
                if st.get("eta_s"):
                    rows.append(("text", "about one find every %s (an average)" % self.wait_text(st["eta_s"]), MUTED))
                elif st["kind"] == "solver" and not st.get("eta_known") and st.get("rate"):
                    rows.append(("text", "time per find shows once it has found some", MUTED))
                for line in (st.get("problems") or [])[-1:]:
                    rows.append(("text", line, WARN))
        return rows

    def draw_progress_row(self, s, ix, yy, iw, row):
        if row[0] == "text":
            s.put(ix, yy, row[1][:iw], row[2], CARD)
        else:
            bw = max(10, min(40, iw - 24))
            n = int(round(bw * row[2]))
            s.put(ix, yy, row[1].ljust(12), MUTED, CARD)
            s.put(ix + 12, yy, "█" * n + "░" * (bw - n), ACCENT, CARD)
            s.put(ix + 13 + bw, yy, "%3d%%" % round(row[2] * 100), TEXT, CARD, True)

    def h_progress(self, s, x, y, w, h, c):
        self.card(s, x, y, w, h, "Progress")
        for k, row in enumerate(self.progress_rows(c, w - 6)[:max(0, h - 3)]):
            self.draw_progress_row(s, x + 3, y + 2 + k, w - 6, row)

    def h_output(self, s, x, y, w, h):
        """A build, a download or a test: its output."""
        self.card(s, x, y, w, h, "Output" + (" (working...)" if self.h_step_name else ""))
        if not self.h_step_name:
            self.button(s, x + w - 10, y, "Hide", self.h_hide_log)
        for k, line in enumerate(self.h_log[-(h - 2):]):
            s.put(x + 3, y + 1 + k, line[:w - 6], DANGER if line.startswith(("FAIL", "ERROR", "FAILED")) else OKC if line.startswith("ok") else TEXT, CARD)

    def h_found(self, s, x, y, w, h):
        rows = self.h_rows
        self.card(s, x, y, w, h, "Found on this machine" + (" (%d)" % len(rows) if rows else ""))
        ix, iw = x + 3, w - 6
        yy = y + (2 if h >= 7 else 1)
        end = y + h - 2
        if not rows:
            s.put(ix, yy, "nothing yet: press Start", MUTED, CARD)
            return
        fit = max(1, end - yy + 1)
        if len(rows) > fit:
            fit -= 1                                             # the last row says how many more
        self.h_top = max(0, min(self.h_top, max(0, len(rows) - fit)))
        for k in range(self.h_top, min(len(rows), self.h_top + fit)):
            f, known = rows[k]
            on = k == self.h_sel
            over = self.mouse is not None and self.mouse[1] == yy and ix <= self.mouse[0] < ix + iw
            bg = PICK if on else (HOVER if over else CARD)
            s.fill(ix - 1, yy, iw + 2, 1, bg)
            s.put(ix, yy, str(f.depth), DEPTH_COL.get(f.depth, TEXT), bg, True)
            s.put(ix + 5, yy, f.tp, TEXT, bg)
            tx = ix + 5 + twidth(f.tp) + 2
            if tx + 28 < ix + iw - 22:
                s.put(tx, yy, ("seed " + f.seed)[:24], MUTED, bg)
                tx += 26
            if tx + 16 < ix + iw - 22:
                s.put(tx, yy, "already known" if known else "new", MUTED if known else SWAMPC, bg, not known)
            bx = ix + iw - 20
            self.button(s, bx, yy, "Copy", (lambda f=f: self.copy(f.tp, "the /tp")))
            self.button(s, bx + 7, yy, "Show on map", (lambda f=f: self.show_on_map(f)))
            self.hits.insert(0, (ix - 1, yy, iw - 20, 1, (lambda k=k: self.h_pick(k))))
            yy += 1
        if len(rows) > self.h_top + fit:
            s.put(ix, yy, "... %d more (scroll)" % (len(rows) - self.h_top - fit), MUTED, CARD)

    def h_footer(self, s, x, y, w, info):
        """What the machine has, on one line, with the checks at the right."""
        if not (info and info.get("pre") is not None):
            return
        b1, b2 = "Test  (f)", "Recheck  (r)"
        bw = twidth(b1) + twidth(b2) + 5
        room = w - 4 - bw
        line = "✓ "
        first = True
        for ok, t, _ in info["pre"]:
            if ok:
                add = ("" if first else "  ·  ") + t
                if twidth(line) + twidth(add) > room:
                    break
                line += add
                first = False
        if room >= 20:
            s.put(x + 1, y, line, MUTED, PAGE)
        bx = self.button(s, x + w - bw, y, b1, self.test_finder)
        self.button(s, bx, y, b2, self.hunt_recheck)

    def h_pick(self, k):
        self.h_sel, self.dirty = k, True

    def h_use_cpu(self):
        self.h_set.update(mode="cpu", level=-54)
        self.h_changed()

    def h_hide_log(self):
        self.h_log, self.dirty = [], True

    def hunt_recheck(self):
        self.h_machine, self.h_mach_t = None, 0.0
        self.h_changed()
        self.say("Checked again")

    # ---- build, download and test steps (they run on a thread; their output shows on the Hunt tab) ------------------------------------
    def log(self, text):
        for line in str(text).splitlines():
            if line.strip():
                self.h_log.append(line.rstrip())
        del self.h_log[:-300]
        self.dirty = True

    def run_step(self, name, work):
        """Run work(log) on a thread, one step at a time."""
        if self.h_step_name:
            self.say("Wait for the current step to finish (%s)" % self.h_step_name)
            return
        self.h_step_name, self.h_log, self.dirty = name, [], True

        def go():
            try:
                work(self.log)
            except (hunt.HuntError, RuntimeError, OSError, subprocess.SubprocessError) as e:
                self.log("FAILED: %s" % e)
                self.say("%s failed: %s" % (name, e), 8)
            finally:
                self.h_step_name = ""
                self.m_res = None
                self.h_machine, self.h_mach_t = None, 0.0
                self.h_changed()
        if self.sync:
            go()
        else:
            threading.Thread(target=go, daemon=True).start()

    def build(self):
        def work(log):
            cmd = ["bash", os.path.join(ROOT, "build.sh")] if os.name != "nt" else \
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(ROOT, "build.ps1")]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", cwd=ROOT)
            for line in p.stdout:
                log(line)
            if p.wait() != 0:
                raise RuntimeError("the build stopped with an error (see the output above)")
            self.say("Built the tools", 5)
        self.run_step("building the tools", work)

    def get_mc(self):
        def work(log):
            mcfiles.fetch(log)
            self.say("Minecraft files ready", 5)
        self.run_step("downloading Minecraft's files", work)

    def test_finder(self):
        lv = self.h_set["level"]

        def work(log):
            ok = hunt.selftest_scan(log) if lv > -55 else hunt.selftest(cpu_only=False, say=log)
            self.say("The finder works: it reproduced the reference hut" if ok else "The finder test found a problem (see the output)", 8)
        self.run_step("testing the finder", work)

    # ---- input --------------------------------------------------------------------------------------------------------------
    def on_event(self, ev):
        kind = ev[0]
        if self.help and kind in ("key", "char", "mouse") and not (kind == "mouse" and ev[1] in ("move", "release")):
            self.help, self.dirty = False, True
            return
        if self.known_open and kind in ("key", "char"):
            self.on_known(ev)
            return
        if kind == "paste" and self.tab == "map":
            self.m_text += ev[1].strip().replace("\n", "")
            self.m_edit, self.dirty = True, True
        elif kind == "paste" and self.tab == "hunt" and not self.h_locked():
            if not self.h_edit:
                self.h_edit_seeds()
            self.h_set["seeds"] += ev[1].strip().replace("\n", " ")
            self.dirty = True
        elif kind == "char":
            self.on_char(ev[1])
        elif kind == "key":
            self.on_key(ev[1])
        elif kind == "mouse":
            self.on_mouse(*ev[1:])

    def on_char(self, c):
        if self.tab == "map" and self.m_edit:
            self.m_text += c
            self.dirty = True
            return
        if self.tab == "hunt" and self.h_edit:
            self.h_set["seeds"] += c
            self.dirty = True
            return
        c = c.lower() if len(c) == 1 else c
        if c in ("q",):
            raise SystemExit
        if c == "t":
            apply_theme("dark" if THEME == "light" else "light")
            self.dirty = True
            self.remember()
        elif c == "?":
            self.help, self.dirty = True, True
        elif c in [k for _, _, k in TABS]:
            self.set_tab(next(t for t, _, k in TABS if k == c))
        elif self.tab == "map":
            if c in ("e", "/"):
                self.m_focus()
            elif c == "k":
                self.open_known()
            elif c == "z":
                self.m_zoom_hut()
            elif c == "r":
                self.m_random()
            elif c in ("+", "="):
                self.m_zoom_to(self.m_zoom - 1)
            elif c in ("-", "_"):
                self.m_zoom_to(self.m_zoom + 1)
            elif c == "[":
                self.m_next_ver(1)
            elif c == "]":
                self.m_next_ver(-1)
            elif c == "h":
                self.m_toggle_hl()
            elif c == "o":
                self.m_cx, self.m_cz, self.dirty = 0, 0, True
            elif c == "c" and self.m_sel is not None:
                f = self.sel_find()
                cx, cz = self.m_sel
                self.copy(f.tp if f else "/tp %d 100 %d" % (cx * 16 + 8, cz * 16 + 8), "the /tp")
        elif self.tab == "hunt":
            f = self.h_sel_find()
            if c == "s":
                if self.h_locked():
                    self.say("Already hunting")
                elif self.h_info and self.h_info["pre"] is not None and any(not ok for ok, _, _ in self.h_info["pre"]):
                    self.say(next(t for ok, t, _ in self.h_info["pre"] if not ok), 7)
                else:
                    self.hunt_start()
            elif c == "x":
                self.hunt_stop()
            elif c == "n":
                self.hunt_new_spot()
            elif c == "e":
                self.h_edit_seeds()
            elif c == "c" and f:
                self.copy(f.tp, "the /tp")
            elif c == "m" and f:
                self.show_on_map(f)
            elif c == "r":
                self.hunt_recheck()
            elif c == "b":
                self.build()
            elif c == "g":
                self.get_mc()
            elif c == "f":
                self.test_finder()

    def on_key(self, k):
        if k == "ctrl+c":
            raise SystemExit
        if self.tab == "hunt" and self.h_edit:
            if k == "enter":
                self.h_end_edit(True)
            elif k == "esc":
                self.h_end_edit(False)
            elif k == "backspace":
                self.h_set["seeds"], self.dirty = self.h_set["seeds"][:-1], True
            return
        if self.tab == "map" and self.m_edit:
            if k == "enter":
                self.m_apply_seed()
            elif k == "esc":
                self.m_edit, self.m_text, self.dirty = False, (str(self.m_seed) if self.m_seed is not None else ""), True
            elif k == "backspace":
                self.m_text, self.dirty = self.m_text[:-1], True
            return
        if k == "esc":
            self.help, self.dirty = False, True
        elif k == "tab":
            ids = [t for t, _, _ in TABS]
            self.set_tab(ids[(ids.index(self.tab) + 1) % len(ids)])
        elif k == "btab":
            ids = [t for t, _, _ in TABS]
            self.set_tab(ids[(ids.index(self.tab) - 1) % len(ids)])
        elif self.tab == "hunt":
            if k in ("up", "down"):
                self.h_move_focus(-1 if k == "up" else 1)
            elif k in ("left", "right"):
                if self.h_focus == "seeds":
                    self.h_edit_seeds()
                else:
                    self.h_step(self.h_focus, -1 if k == "left" else 1)
            elif k == "enter":
                if self.h_focus == "seeds":
                    self.h_edit_seeds()
                else:
                    self.on_char("s")
            elif k in ("pgup", "pgdn"):
                self.h_top = max(0, self.h_top + (-3 if k == "pgup" else 3))
                self.dirty = True
        elif self.tab == "map":
            _, _, mw, mh = self.m_rect
            hx, hy = max(1, mw // 8), max(1, mh // 4)
            if k == "left":
                self.m_pan(-hx, 0)
            elif k == "right":
                self.m_pan(hx, 0)
            elif k == "up":
                self.m_pan(0, -hy)
            elif k == "down":
                self.m_pan(0, hy)
            elif k == "pgup":
                self.m_zoom_to(self.m_zoom - 1)
            elif k == "pgdn":
                self.m_zoom_to(self.m_zoom + 1)
            elif k == "enter":
                self.m_focus()

    def on_mouse(self, kind, x, y, btn):
        old = self.mouse
        self.mouse = (x, y)
        if kind == "move":
            if old != self.mouse:
                self.dirty = True
            return
        if kind == "wheelup" or kind == "wheeldown":
            d = -1 if kind == "wheelup" else 1
            if self.known_open:
                self.f_move(d * 3)
            elif self.tab == "map":
                self.m_zoom_to(self.m_zoom + d, x, y)
            elif self.tab == "hunt":
                self.h_top, self.dirty = max(0, self.h_top + d), True
            return
        ox, oy, mw, mh = self.m_rect
        if kind == "press" and btn == 0:
            for hx, hy, hw, hh, act in reversed(self.hits):
                if hx <= x < hx + hw and hy <= y < hy + hh:
                    self.m_edit = self.m_edit and act == self.m_focus
                    act()
                    self.dirty = True
                    return
            if self.tab == "map" and ox <= x < ox + mw and oy <= y < oy + mh:
                self.m_edit = False
                self.m_drag = (x, y, self.m_cx, self.m_cz, False)
        elif kind == "drag" and self.m_drag and self.tab == "map":
            ax, ay, cx, cz, moved = self.m_drag
            bc, br = self.m_scale()
            if moved or abs(x - ax) + abs(y - ay) >= 1:
                self.m_cx = cx - (x - ax) * bc
                self.m_cz = cz - (y - ay) * br
                self.m_drag = (ax, ay, cx, cz, True)
                self.dirty = True
        elif kind == "release" and self.m_drag:
            sx, sy, cx, cz, moved = self.m_drag
            self.m_drag = None
            if not moved:
                self.m_click(x, y)
            self.dirty = True

    def m_click(self, x, y):
        best, bd = None, 99
        for col, row, c, f in self.m_marks:
            d = max(abs(col - x), abs(row - y))
            if d <= 1 and d < bd:
                best, bd = c, d
        self.m_sel = best
        self.dirty = True

    def feed(self, data):
        events, self.pending = parse_input(self.pending + data)
        for ev in events:
            self.on_event(ev)

    def flush_pending(self):
        """A lone Esc can't be told from the start of an arrow key until nothing follows it: then it was the Esc key."""
        if self.pending == "\x1b":
            self.pending = ""
            self.on_event(("key", "esc"))

    # ---- the loop ----------------------------------------------------------------------------------------------------------
    def apply_prefs(self):
        """Restore the last session. The very first launch gets a random seed, already at its nearest hut."""
        p = W.load_prefs()
        try:
            self.apply_hunt_prefs(p)
        except (ValueError, TypeError, KeyError, hunt.HuntError):
            pass
        if p.get("theme") in THEMES:
            apply_theme(p["theme"])
        v = p.get("version")
        if isinstance(v, str) and any(x[0] == v for x in W.VERSIONS):
            self.m_ver = next(i for i, x in enumerate(W.VERSIONS) if x[0] == v)
        seed, note = W.parse_seed(str(p.get("seed", "")))
        if seed is None:
            self.m_random()
            return
        self.m_text, self.m_seed = str(p["seed"]), seed
        self.m_cx, self.m_cz = int(p.get("cx", 0)), int(p.get("cz", 0))
        self.m_zoom = max(0, min(MAX_ZOOM, int(p.get("zoom", 2))))

    def apply_hunt_prefs(self, p):
        """The Hunt tab's last choices; on the very first launch, the best ones for this computer (a GPU search with a card)."""
        h = p.get("hunt") if isinstance(p.get("hunt"), dict) else None
        if h is None:
            d = hunt.default_settings()
            self.h_set.update(level=d["level"], mode=d["mode"])
            return
        lv, md, ar = h.get("level"), h.get("mode"), h.get("area")
        if lv in hunt.LEVELS and md in hunt.MODES and not hunt.combo_problem(lv, md):
            self.h_set.update(level=lv, mode=md)
        if isinstance(h.get("seeds"), str):
            self.h_set["seeds"] = h["seeds"][:2000]
        if isinstance(ar, int) and 0 <= ar < len(hunt.AREAS):
            self.h_set["area"] = ar

    def remember(self):
        """Save the choices as they are made, so closing the window or a crash never loses them (not in the checks)."""
        if not self.sync:
            self.save_prefs()

    def save_prefs(self):
        W.save_prefs(dict(theme=THEME, seed=self.m_text, version=self.ver()[0], cx=self.m_cx, cz=self.m_cz, zoom=self.m_zoom,
                          hunt=dict(self.h_set)))

    def run(self):
        t = self.term
        try:
            self.apply_prefs()
        except (ValueError, TypeError, KeyError):
            pass                                                   # a damaged settings file is not worth stopping for
        for name in ("SIGHUP", "SIGTERM"):                           # the window closing is a quit too
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), lambda *_: sys.exit(0))
        t.start()
        self.hunt_tick(force=True)                                 # is a hunt already going? (it outlives this window)
        size = None
        try:
            while True:
                w, h = t.size()
                if (w, h) != size:
                    size, self.prev, self.dirty = (w, h), None, True
                data = t.read(0.02)
                if data:
                    self.feed(data)
                elif self.pending:
                    self.flush_pending()
                self.hunt_tick()
                if self.toast[0] and time.monotonic() > self.toast[1]:
                    self.toast, self.dirty = ("", 0), True
                if self.dirty:
                    self.dirty = False
                    t.write(self.render(w, h))             # one write, so the terminal never shows half a frame
        except (SystemExit, KeyboardInterrupt):
            pass
        finally:
            t.stop()
            self.save_prefs()


# ---- no terminal: one frame as text, and the checks -----------------------------------------------------------------------
def frame(w, h, tab=0, theme="light", seed=None):
    apply_theme(theme)
    app = App(None, sync=True)
    app.tab = TABS[tab][0]
    if seed:
        app.m_text = seed
        app.m_apply_seed()
    s = Screen(w, h)
    app.draw(s)                      # the first draw asks for the map
    app.draw(s)                      # the second has it
    return app, s


def frame_text(s):
    return "\n".join("".join(c if c else "" for c in s.ch[y * s.w:(y + 1) * s.w]).rstrip() for y in range(s.h))


def selftest():
    bad = []

    def check(name, ok, detail=""):
        print(("ok    " if ok else "FAIL  ") + name + (("  " + detail) if detail else ""))
        if not ok:
            bad.append(name)

    finds = W.load_finds()
    located = [f for f in finds if f.located]
    check("finds load", len(finds) >= 50, "%d huts" % len(finds))
    check("46 huts at -58", sum(1 for f in finds if f.depth == -58) == 46)
    nearest = min(located, key=lambda f: f.dist)
    check("closest is the 7,303-block -58", nearest.tp == "/tp 5811 -56 4423" and nearest.seed == "795374283637013279", nearest.tp)
    check("numeric seed", W.parse_seed("795374283637013279")[0] == 795374283637013279)
    check("text seed = Java hashCode", W.parse_seed("hello")[0] == 99162322)
    check("seed wraps to 64 bits", W.parse_seed("9223372036854775808")[0] == -9223372036854775808)
    miss = [f for f in located if W.hut_chunk(int(f.seed), (f.x >> 4) // 32, (f.z >> 4) // 32) != (f.x >> 4, f.z >> 4)]
    check("hut placement hits every known hut's chunk", not miss, "%d of %d" % (len(located) - len(miss), len(located)))
    if W.find_tool("biome_map"):
        for code, name in ((215, "1.21.5-26.1"), (214, "1.21.4")):
            sw = sum(1 for f in located if W.biome_chunks(int(f.seed), [(f.x >> 4, f.z >> 4)], code)[0] == W.SWAMP)
            check("swamp at every known hut (%s data)" % name, sw == len(located), "%d of %d" % (sw, len(located)))
        g = W.biome_grid(795374283637013279, 5000, 4000, 40, 40, 16, "1.21.5")
        check("map grid size", len(g) == 1600)
        spots = W.hut_spots(795374283637013279, 5000, 4000, 7000, 6000, "1.21.4")
        check("hut spots include the -58 hut", spots is not None and (363, 276) in spots, str(spots))
        for sh in range(len(TABS)):
            app, s = frame(110, 32, sh, "dark")
            check("%s tab renders (110x32)" % TABS[sh][1], "Traceback" not in frame_text(s))
    else:
        check("map engine built", False, "run ./build.sh")
    print("\n%s" % ("all passed" if not bad else "%d FAILED: %s" % (len(bad), ", ".join(bad))))
    return 1 if bad else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):          # the Windows console defaults to cp1252, which can't print the frame
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if argv[:1] == ["--selftest"]:
        return selftest()
    if argv[:1] == ["--frame"]:
        w, h = int(argv[1]), int(argv[2])
        tab = int(argv[3]) - 1 if len(argv) > 3 else 0
        theme = argv[4] if len(argv) > 4 else "light"
        print(frame_text(frame(w, h, tab, theme)[1]))
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("This opens a full-screen app, so run it in a terminal. (python3 witchhut_studio.py --selftest checks the install.)")
        return 1
    try:
        term = Term()
    except Exception as e:                       # noqa: BLE001
        print("Can't take over this terminal: %s" % e)
        return 1
    App(term).run()
    r = hunt.running()                                      # a hunt is its own process: it carries on after this window closes
    if r:
        print("A hunt (%s, floor %d) is still running in the background. Open this app again to watch it, or stop it from the Hunt tab." %
              (hunt.MODES[r.mode]["label"], r.level))
    return 0


if __name__ == "__main__":
    sys.exit(main())
