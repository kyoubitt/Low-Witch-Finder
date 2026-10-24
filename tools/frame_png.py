#!/usr/bin/env python3
"""frame_png.py W H TAB THEME OUT.png - the app's frame as a picture, to judge colours and layout without a terminal.
Each cell is 8x16 px: its background, the upper-half block as a coloured half, and any other glyph as a small block in its
colour (the shapes of letters aren't drawn, only where the ink is)."""
import struct
import sys
import zlib
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
import witchhut_studio as A  # noqa: E402


def png(path, w, h, rgb):
    raw = b"".join(b"\0" + bytes(rgb[y * w * 3:(y + 1) * w * 3]) for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                           chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def main():
    w, h, tab, theme, out = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]) - 1, sys.argv[4], sys.argv[5]
    app, s = A.frame(w, h, tab, theme)
    cw, chh = 8, 16
    W_, H_ = w * cw, h * chh
    buf = bytearray(W_ * H_ * 3)
    for y in range(h):
        for x in range(w):
            i = y * w + x
            c, fg, bg = s.ch[i], s.fg[i], s.bg[i]
            for part in range(2):
                col = bg
                if c == "▀" and part == 0:
                    col = fg
                rgb = bytes((col >> 16 & 255, col >> 8 & 255, col & 255)) if col >= 0 else bytes((0, 0, 0))
                for yy in range(part * 8, part * 8 + 8):
                    off = ((y * chh + yy) * W_ + x * cw) * 3
                    buf[off:off + cw * 3] = rgb * cw
            if c not in (" ", "", "▀"):
                rgb = bytes((fg >> 16 & 255, fg >> 8 & 255, fg & 255))
                for yy in range(4, 12):
                    off = ((y * chh + yy) * W_ + x * cw + 2) * 3
                    buf[off:off + 4 * 3] = rgb * 4
    png(out, W_, H_, buf)


main()
