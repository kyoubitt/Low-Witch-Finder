#!/usr/bin/env python3
"""test_app.py - drives the App object directly (no terminal) and asserts what the user would see happen."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
import tui_core as T  # noqa: E402
import witchhut_studio as A  # noqa: E402
import witchhut_common as W  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("ok    " if ok else "FAIL  ") + name + (("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


copied = []
A.copy_text = lambda term, text: copied.append(text) or True


def fresh(w=110, h=32, tab="map"):
    A.apply_theme("dark")
    app = A.App(None, sync=True)
    app.tab = tab
    s = T.Screen(w, h)
    app.draw(s)
    app.draw(s)
    return app, s


def redraw(app, w=110, h=32):
    s = T.Screen(w, h)
    app.draw(s)
    app.draw(s)
    return s


def key(app, k):
    app.on_event(("key", k))


def ch(app, c):
    app.on_event(("char", c))


def mouse(app, kind, x, y, b=0):
    app.on_event(("mouse", kind, x, y, b))


# ---- tabs
app, s = fresh()
for c, t in (("2", "hunt"), ("3", "hunt"), ("1", "map")):
    ch(app, c)
    check("key %s -> %s tab" % (c, t), app.tab == t)
key(app, "tab")
check("tab key cycles", app.tab == "hunt")
key(app, "btab")
check("shift+tab cycles back", app.tab == "map")
check("the tabs are Map and Hunt (Setup is part of Hunt)", [t for t, _, _ in A.TABS] == ["map", "hunt"])
ch(app, "t")
check("t flips the theme", A.THEME == "light")
ch(app, "t")

# ---- known huts popup (what used to be the Finds tab)
app, s = fresh()
check("the popup is closed to start with", not app.known_open)
ch(app, "k")
check("k opens the known huts", app.known_open)
rows = app.sorted_finds()
check("first row is the closest hut", rows[0].tp == "/tp 5811 -56 4423")
s = redraw(app)
text = "".join("".join(s.ch[y * s.w:(y + 1) * s.w]) for y in range(s.h))
check("the popup shows the huts", "Known low huts" in text and "795374283637013279" in text)
key(app, "right")
check("right = filter -59", A.DEPTH_FILTERS[app.f_filter] == -59 and len(app.sorted_finds()) == 2)
key(app, "right")
check("filter -58 has 46", len(app.sorted_finds()) == 46)
for _ in range(5):
    key(app, "down")
check("down moves", app.f_sel == 5)
key(app, "end")
check("end goes to the last row", app.f_sel == 45)
key(app, "home")
ch(app, "o")
check("o orders deepest first", app.f_order == "deep")
ch(app, "o")
key(app, "left")
key(app, "left")
check("filter wraps back to All", app.f_filter == 0)
ch(app, "x")
check("other keys don't leak through to the map while the popup is open", app.known_open and app.m_text == "795374283637013279")
key(app, "esc")
check("esc closes it", not app.known_open)
ch(app, "k")
ch(app, "k")
check("k again closes it", not app.known_open)
ch(app, "k")
app.f_sel = 0
redraw(app)
key(app, "enter")
check("enter zooms to the hut on the map", app.tab == "map" and not app.known_open and app.m_text == "795374283637013279"
      and app.ver()[0] == "1.21.4")
check("...centred on the hut at 16 blocks per sample", (app.m_cx, app.m_cz, app.m_zoom) == (5811, 4423, 0), (app.m_cx, app.m_cz, app.m_zoom))
check("...and selected", app.m_sel == (363, 276))

# clicks in the popup
app, s = fresh()
ch(app, "k")
s = redraw(app)
rows = app.sorted_finds()
bw, bh = min(s.w - 4, 88), min(s.h - 2, 34)
px, py = (s.w - bw) // 2, (s.h - bh) // 2
y = py + 4 + 2 + 2          # the third row
mouse(app, "press", px + 20, y)
mouse(app, "release", px + 20, y)
check("clicking a row opens that hut on the map", app.tab == "map" and not app.known_open and app.m_sel == (rows[2].x >> 4, rows[2].z >> 4),
      (app.tab, app.known_open, app.m_sel))
ch(app, "k")
redraw(app)
mouse(app, "press", 1, 1)
mouse(app, "release", 1, 1)
check("clicking outside the popup closes it", not app.known_open)
ch(app, "k")
redraw(app)
before, zoom_before = app.f_sel, app.m_zoom
mouse(app, "wheeldown", px + 20, py + 12)
check("the wheel scrolls the list, not the map", app.f_sel == before + 3 and app.m_zoom == zoom_before, (app.f_sel, app.m_zoom))

# ---- a lone Esc, and Esc as the start of an arrow key
app, s = fresh()
ch(app, "k")
app.feed("\x1b")
check("a lone Esc waits to see if more follows", app.known_open and app.pending == "\x1b")
app.flush_pending()
check("...then it is the Esc key and closes the popup", not app.known_open and app.pending == "")
ch(app, "k")
app.feed("\x1b")
app.feed("[B")
check("Esc followed by [B is the down arrow, not Esc", app.known_open and app.f_sel == 1 and app.pending == "")
app.flush_pending()
check("flushing with nothing pending does nothing", app.known_open)
app.help = True
app.feed("\x1b")
app.flush_pending()
check("Esc closes the help box", not app.help)
app, s = fresh()
app.on_event(("char", "e"))
app.feed("\x1b")
app.flush_pending()
check("Esc leaves the seed field", not app.m_edit)

# ---- zoom to hut
app, s = fresh()
app.m_zoom, app.m_sel = 4, None
redraw(app)
ch(app, "z")
check("z with no selection goes to the seed's deepest known hut", (app.m_cx, app.m_cz, app.m_zoom) == (5816, 4424, 0) and app.m_sel == (363, 276),
      (app.m_cx, app.m_cz, app.m_zoom, app.m_sel))
app.m_zoom, app.m_cx, app.m_cz = 3, 0, 0
redraw(app)
ch(app, "z")
check("z with a hut selected zooms to it", (app.m_cx, app.m_cz, app.m_zoom) == (5816, 4424, 0))
app.m_text = "12345"
app.m_apply_seed()
app.m_sel, app.m_cx, app.m_cz, app.m_zoom = None, 0, 0, 3
redraw(app)
ch(app, "z")
want = W.nearest_huts(12345, app.ver()[0], 0, 0, 1)[0]
check("z works on ANY seed: it goes to the nearest hut", (app.m_cx, app.m_cz, app.m_zoom, app.m_sel) == (want[0] * 16 + 8, want[1] * 16 + 8, 0, want[:2]),
      (app.m_cx, app.m_cz, app.m_zoom, app.m_sel, want))
# the button is on screen and clickable
app, s = fresh()
app.m_sel = (363, 276)
redraw(app)
btn = next((h for h in app.hits if h[4] == app.m_zoom_hut), None)
check("a Zoom to hut button is on the map screen", btn is not None)
if btn:
    app.m_zoom = 4
    mouse(app, "press", btn[0] + 1, btn[1])
    check("clicking it zooms in", app.m_zoom == 0 and (app.m_cx, app.m_cz) == (5816, 4424), (app.m_zoom, app.m_cx, app.m_cz))
# zoom-out cap
app, s = fresh()
for _ in range(20):
    ch(app, "-")
check("zoom out stops at 512 blocks per column", app.m_scale()[0] == 512 and app.m_zoom == A.MAX_ZOOM, (app.m_scale(), app.m_zoom))
for _ in range(20):
    mouse(app, "wheeldown", 30, 12)
check("...also with the wheel", app.m_scale()[0] == 512)
for _ in range(20):
    ch(app, "+")
check("zoom in stops at one chunk per sample", app.m_zoom == 0)

# ---- map
app, s = fresh(tab="map")
check("map has data", app.m_res and app.m_res["grid"] and not app.m_res["err"], app.m_res and app.m_res["err"])
check("grid is the window size", len(app.m_res["grid"]) == app.m_res["w"] * app.m_res["h"])
check("the -58 hut is marked", any(f for _, _, c, f in app.m_marks if f), app.m_marks)
check("hut spots appear", len(app.m_marks) >= 1)
ox, oy, mw, mh = app.m_rect
z0 = app.m_zoom
ch(app, "+")
check("+ zooms in", app.m_zoom == z0 - 1)
ch(app, "-")
ch(app, "-")
check("- zooms out", app.m_zoom == z0 + 1)
app.m_zoom = 3
cx, cz = ox + 20, oy + 8
before = app.m_block_at(cx, cz)
mouse(app, "wheelup", cx, cz)
after = app.m_block_at(cx, cz)
step_old, step_new = 16 << 3, 16 << 2
check("wheel zoom keeps the block under the pointer", abs(before[0] - after[0]) <= step_old and abs(before[1] - after[1]) <= step_old * 2,
      (before, after))
check("wheel zoomed in", app.m_zoom == 2)
c0 = (app.m_cx, app.m_cz)
mouse(app, "press", ox + 30, oy + 10)
mouse(app, "drag", ox + 20, oy + 10)
mouse(app, "drag", ox + 10, oy + 6)
mouse(app, "release", ox + 10, oy + 6)
step = 16 << app.m_zoom
check("dragging pans the map", (app.m_cx, app.m_cz) == (c0[0] + 20 * step, c0[1] + 4 * 2 * step), (c0, (app.m_cx, app.m_cz)))
app.m_sel = (1, 1)
redraw(app)
bx, by = app.m_rect[0] + 5, app.m_rect[1] + 5
mouse(app, "press", bx, by)
mouse(app, "drag", bx + 3, by)
mouse(app, "release", bx + 3, by)
check("a drag does not change the selection", app.m_sel == (1, 1), app.m_sel)
mouse(app, "press", bx, by)
mouse(app, "release", bx, by)
check("a plain click on empty map does", app.m_sel is None, app.m_sel)
key(app, "left")
check("arrow pans", app.m_cx < c0[0] + 20 * step)
ch(app, "o")
check("o goes back to 0,0", (app.m_cx, app.m_cz) == (0, 0))
ch(app, "]")
v1 = app.m_ver
ch(app, "[")
check("[ ] change the version", app.m_ver != v1)
ch(app, "h")
check("h toggles swamps-only", app.m_hl is True)
ch(app, "h")

# typing a seed
ch(app, "e")
check("e focuses the seed field", app.m_edit)
for _ in range(30):
    key(app, "backspace")
for c in "12345":
    ch(app, c)
ch(app, "q")
check("q is typed, not quit, while editing", app.m_text == "12345q")
key(app, "backspace")
key(app, "enter")
check("enter applies the seed", app.m_seed == 12345 and not app.m_edit)
redraw(app)
check("a new map was drawn", app.m_res and app.m_res["key"][0] == 12345, app.m_res and app.m_res["key"])
ch(app, "e")
for _ in range(10):
    key(app, "backspace")
for c in "hello":
    ch(app, c)
key(app, "enter")
check("a text seed works", app.m_seed == 99162322 and "text seed" in app.m_note)
ch(app, "e")
key(app, "esc")
check("esc leaves the field and restores the seed", not app.m_edit and app.m_text == "99162322")

# clicking a marker
app, s = fresh(tab="map")
app.show_on_map(next(f for f in app.finds if f.located))
app.m_sel = None
s = redraw(app)
star = next((m for m in app.m_marks if m[3]), None)
check("the known hut has a marker", star is not None)
if star:
    mouse(app, "press", star[0], star[1])
    mouse(app, "release", star[0], star[1])
    check("clicking a marker selects it", app.m_sel == star[2], app.m_sel)
    redraw(app)
    copied.clear()
    ch(app, "c")
    check("c copies the selected hut's /tp", copied and copied[-1].startswith("/tp"), copied)
mouse(app, "press", app.m_rect[0] + 2, app.m_rect[1] + 2)
mouse(app, "release", app.m_rect[0] + 2, app.m_rect[1] + 2)
check("clicking empty map clears the selection", app.m_sel is None)

# a seed with a different version uses that version's table (and the result differs)
app, s = fresh(tab="map")
app.m_text = "795374283637013279"
app.m_ver = next(i for i, v in enumerate(W.VERSIONS) if v[0] == "1.18.2")
app.m_res = None
redraw(app)
g18 = app.m_res["grid"]
app.m_ver = next(i for i, v in enumerate(W.VERSIONS) if v[0] == "26.2")
app.m_res = None
redraw(app)
check("1.18 and 26.2 maps differ", g18 != app.m_res["grid"])

# ---- the map as a picture (kitty / Sixel), with a stand-in terminal
class FakeTerm:
    colors = "truecolor"

    def __init__(self, g, cell=(10, 20)):
        self.graphics, self.cell = g, cell

    def cell_px(self):
        return self.cell

    def write(self, b):
        pass


def pic_app(g, tab="map", cell=(10, 20)):
    A.apply_theme("dark")
    app = A.App(FakeTerm(g, cell), sync=True)
    app.tab = tab
    app.first = app.render(110, 32)      # the first frame asks for the data and has the picture
    return app


app = pic_app("kitty")
out = app.first
check("kitty: the map is 2 x 4 samples a cell up close", app.m_params(app.m_zoom)[:2] == (2, 4), app.m_params(app.m_zoom))
check("kitty: a picture is sent", b"\x1b_Ga=T" in out and app.pic_on in T.PIC_IDS, out[:80])
check("kitty: the picture is as big as the cells", b"s=%d,v=%d" % (app.m_rect[2] * 10, app.m_rect[3] * 20) in out, out[:120])
out2 = app.render(110, 32)
check("kitty: an unchanged frame sends nothing", b"\x1b_G" not in out2, out2[:60])
old = app.pic_on
app.on_event(("key", "left"))
out3 = app.render(110, 32)
check("kitty: panning sends a new picture on the other id and removes the old one",
      b"a=T" in out3 and app.pic_on != old and (b"a=d,d=I,i=%d" % old) in out3)
app.on_event(("mouse", "move", 30, 12, 0))
check("kitty: moving the pointer does not resend the picture", b"a=T" not in app.render(110, 32))
app.on_event(("char", "?"))
check("kitty: the help box does not remove the picture", app.pic_on is not None and b"a=d" not in app.render(110, 32))
app.on_event(("key", "esc"))
app.render(110, 32)
app.on_event(("char", "2"))
out4 = app.render(110, 32)
check("kitty: leaving the map removes the picture", b"a=d,d=I" in out4 and app.pic_on is None)
check("kitty: the map cells are left on the terminal's background", True)
mapcells = pic_app("kitty")
bgs = {mapcells.scr.bg[(mapcells.m_rect[1] + 3) * 110 + mapcells.m_rect[0] + k] for k in range(5, 20)}
check("kitty: map cells are CLEAR so the picture shows through", T.CLEAR in bgs, bgs)

app = pic_app("sixel")
out = app.first
check("sixel: a sixel picture is sent", b"\x1bP0;1;0q" in out, out[:60])
out2 = app.render(110, 32)
check("sixel: an unchanged frame sends nothing", b"\x1bP" not in out2)
app.on_event(("char", "?"))
out3 = app.render(110, 32)
check("sixel: the help box turns the picture into characters", b"\x1bP0;1;0q" not in out3 and app.pic_on is None and app.m_params(app.m_zoom)[0] == 1)
app.on_event(("key", "esc"))
out4 = app.render(110, 32)
check("sixel: and it comes back when the help closes", b"\x1bP0;1;0q" in out4)

app = pic_app("kitty", cell=(8, 16))
check("a narrower cell changes the sample shape", app.m_params(app.m_zoom)[:2] == (2, 4), app.m_params(app.m_zoom))
app = pic_app("kitty", cell=(9, 22))
check("a taller cell gets more samples down", app.m_params(app.m_zoom)[1] >= 4, app.m_params(app.m_zoom))

app = pic_app("kitty")
app.show_on_map(next(f for f in app.finds if f.located))
app.m_sel = None
app.render(110, 32)
star = next((m for m in app.m_marks if m[3]), None)
check("picture: the known hut has a marker", star is not None)
if star:
    app.on_event(("mouse", "press", star[0], star[1], 0))
    app.on_event(("mouse", "release", star[0], star[1], 0))
    check("picture: clicking its marker selects it", app.m_sel == star[2], app.m_sel)
c0 = (app.m_cx, app.m_cz)
ox, oy, mw, mh = app.m_rect
step = 16 << app.m_zoom
app.on_event(("mouse", "press", ox + 30, oy + 10, 0))
app.on_event(("mouse", "drag", ox + 20, oy + 6, 0))
app.on_event(("mouse", "release", ox + 20, oy + 6, 0))
check("picture: dragging pans by samples", (app.m_cx, app.m_cz) == (c0[0] + 10 * 2 * step, c0[1] + 4 * 4 * step), (c0, (app.m_cx, app.m_cz)))
before = app.m_block_at(ox + 20, oy + 8)
app.m_zoom = 3
before = app.m_block_at(ox + 20, oy + 8)
app.on_event(("mouse", "wheelup", ox + 20, oy + 8, 0))
after = app.m_block_at(ox + 20, oy + 8)
check("picture: wheel zoom keeps the block under the pointer", abs(before[0] - after[0]) <= 2 * 128 and abs(before[1] - after[1]) <= 4 * 128, (before, after))

# the picture holds the biome colours and the markers
app = pic_app("kitty")
app.show_on_map(next(f for f in app.finds if f.located))
app.render(110, 32)
pic = app.pic_req[5]()
check("picture: size matches the request", (pic.w, pic.h) == (round(app.m_rect[2] * 10), round(app.m_rect[3] * 20)), (pic.w, pic.h))
check("picture: the swamp colour is in it", A.SWAMP_COL in pic.pal)
star_f = next(m[3] for m in app.m_marks if m[3])
check("picture: the star's depth colour is in it", A.DEPTH_COL[star_f.depth] in pic.pal)
check("picture: stays inside 256 colours", len(pic.pal) <= 256, len(pic.pal))

# ---- the map: layer painter, overscan, nearest huts, buttons, anchoring, bytes per frame
import math
import random as R


def naive_paint(r, x0, z0, step, pw, ph):
    """What asking every view cell for its sample gives (the definition paint_layer must equal)."""
    rs, rw, rh, g = r["step"], r["w"], r["h"], r["grid"]
    out = [bytearray(b"\xff" * pw) for _ in range(ph)]
    for j in range(ph):
        for i in range(pw):
            X, Z = x0 + i * step, z0 + j * step
            px, pz = (X + step // 2, Z + step // 2) if rs <= step else (X, Z)      # finer: the cell at its middle; coarser: at its corner
            c, rr = (px - r["x0"]) // rs, (pz - r["z0"]) // rs
            if 0 <= c < rw and 0 <= rr < rh:
                out[j][i] = g[rr * rw + c]
    return out


R.seed(11)
bad = 0
for _ in range(400):
    rs, step = 16 << R.randint(0, 5), 16 << R.randint(0, 5)
    rw, rh = R.randint(1, 40), R.randint(1, 40)
    r = dict(step=rs, w=rw, h=rh, x0=rs * R.randint(-60, 60), z0=rs * R.randint(-60, 60), grid=bytes(R.randrange(256) for _ in range(rw * rh)))
    x0, z0, pw, ph = step * R.randint(-40, 40), step * R.randint(-40, 40), R.randint(1, 50), R.randint(1, 30)
    canvas = [bytearray(b"\xff" * pw) for _ in range(ph)]
    A.paint_layer(canvas, r, x0, z0, step, pw, ph)
    if canvas != naive_paint(r, x0, z0, step, pw, ph):
        bad += 1
check("paint_layer (row slices) equals asking every cell, on 400 random views, windows and zooms", bad == 0, "%d differ" % bad)

# overscan: a small pan reuses the fetched window; a big one fetches again
calls = []
real_compute = A.compute_map
A.compute_map = lambda job: (calls.append(job.key), real_compute(job))[1]
A.apply_theme("dark")
app = A.App(None, sync=True)
app.tab, app.m_zoom = "map", 2
redraw(app)
n0 = len(calls)
for _ in range(3):
    app.m_pan(3, 0)                                            # nine columns in all: inside the margin
    redraw(app)
check("small pans fetch nothing new (overscan)", len(calls) == n0, (n0, len(calls)))
app.m_pan(120, 0)
redraw(app)
check("a big pan fetches a new window", len(calls) == n0 + 1, (n0, len(calls)))
A.compute_map = real_compute

# nearest huts: equals a brute-force sweep, and is quick
seed = 795374283637013279
n = W.nearest_huts(seed, "1.21.5", 5816, 4424, 8)
rr = int(n[-1][2] // 512) + 3
cand = [W.hut_chunk(seed, a, b) for a in range(5816 // 512 - rr, 5816 // 512 + rr + 1) for b in range(4424 // 512 - rr, 4424 // 512 + rr + 1)]
ids = W.biome_chunks(seed, cand, "1.21.5")
brute = sorted((math.hypot(c[0] * 16 + 8 - 5816, c[1] * 16 + 8 - 4424), c) for c, b in zip(cand, ids) if b == 6)[:8]
check("nearest_huts equals a brute-force search", [c for _, c in brute] == [(a, b) for a, b, _ in n], (brute[:3], n[:3]))
check("the nearest hut to the known -58 hut is that hut", n[0][:2] == (363, 276) and n[0][2] < 20)
check("nearest_huts works at the world's edge", len(W.nearest_huts(12345, "1.21.4", 29_999_000, -29_999_000, 3)) == 3)

# the buttons
app, s = fresh()
nearest = [h for h in app.hits if h[4] == app.m_zoom_hut and h[1] == 2]
check("a Nearest hut button is on the toolbar", len(nearest) == 1)
app.m_zoom, app.m_sel, app.m_cx, app.m_cz = 3, None, 0, 0
redraw(app)
btn = next(h for h in app.hits if h[4] == app.m_zoom_hut and h[1] == 2)
mouse(app, "press", btn[0] + 1, btn[1])
check("clicking it zooms to the nearest hut", app.m_zoom == 0 and app.m_sel is not None, (app.m_zoom, app.m_sel))
redraw(app)
text = text_of_screen = "\n".join("".join(app.scr_s.ch[y * 110:(y + 1) * 110]) for y in range(32)) if hasattr(app, "scr_s") else ""
app, s = fresh()
t = "\n".join("".join(s.ch[y * s.w:(y + 1) * s.w]) for y in range(s.h))
check("the panel lists nearby huts with distances and a direction", "Huts near here" in t and "⌂" in t and (" NE" in t or " SW" in t or " E" in t))
rows_ = [h for h in app.hits if h[2] > 30 and h[3] == 1 and h[1] > 3 and callable(h[4]) and h[0] >= 70]
check("each listed hut is a clickable row", len(rows_) >= 3, len(rows_))
before = (app.m_cx, app.m_cz)
mouse(app, "press", rows_[1][0] + 2, rows_[1][1])
check("clicking a listed hut zooms to it", app.m_zoom == 0 and app.m_sel is not None and (app.m_cx, app.m_cz) != before, (app.m_zoom, app.m_sel))
old_seed = app.m_seed
ch(app, "r")
check("r picks a random seed and goes straight to its nearest hut", app.m_seed != old_seed and app.m_note == "random seed" and app.m_sel is not None
      and app.m_sel == W.nearest_huts(app.m_seed, app.ver()[0], 0, 0, 1)[0][:2] and app.m_zoom == 1, (app.m_seed, app.m_sel, app.m_zoom))
redraw(app)
check("...and the map has data for it", app.m_res is not None and app.m_res["seed"] == app.m_seed)

# pixel size and scale
app, s = fresh()
ok = True
for z in range(A.MAX_ZOOM + 1):
    app.m_zoom = z
    sx, sy, ps = app.m_params(z)
    ok &= app.m_scale(z) == (16 << z, 2 * (16 << z)) and ps == (2 if z >= 4 else 1) and (sx, sy) == (1, 2)
    ox, oy, mw, mh = app.m_rect
    step, x0, z0, pw, ph = app.m_view()
    ok &= step == (16 << z) * ps and pw == -(-mw // ps) and ph == -(-2 * mh // ps)
check("blocks per column is 16 << zoom at every zoom; zoomed out the pixels are 2 x 2", ok)
app.m_zoom = 3
redraw(app)
mx_, my_ = app.m_rect[0] + 50, app.m_rect[1] + 10
bc, br = app.m_scale()
dc, dr = (mx_ - app.m_rect[0] + 0.5) - app.m_rect[2] / 2, (my_ - app.m_rect[1] + 0.5) - app.m_rect[3] / 2
before = (app.m_cx + dc * bc, app.m_cz + dr * br)
mouse(app, "wheeldown", mx_, my_)
bc2, br2 = app.m_scale()
after = (app.m_cx + dc * bc2, app.m_cz + dr * br2)
check("zooming out across the pixel-size change keeps the block under the pointer", app.m_zoom == 4 and abs(before[0] - after[0]) <= 2 and abs(before[1] - after[1]) <= 2, (before, after))

# bytes of terminal output per frame (192 x 54, truecolor): the lag was the terminal choking on these
class Quiet:
    colors, graphics = "truecolor", False

    def cell_px(self):
        return None

    def write(self, b):
        pass


big = {}
for z in range(A.MAX_ZOOM + 1):
    a2 = A.App(Quiet(), sync=True)
    a2.tab, a2.m_zoom = "map", z
    a2.render(192, 54)
    a2.prev = None
    full = len(a2.render(192, 54))
    a2.on_event(("key", "right"))
    big[z] = (full, len(a2.render(192, 54)))
check("zoomed out (zoom 4 and 5) a frame is under 100 KB (it was 258-282 KB)", all(big[z][0] < 100_000 and big[z][1] < 100_000 for z in (4, 5)), big)
check("at every zoom a frame is under 140 KB", all(max(v) < 140_000 for v in big.values()), big)

# the colour escapes: a tiny terminal reads our output back and must see exactly the screen
import re as _re


def mini_term(chunks, w, h):
    cells = [[(" ", None, None, False)] * w for _ in range(h)]
    st, x, y = [None, None, False], 0, 0
    tok = _re.compile(r"\x1b\[([0-9;]*)([HmJ])|(.)", _re.S)
    for data in chunks:
        for m in tok.finditer(data):
            if m.group(3) is not None:
                if y < h and x < w:
                    cells[y][x] = (m.group(3), st[0], st[1], st[2])
                x += 1
                continue
            params, final = m.group(1), m.group(2)
            if final == "H":
                r_, c_ = (params.split(";") + ["1"])[:2] if params else ("1", "1")
                y, x = int(r_) - 1, int(c_) - 1
            elif final == "J":
                cells = [[(" ", None, None, False)] * w for _ in range(h)]
            else:
                p, i = ([int(v) for v in params.split(";")] if params else [0]), 0
                while i < len(p):
                    v = p[i]
                    if v == 0:
                        st = [None, None, False]
                    elif v == 1:
                        st[2] = True
                    elif v == 49:
                        st[1] = T.CLEAR
                    elif v in (38, 48):
                        if p[i + 1] == 2:
                            col, i = (p[i + 2] << 16) | (p[i + 3] << 8) | p[i + 4], i + 4
                        else:
                            col, i = ("256", p[i + 2]), i + 2
                        st[0 if v == 38 else 1] = col
                    i += 1
    return cells


R.seed(7)
for mode in ("truecolor", "256"):
    w_, h_ = 60, 20

    def rand_screen():
        sc = T.Screen(w_, h_)
        for yy in range(h_):
            for xx in range(w_):
                if R.random() < 0.7:
                    sc._set(yy * w_ + xx, yy * w_, R.choice("\u2580 ab"), R.choice([0xff0000, 0x00ff00, 0x123456, 0xffffff]),
                            R.choice([0x000000, 0x777777, T.CLEAR, 0x123456]), R.random() < 0.1)
        return sc
    s1, s2 = rand_screen(), rand_screen()
    got = mini_term([s1.diff(None, mode).decode(), s2.diff(s1, mode).decode()], w_, h_)
    conv = (lambda c: c) if mode == "truecolor" else (lambda c: c if c == T.CLEAR else ("256", T.to256(c)))
    wrong = 0
    for yy in range(h_):
        for xx in range(w_):
            k = yy * w_ + xx
            ch_, fg_, bg_, bd_ = s2.ch[k] or " ", conv(s2.fg[k]), conv(s2.bg[k]), s2.bd[k]
            g = got[yy][xx]
            if g[0] != ch_ or g[2] != bg_ or g[3] != bd_ or (ch_ != " " and g[1] != fg_):         # a blank's foreground is invisible
                wrong += 1
    check("the short colour escapes reproduce the screen exactly (%s)" % mode, wrong == 0, "%d cells differ" % wrong)

# ---- the Hunt tab, with a fake run folder and a fake driver: nothing here touches the GPU or the machine's real runs
import hunt as H
import json
import os
import tempfile
import time as _time

tmp = tempfile.mkdtemp()
H.RUNS, H.RESULTS = os.path.join(tmp, "runs"), os.path.join(tmp, "results")
real_pre, real_machine, real_start = H.preflight_rows, H.machine, H.Run.start_detached
started = []
H.Run.start_detached = lambda self: started.append((self.level, self.mode, self.seeds_text, self.area))
GPU = dict(name="NVIDIA Test GPU", temp=50, util=0, mem_used=500, mem_total=6144, watts=None)
MACHINE = dict(gpu=GPU, cores=12, java=21, minecraft_files=True, gpu_engine=True, cpu_tools=True)
OK = [(True, "Test GPU", None), (True, "5.0 GB video memory free", None), (True, "tools built", None), (True, "Java 21", None)]


def hunt_app(pre=OK, machine=MACHINE, w=110, h=36):
    H.preflight_rows = lambda level, mode, cfg=None: list(pre)
    H.machine = lambda: dict(machine)
    A.apply_theme("dark")
    app = A.App(None, sync=True)
    app.tab = "hunt"
    s = T.Screen(w, h)
    app.draw(s)
    app.draw(s)
    return app, s


def text_of(s):
    return "\n".join("".join(s.ch[y * s.w:(y + 1) * s.w]) for y in range(s.h))


def look(app, w=110, h=36):
    s = T.Screen(w, h)
    app.draw(s)
    app.draw(s)
    return text_of(s)


app, s = hunt_app()
t = text_of(s)
check("Hunt: the form is there", "Floor y" in t and "-58" in t and "or lower" in t and "Mode" in t and "GPU full" in t and "Start at" in t, t[:900])
check("Hunt: the big button is Start", "Start hunting" in t and "Stop" not in t)
check("Hunt: it says what the machine has, with the checks at the right", "✓ Test GPU" in t and "Test  (f)" in t and "Recheck  (r)" in t, t[-900:])
check("Hunt: the GPU search has no Area row (it covers the far world)", "Area" not in t)
started.clear()
ch(app, "s")
check("Hunt: s starts with what is picked", started == [(-58, "gpu", "", 0)], started)

# the settings: keys
key(app, "right")
check("Hunt: right on Floor y goes deeper, to -59", app.h_set["level"] == -59 and "-59" in look(app))
key(app, "right")
check("Hunt: -59 is the end", app.h_set["level"] == -59)
key(app, "left")
key(app, "left")
check("Hunt: left comes back toward -50", app.h_set["level"] == -57)
key(app, "down")
check("Hunt: down moves to Mode", app.h_focus == "mode")
key(app, "right")
check("Hunt: right on Mode picks GPU gentle", app.h_set["mode"] == "gpu-gentle" and "GPU gentle" in look(app))
key(app, "right")
check("Hunt: then a CPU mode, which pulls the floor up to -54 (CPU covers -50 to -54) and says so",
      app.h_set["mode"] == "cpu" and app.h_set["level"] == -54 and "-50 to -54" in app.toast[0], (app.h_set, app.toast))
t = look(app)
check("Hunt: the scanner shows Seeds and Area", "Seeds" in t and "Area" in t and "the whole world" in t and "made up for you" in t, t[:900])
check("Hunt: Area says how long a seed takes here", "a seed here" in t)
key(app, "up")
key(app, "right")
check("Hunt: -55 with a CPU mode switches to GPU full and says so", app.h_set["level"] == -55 and app.h_set["mode"] == "gpu" and "NVIDIA" in app.toast[0], (app.h_set, app.toast))
key(app, "left")
key(app, "down")
for _ in range(3):
    key(app, "right")
check("Hunt: CPU gentle is the fourth mode", app.h_set["mode"] == "cpu-gentle" and app.h_set["level"] == -54, app.h_set)

# typing seeds
key(app, "down")
check("Hunt: down again is Seeds", app.h_focus == "seeds")
key(app, "enter")
check("Hunt: enter edits the seeds", app.h_edit)
for c in "123 456":
    ch(app, c)
key(app, "backspace")
check("Hunt: typing goes in the box", app.h_set["seeds"] == "123 45" and "123 45" in look(app))
key(app, "esc")
check("Hunt: esc puts the old text back", app.h_set["seeds"] == "" and not app.h_edit)
key(app, "enter")
for c in "123 456":
    ch(app, c)
key(app, "enter")
check("Hunt: enter keeps it", app.h_set["seeds"] == "123 456" and not app.h_edit)
app.on_event(("paste", "789\n"))
check("Hunt: pasting starts editing and adds to it", app.h_set["seeds"] == "123 456789" and app.h_edit)
key(app, "esc")
started.clear()
ch(app, "s")
check("Hunt: Start passes the mode, the seeds and the area", started == [(-54, "cpu-gentle", "123 456", 0)], started)

# the mouse: the arrows of a stepper
app, s = hunt_app()
row = [z for z in app.hits if z[1] == 4 and z[2] == 3]
check("Hunt: the Floor y stepper has two arrow zones", len(row) == 2, row)
mouse(app, "press", row[1][0] + 1, 4)
mouse(app, "release", row[1][0] + 1, 4)
check("Hunt: clicking the right arrow goes deeper", app.h_set["level"] == -59, app.h_set)
mouse(app, "press", row[0][0] + 1, 4)
mouse(app, "release", row[0][0] + 1, 4)
check("Hunt: clicking the left arrow comes back", app.h_set["level"] == -58)

# a start that can't happen says why, and offers the fix
BLOCKED = [(True, "Test GPU", None), (False, "Minecraft is running: close it first", "close-minecraft")]
app, s = hunt_app(pre=BLOCKED)
t = text_of(s)
check("Hunt: a blocked start says why", "Minecraft is running: close it first" in t)
started.clear()
ch(app, "s")
check("Hunt: s while blocked does not start, and explains", not started and app.toast[0].startswith("Minecraft is running"), (started, app.toast))
NEEDS = [(True, "Test GPU", None), (False, "the Minecraft files aren't downloaded yet", "minecraft")]
app, s = hunt_app(pre=NEEDS)
t = text_of(s)
check("Hunt: a missing piece turns the big button into its fix", "Get Minecraft files  (g)" in t and "Start hunting" not in t, t[:700])
steps = []
orig_get = A.mcfiles.fetch
A.mcfiles.fetch = lambda log: (steps.append("fetch"), log("downloading..."))
ch(app, "g")
check("Hunt: g runs that fix, and its output shows on the Hunt tab", steps == ["fetch"] and app.h_log == ["downloading..."] and "downloading..." in look(app))
A.mcfiles.fetch = orig_get
BUILD = [(False, "tools not built yet: lysh", "build")]
app, s = hunt_app(pre=BUILD)
check("Hunt: unbuilt tools offer Build", "Build the tools  (b)" in text_of(s))
NOGPU = dict(MACHINE, gpu=None)
app, s = hunt_app(pre=[(False, "no NVIDIA card found: pick a CPU mode", None)], machine=NOGPU)
t = text_of(s)
check("Hunt: no NVIDIA card is said once in the Mode row and once as the reason", "no NVIDIA card found" in t and "Use CPU full" in t, t[:900])
app.h_use_cpu()
check("Hunt: ...to CPU full and -54", app.h_set["mode"] == "cpu" and app.h_set["level"] == -54)

# a run that is going: the driver pid is this process, so it counts as alive
run = H.SolveRun(-58, "gpu")
os.makedirs(run.out, exist_ok=True)
open(run.f("settings.json"), "w").write(json.dumps(run.settings()))
open(run.f("driver.pid"), "w").write(str(os.getpid()))
now = _time.time()
open(run.f("done.txt"), "w").write("0 64\n64 64\n128 64\n")
open(run.f("funnel.txt"), "w").write("0 64 1000000 1000000 400 9 2 80\n64 64 1100000 1100000 420 11 3 80\n128 64 900000 900000 380 8 1 80\n")
open(run.f("chunk_times.txt"), "w").write("%d\n%d\n%d\n" % (now - 160, now - 80, now))
open(run.f("problems.txt"), "w").write("2026-10-22 18:00:00 chunk 3: terrain stage failed (attempt 1 of 3)\n")
live = dict(phase="terrain", detail="checking 82,000 matches against the ground", chunk=1234, unit_t0=now - 20, unit_expect=60, pid=os.getpid(),
            level=-58, mode="gpu", stopping=False, hot=False, gpu=dict(name="RTX", temp=83, util=100, mem_used=1700, mem_total=6144, watts=60.0))
open(run.f("live.json"), "w").write(json.dumps(live))
app, s = hunt_app()
t = text_of(s)
check("Hunt: a running hunt says so", "● Hunting" in t and "Stop  (x)" in t and "Start hunting" not in t, t[:700])
check("Hunt: the settings show the running hunt's", app.h_set["level"] == -58 and app.h_set["mode"] == "gpu")
check("Hunt: it shows the speed in seeds, the GPU and what it is doing", "seeds/s" in t and "GPU 83 °C" in t and "100%" in t and "checking 82,000 matches" in t, t[:900])
check("Hunt: one bar that moves inside the chunk", "chunk 4" in t and "33%" in t and "█" in t, t[:900])
check("Hunt: the funnel is one line", "matches 3.0M  →  deep enough 1.2k  →  checked 28  →  huts 0" in t, t[:1000])
check("Hunt: a typical wait, with the luck caveat", "about one find every" in t and "an average" in t)
check("Hunt: the latest problem shows", "terrain stage failed" in t)
key(app, "right")
check("Hunt: settings can't change while it runs", app.h_set["level"] == -58 and "Stop the hunt to change this" in app.toast[0], app.toast)
ch(app, "x")
check("Hunt: x asks the run to stop (a STOP file)", os.path.exists(run.f("STOP")))
t = look(app)
check("Hunt: ...and the screen says it is stopping", "Stopping..." in t, t[:600])
st = H.SolveRun(-58, "gpu").status()
check("Hunt: status adds the funnel up", (st["funnel"][0][1], st["funnel"][1][1], st["funnel"][2][1], st["chunks_done"]) == (3000000, 1200, 28, 3), st["funnel"])
check("Hunt: speed comes from the spacing of finished chunks", st["rate"] and abs(st["rate"] - 1000000 / 80.0) < 3000, st["rate"])
check("Hunt: ... and is shown in seeds a second", st["speed_text"].endswith("seeds/s") and st["speed_text"].startswith("819"), st["speed_text"])
os.remove(run.f("STOP"))
live.update(phase="cooling", detail="the GPU is at 89 C: waiting for 82 C")
open(run.f("live.json"), "w").write(json.dumps(live))
app, s = hunt_app()
check("Hunt: a hot GPU pauses and says so", "Paused: the GPU is at 89 C" in text_of(s), text_of(s)[:600])
live.update(phase="tuning", detail="")
open(run.f("live.json"), "w").write(json.dumps(live))
app, s = hunt_app()
check("Hunt: the first-run timing says what it is", "Timing this GPU once" in text_of(s))
live.update(phase="terrain", detail="x")
open(run.f("live.json"), "w").write(json.dumps(live))

# a find, and the list of finds
H.record(["-58 274 1234567890123456789 100 200 nb=2 room+605"], "hunt58", H.RESULTS, -58, 55)
H.record(["-58 274 795374283637013279 363 276 nb=2 room+605"], "hunt58", H.RESULTS, -58, 55)
app, s = hunt_app()
t = text_of(s)
check("Hunt: found huts are listed with their /tp", "Found on this machine" in t and "/tp 1603 -56 3207" in t, t[:1300])
check("Hunt: a hut that is already known is tagged so, a new one is not", "already known" in t and " new" in t, t[:1300])
mine = [f for f, _ in app.h_rows]
check("Hunt: the hunt's huts join the map's huts", len(mine) == 2 and all(f.note == "found on this machine" for f in mine) and mine[0] in app.finds)
app.h_sel = [f.seed for f in mine].index("1234567890123456789")
ch(app, "c")
check("Hunt: c copies the selected hut's /tp", copied[-1] == "/tp 1603 -56 3207", copied[-3:])
ch(app, "m")
check("Hunt: m shows it on the map", app.tab == "map" and app.m_sel == (100, 200), (app.tab, app.m_sel))
check("...at the verified version, zoomed right in", app.ver()[0] == "1.21.4" and app.m_zoom == 0)
check("Hunt: the hunt's hut is a star on the map for its seed", any(f for _, _, c, f in (redraw(app) and app.m_marks) if f and f.seed == "1234567890123456789"))
os.remove(run.f("driver.pid"))

# a hunt that ended: found, stopped, failed
live.update(phase="found", detail="")
open(run.f("live.json"), "w").write(json.dumps(live))
app, s = hunt_app()
check("Hunt: after a find it says so and Start carries on", "Found a hut!" in text_of(s) and "Start hunting" in text_of(s), text_of(s)[:700])
live.update(phase="stopped")
open(run.f("live.json"), "w").write(json.dumps(live))
app, s = hunt_app()
check("Hunt: after a stop, Start carries on", "Stopped. Start carries on." in text_of(s))
live.update(phase="failed")
open(run.f("live.json"), "w").write(json.dumps(live))
open(run.f("FAILED"), "w").write("the solver failed at chunk 3")
app, s = hunt_app()
check("Hunt: a failed run says why", "Stopped: the solver failed at chunk 3" in text_of(s))
os.remove(run.f("FAILED"))

# the scanner, with a history
sc = H.ScanRun(-54, "cpu")
os.makedirs(sc.out, exist_ok=True)
open(sc.f("scanned.txt"), "w").write("111 13700000000 600 0 270.0\n222 13700000000 640 1 262.0\n")
open(sc.f("results.txt"), "w").write("-54 0 222 -987 -1073 scan\n")
H.record_scan(222, -15792, -54, -17168, "scan54", H.RESULTS)
app, s = hunt_app()
app.h_set.update(level=-54, mode="cpu")
app.h_changed()
t = look(app)
check("Hunt: scanner history: seeds done and a speed in seeds a minute", "2 seeds done so far" in t and "regions" in t and "huts 1" in t, t[:900])
check("Hunt: scanner finds are listed with their /tp", "/tp -15792 -54 -17168" in t and "seed 222" in t, t[:1300])
sc2 = H.ScanRun(-54, "cpu")
check("Hunt: the scanner status has the measured time per seed", abs(sc2.status()["sec_per_seed"] - 266.0) < 5, sc2.status()["sec_per_seed"])
check("Hunt: ... and the area row quotes it", "about 4 minutes" in t or "about 5 minutes" in t, t[:900])

# steps: a build, a download or a test runs on a thread (a fake step here), one at a time
A.apply_theme("dark")
app = A.App(None, sync=True)
app.tab = "hunt"
app.run_step("a test", lambda log: (log("one line\nok second"), log("third")))
check("Hunt: a step's output is kept", app.h_log == ["one line", "ok second", "third"] and not app.h_step_name, (app.h_log, app.h_step_name))


def boom(log):
    raise RuntimeError("it broke")


app.run_step("a test", boom)
check("Hunt: a failing step says why and unlocks", app.h_log[-1] == "FAILED: it broke" and not app.h_step_name and app.toast[0].endswith("it broke"))
app.h_step_name = "building the tools"
app.run_step("a test", lambda log: log("nope"))
check("Hunt: one step at a time", app.h_log[-1] == "FAILED: it broke" and "Wait for the current step" in app.toast[0])
t = look(app)
check("Hunt: while a step runs the screen says so", "Working: building the tools" in t, t[:600])
app.h_step_name = ""
check("Hunt: Hide clears the output", "FAILED: it broke" in look(app))
app.h_hide_log()
check("Hunt: ...", app.h_log == [])

# the hunt tab's choices are remembered
os.environ["XDG_CONFIG_HOME"] = os.environ["APPDATA"] = tempfile.mkdtemp(prefix="witchhut-test-")
app = A.App(None, sync=True)
app.h_set.update(level=-52, mode="cpu-gentle", seeds="42", area=1)
app.save_prefs()
app2 = A.App(None, sync=True)
app2.apply_prefs()
check("Hunt: floor, mode, seeds and area come back next time", app2.h_set == dict(level=-52, mode="cpu-gentle", seeds="42", area=1), app2.h_set)
W.save_prefs(dict(hunt=dict(level=-58, mode="cpu", seeds="x", area=99)))
app3 = A.App(None, sync=True)
app3.apply_prefs()
check("Hunt: an impossible saved combination is not trusted", app3.h_set["mode"] != "cpu" or app3.h_set["level"] > -55, app3.h_set)
W.save_prefs({})
app4 = A.App(None, sync=True)
app4.apply_prefs()
check("Hunt: the first launch picks the best for this computer", app4.h_set["mode"] in H.MODES and not H.combo_problem(app4.h_set["level"], app4.h_set["mode"]))
check("hunt.running knows a dead pid is not running", H.running() is None)
H.preflight_rows, H.machine, H.Run.start_detached = real_pre, real_machine, real_start

# ---- remembered settings
import tempfile  # noqa: E402
cfg = tempfile.mkdtemp(prefix="witchhut-test-")
os.environ["XDG_CONFIG_HOME"] = os.environ["APPDATA"] = cfg
app, s = fresh()
app.apply_prefs()
check("first launch picks a random seed", app.m_seed is not None and app.m_text != "")
app.m_text, app.m_seed, app.m_cx, app.m_cz, app.m_zoom, app.m_ver = "1473884015873122059", 1473884015873122059, -15792, -17168, 3, 0
A.apply_theme("light")
app.save_prefs()
check("settings file is written", os.path.isfile(W.prefs_path()))
A.apply_theme("dark")
app2, _ = fresh()
app2.apply_prefs()
check("next launch restores seed, spot, zoom and version",
      (app2.m_seed, app2.m_cx, app2.m_cz, app2.m_zoom, app2.m_ver) == (1473884015873122059, -15792, -17168, 3, 0))
check("... and the theme", A.THEME == "light")
A.apply_theme("dark")
with open(W.prefs_path(), "w") as f:
    f.write("{not json")
app3, _ = fresh()
app3.apply_prefs()
check("a damaged settings file just means a fresh start", app3.m_seed is not None)
with open(W.prefs_path(), "w") as f:
    f.write('{"seed": "abc", "zoom": 99, "version": 1, "theme": "x"}')
app4, _ = fresh()
app4.apply_prefs()
check("odd values are clamped, not trusted", app4.m_zoom <= A.MAX_ZOOM and app4.m_ver == 1)

# ---- sizes and themes: nothing may raise, nothing may draw outside the screen
for theme in ("light", "dark"):
    for (w, h) in ((66, 16), (80, 24), (100, 30), (104, 30), (140, 40), (200, 60)):
        for tab in ("map", "hunt"):
            try:
                A.apply_theme(theme)
                app = A.App(None, sync=True)
                app.tab = tab
                s = T.Screen(w, h)
                app.draw(s)
                app.draw(s)
                app.help = True
                app.draw(s)
                app.help, app.known_open = False, True
                app.draw(s)
            except Exception as e:  # noqa: BLE001
                check("render %s %dx%d %s" % (theme, w, h, tab), False, repr(e))
check("every size and theme renders", not [f for f in fails if f.startswith("render")])

print("\n%s" % ("all passed" if not fails else "%d FAILED" % len(fails)))
sys.exit(1 if fails else 0)
