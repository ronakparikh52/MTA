"""
app.py
------
Flask app that translates MTA real-time data into one compact JSON payload
for the Matrix Portal frontend. The backend ships *all* the data; the LED
board (frontend) decides what to show and when.

Endpoints:
    GET /health   -> simple liveness check
    GET /matrix   -> single JSON payload with trains + buses
    GET /perf     -> recent in-memory performance samples

Run locally (from the `backend/` directory):
    pip install -r requirements.txt
    python app.py
    curl http://127.0.0.1:5000/matrix

Notes:
  * `time_schedule.py` is intentionally NOT imported. That logic now lives
    on the frontend (which picks which screen to render).
  * Buses use SIRI and require MTA_BUS_API_KEY. Without it, the `buses`
    array is simply empty and trains keep working.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

# Load secrets BEFORE importing modules that read os.environ at import time.
load_dotenv(Path(__file__).resolve().parent / ".env")

from bus_api import BusArrival, get_all_bus_arrivals  # noqa: E402
from mta_api import Departure, get_all_departures  # noqa: E402


# Cache TTL: every N seconds we rebuild the payload. The MTA feeds regenerate
# roughly every 30s, so 20s is a fine default.
CACHE_TTL_SECONDS = int(os.environ.get("MATRIX_CACHE_TTL", "20"))
PERF_LOG_PATH = os.environ.get("MATRIX_PERF_LOG_PATH", "backend_perf.log")
PERF_HISTORY_SIZE = int(os.environ.get("MATRIX_PERF_HISTORY_SIZE", "120"))

# SD-card friendly: only every Nth perf sample is written to disk. The full
# in-memory ring (served by /perf) is unaffected.
PERF_LOG_SAMPLE_EVERY = max(1, int(os.environ.get("MATRIX_PERF_LOG_EVERY", "10")))

# Background refresh: when true, a daemon thread rebuilds the payload just
# before the cache expires so /matrix calls are always cache hits.
BG_REFRESH_ENABLED = os.environ.get("MATRIX_BG_REFRESH", "1") != "0"

# Optional shared-secret check: if MATRIX_API_KEY is set, clients must send
# header `X-API-Key: <same value>` or they get 401. Leave unset for no auth.
API_KEY = os.environ.get("MATRIX_API_KEY", "")


app = Flask(__name__)
_logger = logging.getLogger("backend_perf")
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    try:
        _handler: logging.Handler = logging.FileHandler(PERF_LOG_PATH)
    except OSError:
        # Read-only filesystem (some hosts) — fall back to stderr so the app
        # still starts and the in-memory /perf endpoint keeps working.
        _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _logger.addHandler(_handler)


# ---------------------------------------------------------------------------
# Payload shaping (data only — frontend handles all visual formatting)
# ---------------------------------------------------------------------------

def _train_row(group_name: str, d: Departure) -> dict[str, Any]:
    """Compact train row: line / station / dir / min / next."""
    return {
        "line": d.route_id,
        "station": group_name,
        "dir": "U" if d.direction == "N" else ("D" if d.direction == "S" else d.direction),
        "min": d.minutes_away,
        "next": d.next_train_minutes,
    }


def _bus_row(b: BusArrival) -> dict[str, Any]:
    """Compact bus row: same shape as a train row (line / station / dir / min / next)."""
    return {
        "line": b.line,
        "station": b.stop_name,
        "dir": b.direction,
        "min": b.minutes_away,
        "next": b.next_minutes,
    }


def _safe_get_buses(now_epoch: int) -> tuple[list[BusArrival], str | None]:
    """Wrap the bus fetch so a SIRI failure doesn't kill the train payload."""
    try:
        return get_all_bus_arrivals(now_epoch=now_epoch), None
    except Exception as exc:  # pragma: no cover - depends on external service
        return [], str(exc)


def build_payload() -> dict[str, Any]:
    """Run the train + bus fetches concurrently and return a flat JSON dict."""
    now = int(time.time())

    # Trains and buses both do their own internal parallelism, but they
    # block independently — fetch them on separate threads so total wall
    # time is max(trains, buses), not their sum.
    train_groups: dict[str, list[Departure]] = {}
    bus_arrivals: list[BusArrival] = []
    bus_error: str | None = None

    def _fetch_trains() -> None:
        nonlocal train_groups
        train_groups = get_all_departures()

    def _fetch_buses() -> None:
        nonlocal bus_arrivals, bus_error
        bus_arrivals, bus_error = _safe_get_buses(now)

    t1 = threading.Thread(target=_fetch_trains, name="payload-trains")
    t2 = threading.Thread(target=_fetch_buses, name="payload-buses")
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    payload: dict[str, Any] = {
        "ok": True,
        "updated_at": now,
        "trains": [
            _train_row(name, d)
            for name, deps in train_groups.items()
            for d in deps
        ],
        "buses": [_bus_row(b) for b in bus_arrivals],
    }
    if bus_error:
        payload["errors"] = {"buses": bus_error}
    return payload


# ---------------------------------------------------------------------------
# Cache (one process; one worker on the Pi is the intended deployment)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()  # only one refresher runs at a time
_cache: dict[str, Any] = {"payload": None, "body": None, "fetched_at": 0.0}
_perf_history: deque[dict[str, Any]] = deque(maxlen=PERF_HISTORY_SIZE)
_perf_counter = 0


def _record_perf(
    *,
    cache_hit: bool,
    wall_ms: float,
    cpu_ms: float,
    payload_bytes: int | None,
) -> None:
    """Record refresh/request timing.

    Disk write is throttled (see PERF_LOG_SAMPLE_EVERY) to spare the SD card.
    """
    global _perf_counter
    row = {
        "ts_epoch": int(time.time()),
        "cache_hit": cache_hit,
        "wall_ms": round(wall_ms, 3),
        "cpu_ms": round(cpu_ms, 3),
        "cpu_pct_of_wall": round((cpu_ms / wall_ms) * 100, 2) if wall_ms > 0 else 0.0,
        "payload_bytes": payload_bytes,
    }
    _perf_history.append(row)
    _perf_counter += 1
    if not cache_hit or _perf_counter % PERF_LOG_SAMPLE_EVERY == 0:
        _logger.info(json.dumps(row, separators=(",", ":")))


def _refresh_cache() -> None:
    """Rebuild the payload and replace the cache.

    Double-check locking: overlapping callers share a single MTA fetch.
    """
    with _refresh_lock:
        now = time.time()
        with _cache_lock:
            fresh = (
                _cache["body"] is not None
                and now - _cache["fetched_at"] < CACHE_TTL_SECONDS
            )
        if fresh:
            return
        payload = build_payload()
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        with _cache_lock:
            _cache["payload"] = payload
            _cache["body"] = body
            _cache["fetched_at"] = time.time()


def get_cached_body() -> tuple[bytes, bool]:
    """Return the serialized JSON bytes and whether it was a cache hit."""
    now = time.time()
    with _cache_lock:
        body = _cache["body"]
        if body is not None and now - _cache["fetched_at"] < CACHE_TTL_SECONDS:
            return body, True
    _refresh_cache()
    with _cache_lock:
        return _cache["body"] or b'{"ok":false,"error":"no data"}', False


def _background_refresher(stop_event: threading.Event) -> None:
    """Rebuild the cache slightly before it expires, forever."""
    try:
        _refresh_cache()
    except Exception:  # pragma: no cover - first fetch failure is non-fatal
        pass
    interval = max(1, CACHE_TTL_SECONDS - 1)
    while not stop_event.wait(interval):
        try:
            _refresh_cache()
        except Exception:  # pragma: no cover - keep the loop alive on errors
            pass


_refresher_stop = threading.Event()
if BG_REFRESH_ENABLED:
    threading.Thread(
        target=_background_refresher,
        args=(_refresher_stop,),
        name="matrix-refresher",
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/matrix")
def matrix():
    wall_t0 = time.perf_counter()
    cpu_t0 = time.process_time()
    if API_KEY and request.headers.get("X-API-Key", "") != API_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        body, cache_hit = get_cached_body()
        wall_ms = (time.perf_counter() - wall_t0) * 1000
        cpu_ms = (time.process_time() - cpu_t0) * 1000
        _record_perf(
            cache_hit=cache_hit,
            wall_ms=wall_ms,
            cpu_ms=cpu_ms,
            payload_bytes=len(body),
        )
        return Response(body, mimetype="application/json")
    except Exception as exc:  # pragma: no cover - return soft error for device
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/perf")
def perf():
    """Recent performance samples, newest first. Same auth model as /matrix:
    when MATRIX_API_KEY is set, callers must include the matching X-API-Key
    header (otherwise the endpoint would leak request timing patterns and
    the on-disk perf-log path)."""
    if API_KEY and request.headers.get("X-API-Key", "") != API_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    rows = list(_perf_history)
    rows.reverse()
    return jsonify(
        {
            "ok": True,
            "cache_ttl_seconds": CACHE_TTL_SECONDS,
            "samples": rows,
            "log_path": PERF_LOG_PATH,
        }
    )


if __name__ == "__main__":
    # Local dev only. In production (Pi), run via gunicorn or waitress.
    port = int(os.environ.get("MATRIX_PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
