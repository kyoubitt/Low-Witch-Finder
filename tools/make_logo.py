#!/usr/bin/env python3
"""make_logo.py - draws assets/logo.png (the Minecraft swamp hut over the name, in the style of Minecraft's own title: stone letters with a
lit top edge and a dark block edge below) and assets/icon.ico (the hut alone, for the Windows .exe). Pixel art, scaled up without smoothing."""
import io
import os
import struct
import zlib

HERE = os.path.dirname(os.path.realpath(__file__))
HW = HH = 32                                         # the hut's own size in art pixels
hut = [[None] * HW for _ in range(HH)]


def rect(x0, y0, x1, y1, c):                         # inclusive corners
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            hut[y][x] = c


ROOF, ROOF_LIGHT, ROOF_DARK = 0x4b3320, 0x6e4b2a, 0x35231a
WALL, WALL_DARK = 0x6f4e2e, 0x5a3d23
OAK, OAK_LIGHT, OAK_DARK = 0xa07a45, 0xb89258, 0x7d5d32
GLASS, DOOR = 0x1d2630, 0x5c3a1c
WATER, WATER_LIGHT, MOSS = 0x2f5d3a, 0x4f8a4a, 0x244a2e

rect(0, 27, 31, 30, WATER)                           # the swamp
for x in range(0, 32, 3):
    hut[27][x] = WATER_LIGHT
rect(3, 30, 8, 30, MOSS)
rect(21, 30, 29, 30, MOSS)
for x in (5, 13, 18, 26):                            # oak log stilts
    rect(x, 20, x + 1, 28, OAK)
    rect(x, 20, x, 28, OAK_LIGHT)
    rect(x + 1, 20, x + 1, 28, OAK_DARK)
rect(3, 19, 29, 20, OAK)                             # the floor
rect(3, 19, 29, 19, OAK_LIGHT)
rect(5, 10, 27, 18, WALL)                            # spruce plank walls
for y in (12, 14, 16, 18):
    rect(5, y, 27, y, WALL_DARK)
for x in (4, 27):                                    # oak corner posts
    rect(x, 9, x + 1, 19, OAK)
    rect(x, 9, x, 19, OAK_LIGHT)
    rect(x + 1, 9, x + 1, 19, OAK_DARK)
for x0 in (8, 20):                                   # windows
    rect(x0, 13, x0 + 3, 16, GLASS)
    rect(x0, 13, x0 + 3, 13, 0x33414f)
rect(14, 12, 17, 18, DOOR)                           # door
rect(14, 12, 17, 12, 0x7a4f26)
hut[15][16] = 0xd8b040
for k in range(7):                                   # the stair roof, a step in on each side every row
    y = 9 - k
    x0, x1 = 1 + 2 * k, 30 - 2 * k
    for x in range(x0, x1 + 1):
        hut[y][x] = ROOF_LIGHT if y % 2 == 1 and x % 4 < 2 else ROOF
    rect(x0, y, min(x0 + 1, x1), y, ROOF_LIGHT)
    rect(max(x1 - 1, x0), y, x1, y, ROOF_DARK)
rect(1, 10, 30, 10, ROOF_DARK)                       # the roof's underside

OUT = 0x120d09


def outline(g, colour=OUT):
    """A one-pixel outline round everything drawn."""
    h, w = len(g), len(g[0])
    solid = [[g[y][x] is not None for x in range(w)] for y in range(h)]
    for y in range(h):
        for x in range(w):
            if not solid[y][x] and any(0 <= y + dy < h and 0 <= x + dx < w and solid[y + dy][x + dx]
                                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                g[y][x] = colour


GLYPHS = {
    "L": ("X....", "X....", "X....", "X....", "X....", "X....", "XXXXX"),
    "O": (".XXX.", "X...X", "X...X", "X...X", "X...X", "X...X", ".XXX."),
    "W": ("X...X", "X...X", "X...X", "X.X.X", "X.X.X", "XX.XX", "X...X"),
    "I": ("XXX", ".X.", ".X.", ".X.", ".X.", ".X.", "XXX"),
    "T": ("XXXXX", "..X..", "..X..", "..X..", "..X..", "..X..", "..X.."),
    "C": (".XXXX", "X....", "X....", "X....", "X....", "X....", ".XXXX"),
    "H": ("X...X", "X...X", "X...X", "XXXXX", "X...X", "X...X", "X...X"),
    "F": ("XXXXX", "X....", "X....", "XXXX.", "X....", "X....", "X...."),
    "N": ("X...X", "XX..X", "XX..X", "X.X.X", "X..XX", "X..XX", "X...X"),
    "D": ("XXXX.", "X...X", "X...X", "X...X", "X...X", "X...X", "XXXX."),
    "E": ("XXXXX", "X....", "X....", "XXXX.", "X....", "X....", "XXXXX"),
    "R": ("XXXX.", "X...X", "X...X", "XXXX.", "X.X..", "X..X.", "X...X"),
}
STONE, STONE_LIGHT, STONE_DARK, EDGE = 0x9d9d9d, 0xd2d2d2, 0x666666, 0x303030
CW = 112                                             # the logo is 112 art pixels wide
F = 2                                                # a letter's pixel is F x F art pixels, so strokes are thick enough to shade


def letters(lines, y0):
    """The set of lit pixels for each centred line of text starting at row y0 (letters one font pixel apart, words three)."""
    m = set()
    y = y0
    for line in lines:
        glyphs = [None if c == " " else GLYPHS[c] for c in line]
        width = (sum(3 if g is None else len(g[0]) for g in glyphs) + len(glyphs) - 1) * F
        x = (CW - width) // 2
        for g in glyphs:
            if g is None:
                x += (3 + 1) * F
                continue
            for j, row in enumerate(g):
                for i, v in enumerate(row):
                    if v == "X":
                        for a in range(F):
                            for b in range(F):
                                m.add((x + i * F + a, y + j * F + b))
            x += (len(g[0]) + 1) * F
        y += (7 + 2) * F
    return m


def logo():
    top = HH + 2
    rows = top + 2 * (7 + 2) * F + 2
    g = [[None] * CW for _ in range(rows)]
    for y in range(HH):
        for x in range(HW):
            g[y][x + (CW - HW) // 2] = hut[y][x]
    m = letters(("LOW WITCH", "FINDER"), top)
    for (x, y) in m:                                 # the block's side: dark, one pixel down and right
        if (x + 1, y + 1) not in m and g[y + 1][x + 1] is None:
            g[y + 1][x + 1] = EDGE
    for (x, y) in m:                                 # the face: lit on top, shaded below, like a stone block
        if (x, y - 1) not in m:
            g[y][x] = STONE_LIGHT
        elif (x, y + 1) not in m or (x + 1, y) not in m:
            g[y][x] = STONE_DARK
        else:
            g[y][x] = STONE
    for (x, y) in m:                                 # a few darker specks, so it reads as stone
        if (x * 7 + y * 13) % 11 == 0 and g[y][x] == STONE:
            g[y][x] = 0x8b8b8b
    outline(g)
    return g


def to_rows(g, scale):
    px = [[None if c is None else ((c >> 16) & 255, (c >> 8) & 255, c & 255, 255) for c in row] for row in g]
    return [[px[j // scale][i // scale] for i in range(len(g[0]) * scale)] for j in range(len(g) * scale)], len(g[0]) * scale, len(g) * scale


def png_bytes(rows, w, h):
    raw = b"".join(b"\0" + b"".join(bytes(p) if p else b"\0\0\0\0" for p in row) for row in rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


if __name__ == "__main__":
    os.makedirs(os.path.join(HERE, "..", "assets"), exist_ok=True)
    rows, w, h = to_rows(logo(), 8)
    open(os.path.join(HERE, "..", "assets", "logo.png"), "wb").write(png_bytes(rows, w, h))
    hg = [row[:] for row in hut]
    outline(hg)
    rows, w, h = to_rows(hg, 8)                      # 256 x 256 for the icon: a PNG inside an .ico
    data = png_bytes(rows, w, h)
    open(os.path.join(HERE, "..", "assets", "icon.ico"), "wb").write(
        struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(data), 22) + data)
    print("assets/logo.png and assets/icon.ico")
