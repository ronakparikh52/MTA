"""
Split-screen layout
-------------------
Treats the 128x32 panel as two 64x32 halves. Each page shows ONE line
group, with the LEFT half = one direction (uptown / east), RIGHT half =
the opposite direction (downtown / west). Twice as much data visible
compared to the single-canvas layout, at the cost of:

  * Direction word becomes a single letter (`U`/`D`/`E`/`W`) — position
    on the panel implies the rest.
  * Times use the tight format `5|12` (no `m` suffix, no spaces around
    the separator) so trains *and* M104-width pills both fit per side.
  * A faint vertical divider at column 63 visually separates the halves.

Per-side budget at 64 px wide (proven to fit every line/route in the
configured groups):
    badge (10-27 px) + 2 + dir (5 px) + 2 + times "5|12" (~23 px)
                                            "10|22" (~29 px) worst case

Same `Framebuffer` interface as `big_colorful` — runs identically on the
desktop simulator and on the Matrix Portal S3.
"""

from __future__ import annotations

from render import (
    Framebuffer,
    build_group_pages,
    build_split_pages,
    line_color,
    text_color_for_badge,
    time_color,
)
from time_focus import COMMUTE_BOARD_STATIC, commute_static_rows


# Vertical layout (mirrors big_colorful so muscle memory carries over)
DUO_ROW_H = 16
DUO_BADGE_H = 14
TRIO_ROW_TOPS = (0, 11, 22)
TRIO_BADGE_H = 10
# Solo vertically centres one row inside a half-panel when only one commuter
# line arrives in JSON.
SOLO_BADGE_H = 14
SOLO_ROW_TOP = (32 - SOLO_BADGE_H) // 2

# Horizontal split: a single 1-px column at x=63 separates the halves.
LEFT_X0, LEFT_X1 = 0, 62        # 63 px wide
DIVIDER_X = 63
RIGHT_X0, RIGHT_X1 = 64, 127    # 64 px wide

NAME_COLOR = (240, 240, 240)
SEP_COLOR = (170, 170, 170)
DIVIDER_COLOR = (40, 40, 50)
EMPTY_COLOR = (255, 80, 80)


def _short_dir(row: dict) -> str:
    """`U`/`D` for trains (already 1 char), or first letter of bus dir
    word — `Uptown` -> `U`, `Eastbound` -> `E`, etc."""
    d = str(row.get("dir", "?"))
    return d if len(d) == 1 else d[0]


def _draw_times_tight(
    fb: Framebuffer, x_right: int, y_top: int, badge_h: int, row: dict
) -> int:
    """Draw the time block right-aligned with the tight `5|12` format.
    Each minute coloured independently (green/yellow/red); the `|` stays
    gray. Returns the leftmost X used by the times block so the caller
    can place the direction letter without overlap."""
    m = row.get("min")
    n = row.get("next")

    parts: list[tuple[str, tuple[int, int, int]]] = []
    if m is None or m == "?":
        parts.append(("--", SEP_COLOR))
    else:
        parts.append((str(m), time_color(m)))
        if n is not None:
            parts.append(("|", SEP_COLOR))
            parts.append((str(n), time_color(n)))

    total_w = sum(fb.text_size(t)[0] for t, _ in parts)
    _, h = fb.text_size(parts[0][0])
    x = x_right - total_w + 1
    y = y_top + (badge_h - h) // 2
    for text, color in parts:
        fb.draw_text(x, y, text, color)
        x += fb.text_size(text)[0]
    return x_right - total_w + 1


def _draw_split_row(
    fb: Framebuffer,
    x_left: int,
    x_right: int,
    y_top: int,
    badge_h: int,
    row: dict,
) -> None:
    """Render one row inside a 64-wide half panel: `[badge] D 5|12`."""
    line = str(row.get("line", "?"))
    color = line_color(line)
    is_pill = len(line) > 1
    text_w, _ = fb.text_size(line)

    if is_pill:
        badge_w = text_w + 4
        fb.fill_rect(x_left, y_top, badge_w, badge_h, color)
    else:
        # Square footprint for circle badges.
        badge_w = badge_h
        fb.fill_circle(
            x_left + badge_w // 2, y_top + badge_h // 2, badge_h // 2, color
        )

    fb.draw_text_centered(
        x_left + badge_w // 2,
        y_top + badge_h // 2,
        line,
        text_color_for_badge(color),
    )

    times_left = _draw_times_tight(fb, x_right, y_top, badge_h, row)

    # Direction letter sits between the badge and the times. If the row
    # is so cramped it would overlap the times (only happens on M104 +
    # double-digit "10|22"-style minutes), drop the direction letter and
    # let position imply it — left half = uptown/east anyway.
    dir_letter = _short_dir(row)
    dir_w, dir_h = fb.text_size(dir_letter)
    dir_x = x_left + badge_w + 2
    if dir_x + dir_w + 1 <= times_left:
        dir_y = y_top + (badge_h - dir_h) // 2
        fb.draw_text(dir_x, dir_y, dir_letter, NAME_COLOR)


def _draw_divider(fb: Framebuffer) -> None:
    for y in range(fb.height):
        fb.set_pixel(DIVIDER_X, y, DIVIDER_COLOR)


def _render_half(fb, x0, x1, rows, group_rows_count):
    """Draw the rows of one half panel, picking trio / duo / solo geometry
    based on the requested row count."""
    if group_rows_count == 3:
        for top, row in zip(TRIO_ROW_TOPS, rows[:3]):
            _draw_split_row(fb, x0, x1, top, TRIO_BADGE_H, row)
    elif group_rows_count == 1 and rows:
        _draw_split_row(fb, x0, x1, SOLO_ROW_TOP, SOLO_BADGE_H, rows[0])
    else:
        for i, row in enumerate(rows[:2]):
            _draw_split_row(fb, x0, x1, i * DUO_ROW_H, DUO_BADGE_H, row)


def render(
    fb: Framebuffer,
    payload: dict,
    page_index: int,
    anim_state: dict | None = None,
    dt: float = 0.0,
    commute_board: str | None = None,
) -> None:
    """Render one split-screen page.

    `commute_board`:
      * None               -> LEFT/RIGHT opposing directions per group page
      * `COMMUTE_BOARD_STATIC`
                        -> LEFT = static NRW + E + M50 (solo/duo/trio by count)
                           RIGHT = same regular group carousel as usual,
                           driven solely by `page_index`.

    See `COMMUTE_BOARD_STATIC` in `time_focus.py`.
    """
    fb.fill((0, 0, 0))

    if commute_board == COMMUTE_BOARD_STATIC:
        _draw_divider(fb)
        lr = commute_static_rows(payload)
        n = len(lr)
        if n >= 3:
            lc = 3
        elif n == 2:
            lc = 2
        elif n == 1:
            lc = 1
        else:
            lc = 2
        _render_half(fb, LEFT_X0, LEFT_X1, lr, lc)
        right_pages = build_group_pages(payload)
        if right_pages:
            right_group, right_rows = right_pages[page_index % len(right_pages)]
            _render_half(
                fb,
                RIGHT_X0,
                RIGHT_X1,
                right_rows,
                right_group.get("rows", 2),
            )
        return

    pages = build_split_pages(payload)
    if not pages:
        fb.draw_text(2, 12, "no data", EMPTY_COLOR)
        return

    group, left_rows, right_rows = pages[page_index % len(pages)]
    _draw_divider(fb)
    rc = group.get("rows", 2)
    _render_half(fb, LEFT_X0, LEFT_X1, left_rows, rc)
    _render_half(fb, RIGHT_X0, RIGHT_X1, right_rows, rc)
