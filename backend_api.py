"""
backend_api.py
--------------
Tiny Flask app that translates MTA GTFS-Realtime protobuf into compact JSON
for the Matrix Portal. All heavy work happens here; the LED board only does
HTTP GET + render.

Endpoints:
    GET /health   -> simple liveness check
    GET /matrix   -> the one payload the Matrix needs

The payload reuses `time_schedule.get_schedule_bundle()` and is cached briefly
in-memory so rapid polls don't hit the MTA every time (free-tier friendly).

Run locally:
    pip install -r requirements.txt
    python backend_api.py
    curl http://127.0.0.1:5000/matrix
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any

from flask import Flask, Response, jsonify, request

from mta_api import Departure
from time_schedule import ScheduleBundle, get_schedule_bundle


# Cache TTL: every N seconds we rebuild the payload. The MTA feed itself
# regenerates roughly every 30s, so 20s is a fine default.
CACHE_TTL_SECONDS = int(os.environ.get("MATRIX_CACHE_TTL", "20"))
PERF_LOG_PATH = os.environ.get("MATRIX_PERF_LOG_PATH", "backend_perf.log")
PERF_HISTORY_SIZE = int(os.environ.get("MATRIX_PERF_HISTORY_SIZE", "120"))

# SD-card friendly: only every Nth perf sample is written to disk. The full
# in-memory ring (served by /perf) is unaffected. Default 10 = ~1 write every
# 5 minutes of 30s polling, which is negligible SD wear.
PERF_LOG_SAMPLE_EVERY = max(1, int(os.environ.get("MATRIX_PERF_LOG_EVERY", "10")))

# Background refresh: when true, a daemon thread rebuilds the payload just
# before the cache expires so /matrix calls are always cache hits (near-zero
# wall time on the Pi). Default on — set MATRIX_BG_REFRESH=0 to disable.
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
# Payload shaping
# ---------------------------------------------------------------------------

def _row(d: Departure) -> dict[str, Any]:
    """Compact per-row shape: line / dir / min / next."""
    return {
        "line": d.route_id,
        "dir": "U" if d.direction == "N" else ("D" if d.direction == "S" else d.direction),
        "min": d.minutes_away,
        "next": d.next_train_minutes,
    }


def build_payload(bundle: ScheduleBundle) -> dict[str, Any]:
    """Turn a ScheduleBundle into the JSON the Matrix will consume."""
    return {
        "ok": True,
        "updated_at": int(bundle.when_local.timestamp()),
        "screen": bundle.suggested,
        "bloomberg": [_row(d) for d in bundle.bloomberg_rows],
        "groups": {
            name: [_row(d) for d in rows]
            for name, rows in bundle.default_groups.items()
        },
    }


# ---------------------------------------------------------------------------
# Simple cache (one process only; fine for free-tier single worker)
#
# We cache *both* the payload dict (for /matrix consumers that want JSON) and
# the already-serialized bytes so cache hits skip json.dumps on the Pi.
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
    """Record refresh/request timing so we can prove CPU isn't the bottleneck.

    - wall_ms: end-to-end elapsed time seen by caller
    - cpu_ms: process CPU consumed during that period

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
    # Always log cache misses (rare, informative); sample cache hits.
    if not cache_hit or _perf_counter % PERF_LOG_SAMPLE_EVERY == 0:
        _logger.info(json.dumps(row, separators=(",", ":")))


def _refresh_cache() -> None:
    """Rebuild the payload from a fresh feed fetch and replace the cache.

    Double-check locking: overlapping callers share a single MTA fetch. If
    another thread already refreshed while we were waiting, we return without
    doing any additional work.
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
        bundle = get_schedule_bundle()
        payload = build_payload(bundle)
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
    # Cache cold / stale and the background thread hasn't repopulated yet.
    # Synchronously refresh (coalesced across callers by _refresh_lock).
    _refresh_cache()
    with _cache_lock:
        return _cache["body"] or b'{"ok":false,"error":"no data"}', False


def _background_refresher(stop_event: threading.Event) -> None:
    """Rebuild the cache slightly before it expires, forever."""
    # First fetch immediately so /matrix is warm on boot.
    try:
        _refresh_cache()
    except Exception:  # pragma: no cover - first fetch failure is non-fatal
        pass
    # Refresh just before the TTL so callers never see a miss.
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
    """Recent performance samples, newest first."""
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
    # Local dev only. On PythonAnywhere the WSGI file imports `app`.
    app.run(host="0.0.0.0", port=5000, debug=False)
