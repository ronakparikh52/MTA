"""
Subway-disc layout
------------------
Two visual formats, picked automatically based on the group:

  * DUO  (2 rows × 16 px) — for groups with 2 lines/directions, e.g.
    "1/2 Uptown", "Q Express", "Crosstown East". Big 14 px badges, plenty
    of breathing room.

  * TRIO (3 rows × 10 px) — for groups with 3 lines, e.g. "ACE Uptown",
    "NRW Downtown". Smaller 9 px badges; the same direction word appears
    on every row (slightly redundant, but keeps each row self-contained).

Per-row layout in BOTH formats:

    [● A] Uptown                  5m | 12m

    - Single-letter line  -> round colored disc
    - Multi-letter route  -> rectangular pill
    - Direction word in white, color-coded times right-aligned
    - Each minute coloured by urgency: green <=8, yellow 9-15, red >15

Layouts call only `Framebuffer` methods, so the desktop simulator and the
Matrix Portal render identically once `code.py` is built.
"""

from __future__ import annotations

from render import (
    DIR_WORDS,
    Framebuffer,
    build_group_pages,
    line_color,
    text_color_for_badge,
    time_color,
)
from time_focus import COMMUTE_BOARD_STATIC, commute_static_rows


# Duo (2 rows × 16 px) — the format you said is "already perfect"
DUO_ROW_H = 16
DUO_BADGE_H = 14   # 1 px slack top + bottom

# Trio (3 rows × ~11 px) — fits 3 lines on the 32 px panel with 1 px gaps
# between rows. 10 px badges leave more room for the letter inside than 9 px,
# which matters a lot for low-contrast pairs like black-on-yellow (NRWQ).
TRIO_ROW_TOPS = (0, 11, 22)
TRIO_BADGE_H = 10

# Solo / duo / trio heights are shared with the regular group renderer.

NAME_COLOR = (240, 240, 240)
SEP_COLOR = (170, 170, 170)
EMPTY_COLOR = (255, 80, 80)


def _direction_label(row: dict) -> str:
    """Trains arrive with `dir` = 'U'/'D'; buses arrive with full words.
    Return the friendliest label that fits the middle column."""
    d = str(row.get("dir", "?"))
    return DIR_WORDS.get(d, d)


def _draw_times(fb: Framebuffer, x_right: int, y_top: int, badge_h: int, row: dict) -> int:
    """Right-align the time block, colouring each minute number independently.

    Returns the leftmost X the times occupy so the caller knows how much room
    is left for the direction label."""
    m = row.get("min")
    n = row.get("next")

    parts: list[tuple[str, tuple[int, int, int]]] = []
    if m is None or m == "?":
        parts.append(("--", SEP_COLOR))
    else:
        parts.append((f"{m}m", time_color(m)))
        if n is not None:
            parts.append((" | ", SEP_COLOR))
            parts.append((f"{n}m", time_color(n)))

    total_w = sum(fb.text_size(t)[0] for t, _ in parts)
    _, time_h = fb.text_size(parts[0][0])
    x = x_right - total_w
    text_y = y_top + (badge_h - time_h) // 2
    for text, color in parts:
        fb.draw_text(x, text_y, text, color)
        x += fb.text_size(text)[0]
    return x_right - total_w


def _draw_row(fb: Framebuffer, y_top: int, badge_h: int, row: dict) -> None:
    """Single shared row renderer. The only thing that changes between duo
    and trio is `badge_h` (and the y_top spacing), which is exactly the
    knob both formats need."""
    line = str(row.get("line", "?"))
    color = line_color(line)
    is_pill = len(line) > 1
    text_w, _ = fb.text_size(line)

    if is_pill:
        badge_w = text_w + 4
        fb.fill_rect(0, y_top, badge_w, badge_h, color)
    else:
        # Square footprint for circle badges.
        badge_w = badge_h
        fb.fill_circle(badge_w // 2, y_top + badge_h // 2, badge_h // 2, color)

    fb.draw_text_centered(
        badge_w // 2,
        y_top + badge_h // 2,
        line,
        text_color_for_badge(color),
    )

    times_left = _draw_times(fb, fb.width - 1, y_top, badge_h, row)

    label = _direction_label(row)
    name_x = badge_w + 3
    name_avail = (times_left - 2) - name_x
    if name_avail < 6:
        return
    name_w, name_h = fb.text_size(label)
    while name_w > name_avail and len(label) > 1:
        label = label[:-1]
        name_w, _ = fb.text_size(label)
    name_y = y_top + (badge_h - name_h) // 2
    fb.draw_text(name_x, name_y, label, NAME_COLOR)


def _render_solo(fb: Framebuffer, row: dict) -> None:
    """Single row, vertically centred — fallback when JSON only has one
    commute line for the trio slot."""
    _row_top = (32 - DUO_BADGE_H) // 2
    _draw_row(fb, _row_top, DUO_BADGE_H, row)


def _render_duo(fb: Framebuffer, rows: list[dict]) -> None:
    for i, row in enumerate(rows[:2]):
        _draw_row(fb, i * DUO_ROW_H, DUO_BADGE_H, row)


def _render_trio(fb: Framebuffer, rows: list[dict]) -> None:
    for top, row in zip(TRIO_ROW_TOPS, rows[:3]):
        _draw_row(fb, top, TRIO_BADGE_H, row)


def _render_rows(fb: Framebuffer, rows: list[dict]) -> None:
    """Pick the right geometry (solo / duo / trio) based on row count."""
    n = len(rows)
    if n == 0:
        fb.draw_text(2, 12, "no data", EMPTY_COLOR)
    elif n == 1:
        _render_solo(fb, rows[0])
    elif n == 2:
        _render_duo(fb, rows)
    else:
        _render_trio(fb, rows)


def render(
    fb: Framebuffer,
    payload: dict,
    page_index: int,
    anim_state: dict | None = None,
    dt: float = 0.0,
    commute_board: str | None = None,
) -> None:
    """Render one page on the panel.

    `commute_board`:
      * None                    -> normal page cycle at `page_index`
      * `COMMUTE_BOARD_STATIC`  -> NRW uptown + E uptown + M50 east at once,
                                   static trio (solo/duo if some rows absent).
                                   Caller keeps this up while idle during the
                                   commute window; SPACE/button drops back to
                                   None so the rider can browse other pages.
    """
    fb.fill((0, 0, 0))

    if commute_board == COMMUTE_BOARD_STATIC:
        _render_rows(fb, commute_static_rows(payload))
        return

    pages = build_group_pages(payload)
    if not pages:
        fb.draw_text(2, 12, "no data", EMPTY_COLOR)
        return

    group, rows = pages[page_index % len(pages)]
    if group.get("rows", 2) == 3:
        _render_trio(fb, rows)
    else:
        _render_duo(fb, rows)
