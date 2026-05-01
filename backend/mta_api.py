"""
mta_api.py
----------
Live NYC subway departures from the MTA's free GTFS-Realtime feeds, returned
as simple `Departure` records. This module is JSON/data only — formatting
(colors, fonts, layout) is the frontend's job.

Pipeline:
  1. The MTA publishes each line group as a protobuf feed over HTTPS.
  2. `fetch_feed` downloads one feed; `gtfs-realtime-bindings` decodes it.
  3. `extract_departures` walks the feed and keeps predictions that match a
     given set of parent stop IDs and routes, filtering out departures that
     are already gone or below MIN_MINUTES_ACHIEVABLE.
  4. `build_display_rows` caps the result to N rows per (route, direction)
     and fills each row's `next_train_minutes` from the rows we dropped.

No API key is required.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Iterable

import requests
from google.transit import gtfs_realtime_pb2


# ---------------------------------------------------------------------------
# Feed URLs & constants
# ---------------------------------------------------------------------------

FEED_BASE = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds"

FEED_URLS: dict[str, str] = {
    "ACE":      f"{FEED_BASE}/nyct%2Fgtfs-ace",
    "NQRW":     f"{FEED_BASE}/nyct%2Fgtfs-nqrw",
    "NUMBERED": f"{FEED_BASE}/nyct%2Fgtfs",
}

# "0 min" predictions are effectively "less than 60 seconds away"; you can't
# physically catch that train, so we ignore anything below this threshold.
MIN_MINUTES_ACHIEVABLE = 1


# ---------------------------------------------------------------------------
# Station / line configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StationGroup:
    name: str
    feed_key: str
    stop_ids: tuple[str, ...]   # parent IDs (no N/S suffix); matches both platforms
    routes: tuple[str, ...]     # allowed GTFS route_ids


STATION_GROUPS: list[StationGroup] = [
    StationGroup("ACE @ 50 St",  "ACE",      ("A25",), ("A", "C", "E")),
    StationGroup("1/2 @ 50 St",  "NUMBERED", ("120",), ("1", "2")),
    StationGroup("NRQW @ 49 St", "NQRW",     ("R16",), ("N", "Q", "R", "W")),
]


# ---------------------------------------------------------------------------
# Departure record (the only shape the rest of the app needs to know about)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Departure:
    route_id: str               # "A", "C", "1", "N", etc.
    stop_id: str                # platform-level id, e.g. "A25N"
    direction: str              # "N" uptown / "S" downtown
    arrival_epoch: int          # POSIX seconds when train arrives
    minutes_away: int           # whole minutes from "now"
    next_train_minutes: int | None = None  # following train at same stop+dir


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------

# Shared HTTP session: one TCP+TLS connection stays warm between polls, which
# is the single biggest win on a Pi Zero 2 W where TLS handshakes are costly.
_SESSION = requests.Session()
_SESSION.headers.update({
    "Accept-Encoding": "gzip, deflate",
    "User-Agent": "mta-led-sign/1.0",
})


def fetch_feed(feed_key: str, timeout: float = 10.0) -> gtfs_realtime_pb2.FeedMessage:
    """Download one MTA feed and decode it into a protobuf FeedMessage."""
    response = _SESSION.get(FEED_URLS[feed_key], timeout=timeout)
    response.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    return feed


_FEED_KEYS: tuple[str, ...] = tuple(FEED_URLS)
# Long-lived pool, sized to the number of feeds. Reused across refreshes.
_FEED_POOL = ThreadPoolExecutor(max_workers=len(_FEED_KEYS), thread_name_prefix="mta-feed")


def fetch_all_subway_feeds(timeout: float = 10.0) -> dict[str, gtfs_realtime_pb2.FeedMessage]:
    """Download every subway feed we care about, in parallel (I/O-bound)."""
    feeds = _FEED_POOL.map(lambda k: fetch_feed(k, timeout=timeout), _FEED_KEYS)
    return dict(zip(_FEED_KEYS, feeds))


def extract_departures(
    feed: gtfs_realtime_pb2.FeedMessage,
    parent_stop_ids: Iterable[str],
    routes: Iterable[str],
    now_epoch: int | None = None,
) -> list[Departure]:
    """Scan a feed and return matching future departures, sorted by time.

    Drops:
      * entities without a `trip_update`
      * trips whose route_id isn't in `routes`
      * stop_time_updates whose parent stop isn't in `parent_stop_ids`
      * predictions already in the past or below MIN_MINUTES_ACHIEVABLE
    """
    now = int(time.time()) if now_epoch is None else now_epoch
    allowed_stops = frozenset(parent_stop_ids)
    allowed_routes = frozenset(routes)
    min_future_epoch = now + MIN_MINUTES_ACHIEVABLE * 60
    out: list[Departure] = []

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip = entity.trip_update
        route_id = trip.trip.route_id
        if route_id not in allowed_routes:
            continue

        for stu in trip.stop_time_update:
            stop_id = stu.stop_id
            suffix = stop_id[-1:]
            parent = stop_id[:-1] if suffix in ("N", "S") else stop_id
            if parent not in allowed_stops:
                continue

            # Prefer `departure.time`, fall back to `arrival.time` (0 = unset).
            when = (stu.departure.time if stu.HasField("departure") else 0) \
                or (stu.arrival.time if stu.HasField("arrival") else 0)
            if when < min_future_epoch:
                continue

            out.append(Departure(
                route_id=route_id,
                stop_id=stop_id,
                direction=suffix if suffix in ("N", "S") else "?",
                arrival_epoch=when,
                minutes_away=(when - now) // 60,
            ))

    out.sort(key=lambda d: d.arrival_epoch)
    return out


# ---------------------------------------------------------------------------
# Turn raw departures into the rows we actually display
# ---------------------------------------------------------------------------

def build_display_rows(
    departures: list[Departure],
    *,
    per_route_direction: int = 1,
) -> list[Departure]:
    """Cap rows per (route, direction) and fill `next_train_minutes`.

    - Groups `departures` (assumed sorted asc by time) by (route_id, direction).
    - Keeps the first `per_route_direction` rows in each group.
    - For each kept row, `next_train_minutes` is the `minutes_away` of the next
      prediction in that same group — even if that next train wasn't kept as
      its own row. Returns a fresh sorted list by (route_id, direction).
    """
    if per_route_direction < 1:
        raise ValueError("per_route_direction must be at least 1")

    buckets: dict[tuple[str, str], list[Departure]] = {}
    for d in departures:
        buckets.setdefault((d.route_id, d.direction), []).append(d)

    out: list[Departure] = []
    for key in sorted(buckets):
        seq = buckets[key]
        for i in range(min(per_route_direction, len(seq))):
            nxt = seq[i + 1].minutes_away if i + 1 < len(seq) else None
            out.append(replace(seq[i], next_train_minutes=nxt))
    return out


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

def get_all_departures_from_feed_cache(
    feed_cache: dict[str, gtfs_realtime_pb2.FeedMessage],
    *,
    per_route_direction: int = 1,
) -> dict[str, list[Departure]]:
    """Build the default station-group board from an existing feed cache."""
    results: dict[str, list[Departure]] = {}
    for group in STATION_GROUPS:
        full = extract_departures(feed_cache[group.feed_key], group.stop_ids, group.routes)
        results[group.name] = build_display_rows(full, per_route_direction=per_route_direction)
    return results


def get_departures_for_group(
    group: StationGroup,
    *,
    per_route_direction: int = 1,
) -> list[Departure]:
    """Fetch one group's feed and return display-ready rows."""
    full = extract_departures(fetch_feed(group.feed_key), group.stop_ids, group.routes)
    return build_display_rows(full, per_route_direction=per_route_direction)


def get_all_departures(*, per_route_direction: int = 1) -> dict[str, list[Departure]]:
    """Fetch every configured group (one HTTP call per distinct feed)."""
    return get_all_departures_from_feed_cache(
        fetch_all_subway_feeds(), per_route_direction=per_route_direction,
    )
