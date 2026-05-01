"""
bus_api.py
----------
Real-time NYC bus arrivals via MTA's SIRI StopMonitoring endpoint.

Unlike the subway feeds, SIRI is JSON (no protobuf) but requires a free API
key. Get one at https://register.developer.obanyc.com and export it as
MTA_BUS_API_KEY.

Each entry in `BUS_STOPS` is one (route, direction) pair pinned to a single
real-world bus stop. Look up your stop codes once via:

    MTA_BUS_API_KEY=... python bus_api.py find M50

Then paste the codes into BUS_STOPS below.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# Load `backend/.env` (gitignored) so MTA_BUS_API_KEY is available without
# having to export it manually. Idempotent — safe if called from app.py too.
load_dotenv(Path(__file__).resolve().parent / ".env")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BUS_API_KEY = os.environ.get("MTA_BUS_API_KEY", "")

SIRI_STOP_MONITORING_URL = "https://bustime.mta.info/api/siri/stop-monitoring.json"
STOPS_FOR_ROUTE_URL = "https://bustime.mta.info/api/where/stops-for-route/MTA%20NYCT_{route}.json"


@dataclass(frozen=True)
class BusStop:
    line: str          # GTFS-style route, e.g. "M50"
    direction: str     # free-text label for display; you choose the wording
    stop_code: str     # SIRI MonitoringRef, e.g. "401919" (find with `find` cmd)


# Bus lines covering the Midtown West / Hell's Kitchen corridor (roughly
# 42-50 St between 6 Av and 10 Av). Adjust BUS_STOPS for your own stop codes
# — use `python bus_api.py find <ROUTE>` to look them up by route + cross
# street. Empty stop_code means that row is silently skipped at fetch time.
BUS_STOPS: list[BusStop] = [
    BusStop("M11",  "Uptown",    "401409"),   # 10 AV / W 47 ST  (NE)
    BusStop("M11",  "Downtown",  "401495"),   # 9 AV  / W 46 ST  (SW)
    BusStop("M20",  "Uptown",    "404846"),   # 8 AV  / W 49 ST  (NE)
    BusStop("M20",  "Downtown",  "403797"),   # 7 AV  / W 50 ST  (SW)
    BusStop("M42",  "Eastbound", "403239"),   # W 42 ST / 8 AV   (SE)
    BusStop("M42",  "Westbound", "401851"),   # W 42 ST / 8 AV   (NW)
    BusStop("M50",  "Eastbound", "402167"),   # W 50 ST / 8 AV   (SE)
    BusStop("M50",  "Westbound", "404035"),   # W 49 ST / 7 AV   (NW)
    BusStop("M104", "Uptown",    "404846"),   # 8 AV  / W 49 ST  (NE)
    BusStop("M104", "Downtown",  "405292"),   # 7 AV  / W 49 ST  (SW)
    BusStop("M7",   "Uptown",    "400938"),   # 6 AV  / W 47 ST  (NE)
    BusStop("M7",   "Downtown",  "403797"),   # 7 AV  / W 50 ST  (SW)
]


# ---------------------------------------------------------------------------
# Output record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BusArrival:
    line: str
    direction: str
    stop_name: str          # e.g. "9 AV/W 50 ST" — populated from SIRI
    minutes_away: int
    next_minutes: int | None = None


# ---------------------------------------------------------------------------
# HTTP plumbing (shared session + long-lived thread pool)
# ---------------------------------------------------------------------------

_SESSION = requests.Session()
_SESSION.headers.update({
    "Accept-Encoding": "gzip, deflate",
    "User-Agent": "mta-led-sign/1.0",
})

# One worker per stop is overkill; cap at 8 so we never hammer the SIRI API.
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bus-fetch")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------

def fetch_arrivals_for_stop(
    stop: BusStop,
    now_epoch: int,
    timeout: float = 8.0,
) -> BusArrival | None:
    """Return a single BusArrival for `stop` (next + train-after), or None.

    Skips silently when no API key is configured or the stop_code is blank.
    """
    if not BUS_API_KEY or not stop.stop_code:
        return None

    params = {
        "key": BUS_API_KEY,
        "MonitoringRef": stop.stop_code,
        "LineRef": f"MTA NYCT_{stop.line}",
        "MaximumStopVisits": 5,
    }
    response = _SESSION.get(SIRI_STOP_MONITORING_URL, params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    minutes: list[int] = []
    stop_name = ""
    deliveries = (
        data.get("Siri", {})
            .get("ServiceDelivery", {})
            .get("StopMonitoringDelivery", [])
    )
    for delivery in deliveries:
        for visit in delivery.get("MonitoredStopVisit", []):
            mvj = visit.get("MonitoredVehicleJourney", {})
            call = mvj.get("MonitoredCall", {})
            if not stop_name:
                stop_name = call.get("StopPointName", "") or ""
            iso = call.get("ExpectedArrivalTime") or call.get("AimedArrivalTime")
            arrival_dt = _parse_iso(iso)
            if arrival_dt is None:
                continue
            mins = int((arrival_dt.timestamp() - now_epoch) // 60)
            if mins >= 0:
                minutes.append(mins)

    if not minutes:
        return None
    minutes.sort()
    return BusArrival(
        line=stop.line,
        direction=stop.direction,
        stop_name=stop_name or stop.stop_code,
        minutes_away=minutes[0],
        next_minutes=minutes[1] if len(minutes) > 1 else None,
    )


def get_all_bus_arrivals(now_epoch: int | None = None) -> list[BusArrival]:
    """Fetch every configured bus stop concurrently and return arrivals."""
    if not BUS_API_KEY:
        return []
    now = int(time.time()) if now_epoch is None else now_epoch
    results = _POOL.map(lambda s: fetch_arrivals_for_stop(s, now), BUS_STOPS)
    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# CLI helper: look up stop codes for a route
# ---------------------------------------------------------------------------

def find_stops_for_route(route: str) -> list[dict[str, Any]]:
    """List every stop on `route` (e.g. 'M50') so you can find stop codes."""
    if not BUS_API_KEY:
        raise RuntimeError("Set MTA_BUS_API_KEY first.")
    url = STOPS_FOR_ROUTE_URL.format(route=route)
    response = requests.get(
        url,
        params={"key": BUS_API_KEY, "version": "2", "includePolylines": "false"},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    stops = data.get("data", {}).get("references", {}).get("stops", [])
    return [
        {
            "code": s.get("code"),
            "name": s.get("name"),
            "direction": s.get("direction"),
        }
        for s in stops
    ]


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "find":
        route = sys.argv[2].upper()
        print(f"Stops on {route} (direction = compass heading the bus is going):\n")
        print(f"  {'CODE':>8}  {'DIR':>3}  NAME")
        for stop in find_stops_for_route(route):
            print(f"  {stop['code']:>8}  {stop.get('direction', '?'):>3}  {stop['name']}")
    else:
        print("Usage: python bus_api.py find <ROUTE>", file=sys.stderr)
        print("Example: python bus_api.py find M50", file=sys.stderr)
        sys.exit(2)
