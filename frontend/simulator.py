"""
simulator.py
------------
Desktop preview of the LED panel. Polls the same /matrix endpoint the
Matrix Portal will fetch, then renders each layout in `layouts/` to a fake
128×32 framebuffer scaled up in a pygame window.

Keys:
    SPACE  — advance to the next page right now (also temporarily exits
             the commute focus on the full layout so you can scroll
             through the regular pages — auto-reverts after ~30 s idle)
    C      — toggle commute-window override (for testing outside 7-12 ET)
    Q/ESC  — quit

Run:
    cd frontend
    pip install -r requirements.txt
    python simulator.py

    Preview commuter trio anytime (MATRIX_FORCE_COMMUTE=1 overrides the clock):
        MATRIX_FORCE_COMMUTE=1 python simulator.py

Configure via environment vars:
    MATRIX_BACKEND_URL       default http://127.0.0.1:5001/matrix
    MATRIX_POLL_SECONDS      default 10  (faster than 30 for dev iteration)
    MATRIX_PAGE_INTERVAL     default 10  (seconds per page, matches your spec)
    MATRIX_FORCE_COMMUTE=1    start in commuter window regardless of clock —
                             static NRW+E+M50 trio (full stays frozen while idle;
                             split left static, split right rotates as usual).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pygame
import requests

import bitmap_font
import time_focus
from render import PANEL_H, PANEL_W, Framebuffer
from layouts.big_colorful import render as render_big
from layouts.split import render as render_split


def _commute_override_from_env() -> bool | None:
    """If MATRIX_FORCE_COMMUTE is set to a truthy/falsey string, honour it."""
    raw = os.environ.get("MATRIX_FORCE_COMMUTE", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None

# Local timezone the commute rule is evaluated in. Hard-coded to NYC since
# the whole project is MTA-specific; change here if you ever relocate.
LOCAL_TZ = ZoneInfo("America/New_York")
# How long after the user presses SPACE during commute mode before the
# full layout snaps back to the stagnant commute screen.
COMMUTE_IDLE_RESET_S = 30.0


# Adding a layout = appending a tuple here. Removing one = deleting its file
# under `layouts/` and removing its import + tuple. Nothing else to change.
# Both layouts render at the same time so they can be compared visually;
# pick the winner later and remove the loser from this list.
LAYOUTS: list[tuple[str, callable]] = [
    ("1. Single canvas (current)", render_big),
    ("2. Split screen (left=Up/East, right=Down/West)", render_split),
]


BACKEND_URL = os.environ.get("MATRIX_BACKEND_URL", "http://127.0.0.1:5001/matrix")
POLL_SECONDS = float(os.environ.get("MATRIX_POLL_SECONDS", "10"))
PAGE_INTERVAL_S = float(os.environ.get("MATRIX_PAGE_INTERVAL", "10"))

PIXEL_SIZE = 8           # each LED rendered as PIXEL_SIZE × PIXEL_SIZE on screen
LABEL_HEIGHT = 24
GAP_HEIGHT = 8
STATUS_HEIGHT = 24

WIN_W = PANEL_W * PIXEL_SIZE
WIN_H = (
    sum(LABEL_HEIGHT + PANEL_H * PIXEL_SIZE for _ in LAYOUTS)
    + GAP_HEIGHT * (len(LAYOUTS) - 1)
    + STATUS_HEIGHT
)


class PygameFramebuffer(Framebuffer):
    """Implements the Framebuffer interface using a pygame.Surface (128×32)."""

    width = PANEL_W
    height = PANEL_H

    def __init__(self) -> None:
        self.surface = pygame.Surface((PANEL_W, PANEL_H))

    def fill(self, rgb): self.surface.fill(rgb)

    def set_pixel(self, x, y, rgb):
        if 0 <= x < self.width and 0 <= y < self.height:
            self.surface.set_at((x, y), rgb)

    def fill_rect(self, x, y, w, h, rgb):
        pygame.draw.rect(self.surface, rgb, (x, y, w, h))

    def fill_circle(self, cx, cy, r, rgb):
        # Drawing on a 128x32 grid: pygame.draw.circle anti-aliases the edge
        # which on real LEDs would just be ON-or-OFF. Render a clean circle
        # by walking the bounding box and lighting LEDs whose centers fall
        # inside the radius — same algorithm we'll use on displayio.
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                dx = x - cx
                dy = y - cy
                if dx * dx + dy * dy <= r * r:
                    self.set_pixel(x, y, rgb)

    def text_size(self, text, font="small"):
        # `font` is accepted for API compatibility but our bitmap font is
        # one fixed size; the on-device side will work the same way.
        return bitmap_font.text_size(text)

    def draw_text(self, x, y, text, rgb, font="small"):
        bitmap_font.draw(self.set_pixel, x, y, text, rgb)

    def draw_text_centered(self, cx, cy, text, rgb, font="small"):
        bitmap_font.draw_centered(self.set_pixel, cx, cy, text, rgb)


def _fetch_loop(state: dict, stop_event: threading.Event) -> None:
    """Background thread: hit the backend every POLL_SECONDS, stash the JSON."""
    while not stop_event.is_set():
        try:
            r = requests.get(BACKEND_URL, timeout=5)
            r.raise_for_status()
            state["payload"] = r.json()
            state["last_fetch"] = time.time()
            state["error"] = None
        except Exception as exc:
            state["error"] = str(exc)
        stop_event.wait(POLL_SECONDS)


def main() -> None:
    pygame.init()
    pygame.display.set_caption("MTA sign simulator (128×32)")
    screen = pygame.display.set_mode((WIN_W, WIN_H))
    label_font = pygame.font.SysFont("Menlo,DejaVu Sans Mono", 14, bold=True)
    status_font = pygame.font.SysFont("Menlo,DejaVu Sans Mono", 12)

    state = {"payload": {"trains": [], "buses": []}, "last_fetch": 0.0, "error": None}
    stop_event = threading.Event()
    threading.Thread(target=_fetch_loop, args=(state, stop_event), daemon=True).start()

    page_indexes = [0] * len(LAYOUTS)
    last_page_advance = time.time()
    fbs = [PygameFramebuffer() for _ in LAYOUTS]
    anim_states = [{} for _ in LAYOUTS]

    # Last button press in monotonic seconds. The full layout suspends the
    # commute auto-cycle for COMMUTE_IDLE_RESET_S after a press so the rider
    # can navigate to other trains; the split layout never suspends because
    # its RIGHT half is already showing "everything else".
    last_user_input = 0.0
    # None = follow the clock (Mon-Fri 7–12 NYC). MATRIX_FORCE_COMMUTE
    # pre-seeds this; press C to cycle forced-on → forced-off → auto.
    commute_override: bool | None = _commute_override_from_env()

    def _is_commute_now() -> bool:
        if commute_override is not None:
            return commute_override
        nyc = datetime.now(LOCAL_TZ)
        return time_focus.is_commute_window(nyc.weekday(), nyc.hour, nyc.minute)

    clock = pygame.time.Clock()
    last_frame = time.time()
    running = True
    while running:
        now = time.time()
        dt = now - last_frame
        last_frame = now

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False
                elif event.key == pygame.K_SPACE:
                    page_indexes = [p + 1 for p in page_indexes]
                    last_page_advance = now
                    last_user_input = now
                    for st in anim_states:
                        st.clear()
                elif event.key == pygame.K_c:
                    # Cycle: auto -> force-on -> force-off -> auto
                    if commute_override is None:
                        commute_override = True
                    elif commute_override is True:
                        commute_override = False
                    else:
                        commute_override = None

        in_commute = _is_commute_now()
        full_idle = in_commute and (now - last_user_input) > COMMUTE_IDLE_RESET_S

        if now - last_page_advance >= PAGE_INTERVAL_S:
            # Split layout: RIGHT side always rotates on the usual interval.
            page_indexes[1] += 1
            # Full layout: NRW+E+M50 commute view is frozen while idle —
            # only bump the carousel when the rider is browsing other trains.
            if not full_idle:
                page_indexes[0] += 1
            last_page_advance = now
            for st in anim_states:
                st.clear()

        commute_for_full = (
            time_focus.COMMUTE_BOARD_STATIC if full_idle else None
        )
        commute_for_split = (
            time_focus.COMMUTE_BOARD_STATIC if in_commute else None
        )
        commute_per_layout = [commute_for_full, commute_for_split]

        screen.fill((24, 24, 28))

        y_off = 0
        for i, (name, render_fn) in enumerate(LAYOUTS):
            try:
                render_fn(
                    fbs[i],
                    state["payload"],
                    page_indexes[i],
                    anim_states[i],
                    dt,
                    commute_board=commute_per_layout[i],
                )
            except Exception as exc:
                fbs[i].fill((40, 0, 0))
                fbs[i].draw_text(2, 2, "render error", (255, 100, 100), font="small")
                fbs[i].draw_text(2, 12, str(exc)[:30], (255, 100, 100), font="small")

            label = label_font.render(f"{i+1}. {name}", False, (230, 230, 230))
            screen.blit(label, (8, y_off + 4))
            y_off += LABEL_HEIGHT

            scaled = pygame.transform.scale(
                fbs[i].surface, (WIN_W, PANEL_H * PIXEL_SIZE)
            )
            screen.blit(scaled, (0, y_off))
            y_off += PANEL_H * PIXEL_SIZE
            if i < len(LAYOUTS) - 1:
                y_off += GAP_HEIGHT

        if state["error"]:
            status = f"ERR  {state['error'][:80]}"
            color = (255, 120, 120)
        else:
            age = int(now - state["last_fetch"]) if state["last_fetch"] else -1
            if commute_override is None:
                mode = "commute" if in_commute else "cycle"
            else:
                mode = ("commute" if in_commute else "cycle") + "*"  # * = forced
            board_now = commute_for_full or commute_for_split or "-"
            status = (
                f"OK · {len(state['payload'].get('trains', []))} trains · "
                f"{len(state['payload'].get('buses', []))} buses · "
                f"last {age}s · mode={mode} board={board_now} · SPACE/C/Q"
            )
            color = (180, 180, 180)
        status_surf = status_font.render(status, False, color)
        screen.blit(status_surf, (8, WIN_H - STATUS_HEIGHT + 4))

        pygame.display.flip()
        clock.tick(30)

    stop_event.set()
    pygame.quit()


if __name__ == "__main__":
    main()
