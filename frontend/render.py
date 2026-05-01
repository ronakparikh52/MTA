"""
render.py
---------
Shared rendering primitives used by every layout in `layouts/` and by both
targets (the desktop pygame simulator and CircuitPython on the Matrix Portal).

Layouts speak ONLY to the `Framebuffer` interface defined here, so they're
portable: the desktop sim implements `Framebuffer` with pygame, the device
will implement it with `displayio`.
"""

from __future__ import annotations

PANEL_W = 128
PANEL_H = 32


# Approximate official subway colors (RGB 0-255). NYC city buses use a
# uniform "bus blue" badge so they're visually distinct from trains.
LINE_COLORS: dict[str, tuple[int, int, int]] = {
    # IND 8 Av
    "A": (0, 57, 166), "C": (0, 57, 166), "E": (0, 57, 166),
    # IND 6 Av
    "B": (255, 99, 27), "D": (255, 99, 27), "F": (255, 99, 27), "M": (255, 99, 27),
    # BMT Broadway
    "N": (252, 204, 10), "Q": (252, 204, 10), "R": (252, 204, 10), "W": (252, 204, 10),
    "NRW": (252, 204, 10),
    # IRT 7 Av
    "1": (238, 53, 63), "2": (238, 53, 63), "3": (238, 53, 63),
    # IRT Lex
    "4": (0, 147, 69), "5": (0, 147, 69), "6": (0, 147, 69),
    # IRT Flushing
    "7": (116, 44, 148),
    # Misc
    "G": (108, 190, 69),
    "L": (167, 169, 172), "S": (128, 128, 128),
    # Manhattan locals
    "M11": (0, 116, 217), "M20": (0, 116, 217), "M42": (0, 116, 217),
    "M50": (0, 116, 217), "M104": (0, 116, 217), "M7":  (0, 116, 217),
}
DEFAULT_LINE_COLOR = (180, 180, 180)


def line_color(line: str) -> tuple[int, int, int]:
    return LINE_COLORS.get(line.upper(), DEFAULT_LINE_COLOR)


def text_color_for_badge(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Black on light badges, white on dark ones — for legible labels."""
    brightness = sum(rgb) / 3
    return (0, 0, 0) if brightness > 128 else (255, 255, 255)


def all_rows(payload: dict) -> list[dict]:
    """Combine trains + buses into one list, sorted by minutes_away ascending."""
    rows: list[dict] = list(payload.get("trains", [])) + list(payload.get("buses", []))
    rows.sort(key=lambda r: r.get("min", 999))
    return rows


def page_chunk(rows: list[dict], page_index: int, per_page: int) -> tuple[list[dict], int]:
    """Return (chunk_for_this_page, total_pages). Wraps if page_index overflows."""
    if not rows:
        return [], 1
    total = max(1, (len(rows) + per_page - 1) // per_page)
    page = page_index % total
    return rows[page * per_page:(page + 1) * per_page], total


# Sign-style line groups. Each page on the matrix shows ONE group, where a
# "group" is a (related-lines, single-direction) pair. So each page only has
# trains/buses going the same way, and lines that "go together" (ACE trio,
# NRW yellow trio, 1/2 numbered pair, geographically-paired bus routes) are
# rendered side-by-side.
#
#   - rows == 3  -> trio layout (3 stacked rows, smaller badges)
#   - rows == 2  -> duo  layout (2 stacked rows, big badges) [the format
#                                you said is "already perfect"]
#
# Bus routes are split geographically into duos so we never need a >3-row
# layout for the 4 avenue buses in the configured area.
GROUPS: list[dict] = [
    # 3-line subway trios (yellow + blue)
    {"name": "NRW Uptown",   "lines": ["N", "R", "W"], "dirs": {"U"}, "rows": 3},
    {"name": "NRW Downtown", "lines": ["N", "R", "W"], "dirs": {"D"}, "rows": 3},
    {"name": "ACE Uptown",   "lines": ["A", "C", "E"], "dirs": {"U"}, "rows": 3},
    {"name": "ACE Downtown", "lines": ["A", "C", "E"], "dirs": {"D"}, "rows": 3},

    # 2-line duos for the rest of the trains
    {"name": "Q Express",    "lines": ["Q"],     "dirs": {"U", "D"},  "rows": 2},
    {"name": "1/2 Uptown",   "lines": ["1", "2"], "dirs": {"U"},      "rows": 2},
    {"name": "1/2 Downtown", "lines": ["1", "2"], "dirs": {"D"},      "rows": 2},

    # Buses, paired by avenue corridor. M11/M20 share the 8-10 Av "west"
    # corridor; M104/M7 share the Broadway / 6 Av "east" corridor; M42/M50
    # are the two crosstowns. Adjust the lines/dirs to match your stops.
    {"name": "West Ave Uptown",    "lines": ["M11", "M20"], "dirs": {"Uptown"},   "rows": 2},
    {"name": "West Ave Downtown",  "lines": ["M11", "M20"], "dirs": {"Downtown"}, "rows": 2},
    {"name": "East Ave Uptown",    "lines": ["M104", "M7"], "dirs": {"Uptown"},   "rows": 2},
    {"name": "East Ave Downtown",  "lines": ["M104", "M7"], "dirs": {"Downtown"}, "rows": 2},
    {"name": "Crosstown East",     "lines": ["M42", "M50"], "dirs": {"Eastbound"}, "rows": 2},
    {"name": "Crosstown West",     "lines": ["M42", "M50"], "dirs": {"Westbound"}, "rows": 2},
]


# Shorthand → full word so the badge column always tells you where the train
# is going. Bus directions already arrive as full words ("Eastbound" etc.).
DIR_WORDS = {"U": "Uptown", "D": "Downtown"}


# Time-to-arrival colour code. Tuned so the matrix mirrors how someone would
# read the sign at a glance: green = leave now, yellow = hustle, red = chill.
TIME_GREEN = (0, 220, 80)
TIME_YELLOW = (255, 200, 0)
TIME_RED = (240, 60, 60)
TIME_GRAY = (170, 170, 170)


def time_color(minutes) -> tuple[int, int, int]:
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return TIME_GRAY
    if m <= 8:
        return TIME_GREEN
    if m <= 15:
        return TIME_YELLOW
    return TIME_RED


# --------------------------------------------------------------------------- #
# Split-screen variant: each page treats the panel as two 64x32 halves and
# shows BOTH directions of one line group at the same time. Trades the full
# direction word for a single letter (U/D/E/W) and tighter time format
# ("5|12" instead of "5m | 12m") to make everything fit per side.
# --------------------------------------------------------------------------- #
SPLIT_GROUPS: list[dict] = [
    # 3-line trios (left = uptown, right = downtown)
    {"name": "ACE", "lines": ["A", "C", "E"], "left": "U", "right": "D", "rows": 3},
    {"name": "NRW", "lines": ["N", "R", "W"], "left": "U", "right": "D", "rows": 3},

    # 2-line duos
    {"name": "1/2", "lines": ["1", "2"], "left": "U", "right": "D", "rows": 2},
    {"name": "Q",   "lines": ["Q"],     "left": "U", "right": "D", "rows": 2},

    # Buses (full-word direction strings to match payload, single letter is
    # derived at render time by taking the first character)
    {"name": "West Ave",  "lines": ["M11", "M20"], "left": "Uptown",    "right": "Downtown",  "rows": 2},
    {"name": "East Ave",  "lines": ["M104", "M7"], "left": "Uptown",    "right": "Downtown",  "rows": 2},
    {"name": "Crosstown", "lines": ["M42", "M50"], "left": "Eastbound", "right": "Westbound", "rows": 2},
]


def build_split_pages(payload: dict) -> list[tuple[dict, list[dict], list[dict]]]:
    """For each SPLIT_GROUP, return (group, left_rows, right_rows). Skip
    pages where both halves are empty so the cycle never shows blanks."""
    rows = list(payload.get("trains", [])) + list(payload.get("buses", []))
    pages: list[tuple[dict, list[dict], list[dict]]] = []
    for group in SPLIT_GROUPS:
        line_order = {ln: i for i, ln in enumerate(group["lines"])}
        left = [r for r in rows if r.get("line") in line_order and r.get("dir") == group["left"]]
        right = [r for r in rows if r.get("line") in line_order and r.get("dir") == group["right"]]
        if not left and not right:
            continue
        left.sort(key=lambda r: line_order[r["line"]])
        right.sort(key=lambda r: line_order[r["line"]])
        pages.append((group, left[: group["rows"]], right[: group["rows"]]))
    return pages


def build_group_pages(payload: dict) -> list[tuple[dict, list[dict]]]:
    """Walk GROUPS in order, return a list of (group_dict, matching_rows)
    pages. Each page corresponds to one entry in GROUPS — no sub-pagination,
    because every group is sized to fit on one screen (2 or 3 rows). Empty
    groups (no arrivals right now) are skipped so the cycle never shows a
    blank screen.

    Within a page, rows are sorted by the order the line appears in
    `group["lines"]` (so "ACE Uptown" always shows A, then C, then E — the
    same scan order every cycle, easier on the eyes than re-sorting by
    minutes which moves rows around)."""
    rows = list(payload.get("trains", [])) + list(payload.get("buses", []))
    pages: list[tuple[dict, list[dict]]] = []
    for group in GROUPS:
        line_order = {ln: i for i, ln in enumerate(group["lines"])}
        dirs = group["dirs"]
        matched = [
            r for r in rows
            if r.get("line") in line_order and r.get("dir") in dirs
        ]
        if not matched:
            continue
        matched.sort(key=lambda r: (line_order[r["line"]], r.get("min", 999)))
        # Cap at the group's row count — for the 4 avenue buses we already
        # split geographically so this is a safety guard, not a chopper.
        pages.append((group, matched[: group["rows"]]))
    return pages


class Framebuffer:
    """Minimal abstract interface every layout calls.

    Concrete implementations:
      - simulator.py  → pygame.Surface
      - code.py       → displayio.Bitmap (TODO when hardware arrives)
    """
    width: int = PANEL_W
    height: int = PANEL_H

    def fill(self, rgb: tuple[int, int, int]) -> None: raise NotImplementedError
    def set_pixel(self, x: int, y: int, rgb: tuple[int, int, int]) -> None: raise NotImplementedError
    def fill_rect(self, x: int, y: int, w: int, h: int, rgb: tuple[int, int, int]) -> None: raise NotImplementedError
    def fill_circle(self, cx: int, cy: int, r: int, rgb: tuple[int, int, int]) -> None: raise NotImplementedError
    def text_size(self, text: str, font: str = "small") -> tuple[int, int]: raise NotImplementedError
    def draw_text(self, x: int, y: int, text: str, rgb: tuple[int, int, int], font: str = "small") -> None: raise NotImplementedError
    def draw_text_centered(
        self, cx: int, cy: int, text: str, rgb: tuple[int, int, int], font: str = "small"
    ) -> None:
        """Center `text` on (cx, cy) using its actual visible-pixel bbox.

        Necessary for badge labels — using the font's nominal width/height
        leaves whitespace around descenders and right-side bearings, which
        looks badly off-center for letters like ``Q`` and ``W``.
        """
        raise NotImplementedError
