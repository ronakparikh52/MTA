# Matrix Portal S3 setup

Once-per-board procedure to get the LED sign running.

## 1. Flash CircuitPython

1. Hold the **BOOT** button while plugging the Matrix Portal S3 into your laptop.
   It mounts as `MATRIXBOOT`.
2. Download the latest CircuitPython 9.x `.uf2` for the **Adafruit Matrix Portal S3**
   from <https://circuitpython.org/board/adafruit_matrixportal_s3/>.
3. Drop the `.uf2` onto `MATRIXBOOT`. The board reboots and remounts as `CIRCUITPY`.

## 2. Install required libraries

Download the matching CircuitPython library bundle from
<https://circuitpython.org/libraries> (use the `9.x` bundle).
Copy these from the bundle's `lib/` into `CIRCUITPY/lib/`:

- `adafruit_requests.mpy`
- `adafruit_connection_manager.mpy`

Everything else this project needs (`displayio`, `framebufferio`, `rgbmatrix`,
`socketpool`, `wifi`, `ssl`, `gc`, `os`, `time`) is built into the firmware.

## 3. Copy this project's frontend code

Copy these files **from `frontend/` in this repo to the root of `CIRCUITPY`**:

- `code.py`
- `bitmap_font.py`
- `render.py`
- `layouts/` (the whole folder)

## 4. Add config

Copy `settings.toml.example` to `CIRCUITPY/settings.toml` and fill in:

- `CIRCUITPY_WIFI_SSID`, `CIRCUITPY_WIFI_PASSWORD` — your WiFi
- `BACKEND_URL` — your Pi's LAN IP, e.g. `http://192.168.1.42:5001/matrix`

The board reboots automatically when `settings.toml` is saved. The panel
should show "wifi…" then "loading" then start cycling through the sign.

## How updates work

After this is set up once, every code change is just:
1. Edit files in `frontend/`.
2. Copy the changed file(s) to `CIRCUITPY/`.
3. CircuitPython auto-reboots and runs the new code.

The same `bitmap_font.py`, `render.py`, and `layouts/` files run on both
the desktop simulator and the device — there is no device-specific layout
code. Only `code.py` (device entry point) and `simulator.py` (desktop entry
point) differ, and they're both small thin adapters around the shared
`Framebuffer` interface.
