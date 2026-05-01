"""
code.py
-------
CircuitPython entry point for the Adafruit Matrix Portal S3.

Auto-runs on boot. Drives a 128x32 chained pair of 64x32 RGB matrix
panels (HUB75) using `rgbmatrix` + `framebufferio`.

Behavior:
  * Connect to WiFi (creds from `settings.toml`).
  * Every POLL_SECONDS, GET the backend's /matrix endpoint (the Pi's
    JSON payload).
  * Every PAGE_SECONDS, advance the carousel (except during Mon-Fri 7-12
    when idle: the full panel stays on the static NRW+E+M50 trio until
    you press a button, then you can scroll other groups for ~30 seconds).
  * Render via the *exact same* `layouts.big_colorful.render()` that
    the desktop simulator uses. The only divergence between targets is
    `Framebuffer` — the simulator's wraps a pygame.Surface, the device's
    wraps a `displayio.Bitmap`. Layouts don't know or care which one.

Required CircuitPython libraries (drop into /lib on the CIRCUITPY drive):
    - adafruit_requests
    - adafruit_connection_manager   (CP 9+)

Required settings.toml at the root of CIRCUITPY:
    CIRCUITPY_WIFI_SSID = "your-wifi"
    CIRCUITPY_WIFI_PASSWORD = "your-password"
    BACKEND_URL = "http://192.168.x.y:5001/matrix"
    POLL_SECONDS = "30"
    PAGE_SECONDS = "10"
    LOCAL_TZ_OFFSET_HOURS = "-4"   # ET: -5 (EST) or -4 (EDT)
"""

import gc
import os
import ssl
import time

import board
import digitalio
import displayio
import framebufferio
import rgbmatrix
import socketpool
import wifi

import adafruit_requests

import bitmap_font
import time_focus
from render import Framebuffer
from layouts.big_colorful import render as render_layout


# --------------------------------------------------------------------------- #
# Display setup
# --------------------------------------------------------------------------- #

PANEL_W = 128
PANEL_H = 32

# Free any auto-claimed displays so we can take over the matrix pins.
displayio.release_displays()

# Two 64x32 panels chained = 128x32 logical canvas. bit_depth=4 gives 16
# levels per channel (4096 colors); enough for our palette and keeps RAM
# use modest. The Matrix Portal S3 exposes named pin constants for every
# HUB75 pin so we don't have to hand-map them.
matrix = rgbmatrix.RGBMatrix(
    width=PANEL_W,
    height=PANEL_H,
    bit_depth=4,
    rgb_pins=[
        board.MTX_R1, board.MTX_G1, board.MTX_B1,
        board.MTX_R2, board.MTX_G2, board.MTX_B2,
    ],
    addr_pins=[board.MTX_ADDRA, board.MTX_ADDRB, board.MTX_ADDRC, board.MTX_ADDRD],
    clock_pin=board.MTX_CLK,
    latch_pin=board.MTX_LAT,
    output_enable_pin=board.MTX_OE,
    tile=1,
    serpentine=False,
    doublebuffer=True,
)
display = framebufferio.FramebufferDisplay(matrix, auto_refresh=True)

# Single bitmap that the layout writes pixels into. 256 palette entries is
# the max for an 8-bit-indexed bitmap; we only need ~15 distinct colours so
# this is comfortable.
bitmap = displayio.Bitmap(PANEL_W, PANEL_H, 256)
palette = displayio.Palette(256)
palette[0] = 0x000000  # background

group = displayio.Group()
group.append(displayio.TileGrid(bitmap, pixel_shader=palette))
display.root_group = group


# --------------------------------------------------------------------------- #
# Framebuffer adapter — same interface as the simulator's PygameFramebuffer.
# --------------------------------------------------------------------------- #

class MatrixFramebuffer(Framebuffer):
    """Implements `Framebuffer` against a `displayio.Bitmap` + `Palette`.

    Maintains an RGB->palette-index cache so we can keep speaking in
    (r,g,b) tuples in the layout code while displayio wants integer
    indices into a palette."""

    width = PANEL_W
    height = PANEL_H

    def __init__(self, bitmap, palette):
        self._bitmap = bitmap
        self._palette = palette
        # (r,g,b) -> palette index. Black is pre-installed at index 0.
        self._color_index = {(0, 0, 0): 0}

    def _idx(self, rgb):
        idx = self._color_index.get(rgb)
        if idx is not None:
            return idx
        slot = len(self._color_index)
        if slot >= 256:
            return 0  # palette full — shouldn't happen with our small set
        self._palette[slot] = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
        self._color_index[rgb] = slot
        return slot

    def fill(self, rgb):
        # Bitmap.fill() runs in C and is the fastest way to clear the panel.
        self._bitmap.fill(self._idx(rgb))

    def set_pixel(self, x, y, rgb):
        if 0 <= x < self.width and 0 <= y < self.height:
            self._bitmap[x, y] = self._idx(rgb)

    def fill_rect(self, x, y, w, h, rgb):
        idx = self._idx(rgb)
        for py in range(y, y + h):
            if py < 0 or py >= self.height:
                continue
            for px in range(x, x + w):
                if 0 <= px < self.width:
                    self._bitmap[px, py] = idx

    def fill_circle(self, cx, cy, r, rgb):
        idx = self._idx(rgb)
        r2 = r * r
        for y in range(cy - r, cy + r + 1):
            if y < 0 or y >= self.height:
                continue
            for x in range(cx - r, cx + r + 1):
                if x < 0 or x >= self.width:
                    continue
                dx = x - cx
                dy = y - cy
                if dx * dx + dy * dy <= r2:
                    self._bitmap[x, y] = idx

    def text_size(self, text, font="small"):
        return bitmap_font.text_size(text)

    def draw_text(self, x, y, text, rgb, font="small"):
        bitmap_font.draw(self.set_pixel, x, y, text, rgb)

    def draw_text_centered(self, cx, cy, text, rgb, font="small"):
        bitmap_font.draw_centered(self.set_pixel, cx, cy, text, rgb)


fb = MatrixFramebuffer(bitmap, palette)


# --------------------------------------------------------------------------- #
# WiFi + HTTP
# --------------------------------------------------------------------------- #

BACKEND_URL = os.getenv("BACKEND_URL", "")
POLL_SECONDS = int(os.getenv("POLL_SECONDS") or "30")
PAGE_SECONDS = int(os.getenv("PAGE_SECONDS") or "10")
WIFI_SSID = os.getenv("CIRCUITPY_WIFI_SSID")
WIFI_PASSWORD = os.getenv("CIRCUITPY_WIFI_PASSWORD")
# CircuitPython has no `zoneinfo`; we approximate ET by applying a fixed
# offset to UTC. -4 covers the EDT half of the year (mid-Mar -> early Nov);
# the user can flip to -5 for EST. A 1-hour drift twice a year is fine for
# a "morning commute window" check that's only loose to the minute anyway.
LOCAL_TZ_OFFSET_HOURS = int(os.getenv("LOCAL_TZ_OFFSET_HOURS") or "-4")
# Mirror of the simulator constant.
COMMUTE_IDLE_RESET_S = 30.0


def _show_status_text(message, rgb=(255, 80, 80)):
    """Render a one-off status message centered on the panel — used during
    boot and for fatal errors so the panel never just sits blank."""
    fb.fill((0, 0, 0))
    fb.draw_text_centered(PANEL_W // 2, PANEL_H // 2, message, rgb)


def _connect_wifi():
    if not WIFI_SSID or not WIFI_PASSWORD:
        _show_status_text("no wifi creds")
        raise RuntimeError("Missing CIRCUITPY_WIFI_SSID / CIRCUITPY_WIFI_PASSWORD")
    _show_status_text("wifi...", (180, 180, 180))
    wifi.radio.connect(WIFI_SSID, WIFI_PASSWORD)
    pool = socketpool.SocketPool(wifi.radio)
    return adafruit_requests.Session(pool, ssl.create_default_context())


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def _make_button(pin):
    """Configure a Matrix Portal S3 user button as input + pull-up.
    The buttons are active-low (pressed = False on `.value`)."""
    btn = digitalio.DigitalInOut(pin)
    btn.direction = digitalio.Direction.INPUT
    btn.pull = digitalio.Pull.UP
    return btn


def _local_time_now():
    """Return (weekday, hour, minute) in the configured local timezone.

    `time.time()` on CP is UTC after NTP sync (which `wifi.radio.connect`
    triggers indirectly via the network connection on the S3). We add the
    fixed offset and feed the result back through `time.localtime` so we
    get a struct_time we can read fields off."""
    local_epoch = time.time() + LOCAL_TZ_OFFSET_HOURS * 3600
    t = time.localtime(local_epoch)
    return t.tm_wday, t.tm_hour, t.tm_min


def main():
    if not BACKEND_URL:
        _show_status_text("no BACKEND_URL")
        while True:
            time.sleep(60)

    session = _connect_wifi()

    # Built-in user buttons. UP advances pages (same as SPACE in the
    # simulator); DOWN does the same but in reverse so you can scroll
    # back. Each press also resets the "idle" timer so commute mode
    # doesn't snap you back instantly.
    btn_up = _make_button(board.BUTTON_UP)
    btn_down = _make_button(board.BUTTON_DOWN)
    prev_up = True   # not-pressed
    prev_down = True

    payload = {"trains": [], "buses": []}
    last_fetch = 0.0
    last_page = 0.0
    page_index = 0
    consecutive_errors = 0
    last_user_input = -1e9  # never pressed -> idle reset always satisfied

    _show_status_text("loading", (180, 180, 180))

    while True:
        now = time.monotonic()

        if now - last_fetch >= POLL_SECONDS or last_fetch == 0.0:
            try:
                resp = session.get(BACKEND_URL, timeout=10)
                payload = resp.json()
                resp.close()
                last_fetch = now
                consecutive_errors = 0
                gc.collect()
            except Exception as exc:  # noqa: BLE001
                consecutive_errors += 1
                _show_status_text("net err", (255, 80, 80))
                time.sleep(min(30, 2 * consecutive_errors))
                continue

        # Edge-trigger button reads: only react on press (True -> False).
        cur_up = btn_up.value
        cur_down = btn_down.value
        if prev_up and not cur_up:
            page_index += 1
            last_page = now
            last_user_input = now
        if prev_down and not cur_down:
            page_index -= 1
            last_page = now
            last_user_input = now
        prev_up, prev_down = cur_up, cur_down

        # Auto-advance: full panel freezes on the commute trio while idle ;
        # after button activity the rider cycles through pages every tick.
        try:
            wday, hour, minute = _local_time_now()
            in_commute = time_focus.is_commute_window(wday, hour, minute)
        except Exception:
            in_commute = False

        idle_commute_focus = (
            in_commute and (now - last_user_input) > COMMUTE_IDLE_RESET_S
        )

        if now - last_page >= PAGE_SECONDS:
            last_page = now
            if not idle_commute_focus:
                page_index += 1

        if idle_commute_focus:
            commute_board = time_focus.COMMUTE_BOARD_STATIC
        else:
            commute_board = None

        try:
            render_layout(
                fb, payload, page_index, {}, 0.0, commute_board=commute_board
            )
        except Exception as exc:  # noqa: BLE001
            _show_status_text("render err", (255, 80, 80))

        time.sleep(0.1)


main()
