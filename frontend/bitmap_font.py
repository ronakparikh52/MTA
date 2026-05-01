"""
bitmap_font.py
--------------
Hand-crafted 5x7 monospace bitmap font, public domain.

Why a custom font: pygame's SysFont rasterises a vector font (Menlo etc.)
down to 8 px, which leaves stair-stepping and odd kerning at LED-matrix
sizes. A real bitmap font is *designed* for this exact pixel grid, so what
the simulator draws is exactly what the LEDs will light. The same dict
ports straight to CircuitPython (`for row in glyph: for col in range(W):
if row & (1 << (W-1-col)): bitmap[x+col, y+row_i] = idx`) — no font file
parsing on-device, no `adafruit_bitmap_font` dependency.

Glyph encoding:
  * 7 rows × 5 cols, MSB = leftmost pixel.
  * `0b10000` lights the leftmost column, `0b00001` the rightmost.
  * Lowercase glyphs leave rows 0-1 blank (so the x-height baseline aligns
    with capitals); ascenders (b, d, t) and descenders (p, q, g, …) extend
    into those rows.

Coverage: just the chars this project actually renders — line letters, bus
route numerals, direction words ("Uptown", "Downtown", "Eastbound",
"Westbound"), digits, "m", "|", "/", and a few fallbacks. Adding glyphs is
trivial; missing chars draw a "?".
"""

# `from __future__ import annotations` makes all type hints strings that are
# never evaluated at runtime — so the `Callable[...]` references below don't
# need `typing` (which CircuitPython doesn't ship). CPython type checkers
# still understand them; CircuitPython just stores them as strings.
from __future__ import annotations

GLYPH_W = 5
GLYPH_H = 7
ADVANCE = GLYPH_W + 1   # 1 px gap between adjacent characters
LINE_H = GLYPH_H

_LEFTMOST_BIT = 0b10000


GLYPHS: dict[str, tuple[int, ...]] = {
    ' ': (0, 0, 0, 0, 0, 0, 0),

    # Punctuation / separators
    '|': (0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100),
    '/': (0b00001, 0b00010, 0b00010, 0b00100, 0b01000, 0b01000, 0b10000),
    '?': (0b01110, 0b10001, 0b00001, 0b00110, 0b00100, 0b00000, 0b00100),
    ':': (0,       0,       0b00100, 0,       0b00100, 0,       0),
    '.': (0,       0,       0,       0,       0,       0b00100, 0b00100),
    '-': (0,       0,       0,       0b01110, 0,       0,       0),

    # Digits
    '0': (0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110),
    '1': (0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    '2': (0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b01000, 0b11111),
    '3': (0b11110, 0b00001, 0b00001, 0b01110, 0b00001, 0b00001, 0b11110),
    '4': (0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010),
    '5': (0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110),
    '6': (0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110),
    '7': (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000),
    '8': (0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110),
    '9': (0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100),

    # Uppercase used in line letters and word starts
    'A': (0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    'B': (0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110),
    'C': (0b01111, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b01111),
    'D': (0b11110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11110),
    'E': (0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111),
    'F': (0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000),
    'G': (0b01111, 0b10000, 0b10000, 0b10011, 0b10001, 0b10001, 0b01111),
    'J': (0b00111, 0b00010, 0b00010, 0b00010, 0b00010, 0b10010, 0b01100),
    'L': (0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111),
    'M': (0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001),
    'N': (0b10001, 0b11001, 0b11001, 0b10101, 0b10011, 0b10011, 0b10001),
    'Q': (0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101),
    'R': (0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001),
    'S': (0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110),
    'T': (0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100),
    'U': (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    'W': (0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b11011, 0b10001),

    # Lowercase used in direction words: Uptown/Downtown/Eastbound/Westbound,
    # plus "no data".
    'a': (0,       0,       0b01110, 0b00001, 0b01111, 0b10001, 0b01111),
    'b': (0b10000, 0b10000, 0b10110, 0b11001, 0b10001, 0b10001, 0b11110),
    'c': (0,       0,       0b01110, 0b10001, 0b10000, 0b10001, 0b01110),
    'd': (0b00001, 0b00001, 0b01101, 0b10011, 0b10001, 0b10001, 0b01111),
    'e': (0,       0,       0b01110, 0b10001, 0b11111, 0b10000, 0b01111),
    'i': (0b00100, 0,       0b01100, 0b00100, 0b00100, 0b00100, 0b01110),
    'm': (0,       0,       0b11010, 0b10101, 0b10101, 0b10001, 0b10001),
    'n': (0,       0,       0b10110, 0b11001, 0b10001, 0b10001, 0b10001),
    'o': (0,       0,       0b01110, 0b10001, 0b10001, 0b10001, 0b01110),
    # 'p' has a descender in row 6, so its body sits in rows 2-4.
    'p': (0,       0,       0b11110, 0b10001, 0b11110, 0b10000, 0b10000),
    'r': (0,       0,       0b10110, 0b11001, 0b10000, 0b10000, 0b10000),
    's': (0,       0,       0b01111, 0b10000, 0b01110, 0b00001, 0b11110),
    't': (0b00100, 0b00100, 0b01110, 0b00100, 0b00100, 0b00100, 0b00011),
    'u': (0,       0,       0b10001, 0b10001, 0b10001, 0b10011, 0b01101),
    'w': (0,       0,       0b10001, 0b10001, 0b10101, 0b11011, 0b10001),
    'y': (0,       0,       0b10001, 0b10001, 0b01111, 0b00001, 0b11110),
}


def _glyph_for(ch: str) -> tuple[int, ...]:
    return GLYPHS.get(ch) or GLYPHS.get(ch.upper()) or GLYPHS['?']


def text_size(text: str) -> tuple[int, int]:
    """Width includes glyphs + gaps between them but no trailing gap."""
    if not text:
        return (0, 0)
    return (len(text) * ADVANCE - 1, GLYPH_H)


def draw(
    set_pixel: Callable[[int, int, tuple[int, int, int]], None],
    x: int,
    y: int,
    text: str,
    color: tuple[int, int, int],
) -> None:
    """Draw `text` with its top-left at (x, y). `set_pixel` does the work,
    so the same routine works for pygame surfaces or displayio.Bitmaps."""
    for ch in text:
        glyph = _glyph_for(ch)
        for row_i, row_bits in enumerate(glyph):
            if row_bits == 0:
                continue
            for col_i in range(GLYPH_W):
                if row_bits & (_LEFTMOST_BIT >> col_i):
                    set_pixel(x + col_i, y + row_i, color)
        x += ADVANCE


def visual_bbox(text: str) -> tuple[int, int, int, int]:
    """Return (x, y, w, h) of the lit-pixel bounding box for `text`. Used
    for badge centering — capital letters fill rows 0-6, lowercase only fill
    2-6, descenders push to row 6, and we want the body to sit at the
    geometric middle of the badge, not the font's nominal middle."""
    if not text:
        return (0, 0, 0, 0)
    min_x, min_y = 10**6, 10**6
    max_x, max_y = -1, -1
    for i, ch in enumerate(text):
        glyph = _glyph_for(ch)
        for row_i, row_bits in enumerate(glyph):
            if row_bits == 0:
                continue
            min_y = min(min_y, row_i)
            max_y = max(max_y, row_i)
            for col_i in range(GLYPH_W):
                if row_bits & (_LEFTMOST_BIT >> col_i):
                    abs_x = i * ADVANCE + col_i
                    min_x = min(min_x, abs_x)
                    max_x = max(max_x, abs_x)
    if max_y < 0:
        return (0, 0, 0, 0)
    return (min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)


def draw_centered(
    set_pixel: Callable[[int, int, tuple[int, int, int]], None],
    cx: int,
    cy: int,
    text: str,
    color: tuple[int, int, int],
) -> None:
    """Place `text` so its visible-pixel bbox is centered on (cx, cy)."""
    bx, by, bw, bh = visual_bbox(text)
    base_x = cx - bx - bw // 2
    base_y = cy - by - bh // 2
    draw(set_pixel, base_x, base_y, text, color)
