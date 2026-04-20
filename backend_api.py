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

import os
import threading
import time
from typing import Any

from flask import Flask, jsonify

from mta_api import Departure
from time_schedule import ScheduleBundle, get_schedule_bundle


# Cache TTL: every N seconds we rebuild the payload. The MTA feed itself
# regenerates roughly every 30s, so 20s is a fine default.
CACHE_TTL_SECONDS = int(os.environ.get("MATRIX_CACHE_TTL", "20"))

# Optional shared-secret check: if MATRIX_API_KEY is set, clients must send
# header `X-API-Key: <same value>` or they get 401. Leave unset for no auth.
API_KEY = os.environ.get("MATRIX_API_KEY", "")


app = Flask(__name__)


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
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"payload": None, "fetched_at": 0.0}


def get_cached_payload() -> dict[str, Any]:
    """Return a recent payload, refreshing only every CACHE_TTL_SECONDS."""
    now = time.time()
    with _cache_lock:
        if _cache["payload"] is not None and now - _cache["fetched_at"] < CACHE_TTL_SECONDS:
            return _cache["payload"]
    # Fetch outside the lock so parallel requests don't serialize on MTA I/O
    bundle = get_schedule_bundle()
    payload = build_payload(bundle)
    with _cache_lock:
        _cache["payload"] = payload
        _cache["fetched_at"] = time.time()
    return payload


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/matrix")
def matrix():
    if API_KEY:
        from flask import request
        if request.headers.get("X-API-Key", "") != API_KEY:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        return jsonify(get_cached_payload())
    except Exception as exc:  # pragma: no cover - return soft error for device
        return jsonify({"ok": False, "error": str(exc)}), 502


if __name__ == "__main__":
    # Local dev only. On PythonAnywhere the WSGI file imports `app`.
    app.run(host="0.0.0.0", port=5000, debug=False)
