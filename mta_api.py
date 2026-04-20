"""
mta_api.py
----------
Live NYC subway departures from the MTA's free GTFS-Realtime feeds, returned
as simple `Departure` records plus helpers to format them for a terminal or
a hardware display.

How it works:
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

import os
import sys
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


# Module-level tables so direction helpers don't rebuild a dict per call.
_DIRECTION_LABELS = {"N": "Uptown", "S": "Downtown"}
_DIRECTION_LETTERS = {"N": "U", "S": "D"}


def direction_label(direction: str) -> str:
    """Long label: 'Uptown' / 'Downtown'."""
    return _DIRECTION_LABELS.get(direction, direction)


def direction_letter(direction: str) -> str:
    """Short label: 'U' / 'D'."""
    return _DIRECTION_LETTERS.get(direction, direction)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_departure_pipes(d: Departure, *, missing_next: str = "—") -> str:
    """Hardware-friendly row: ``LINE | Direction | min | next_min |``."""
    nxt = d.next_train_minutes if d.next_train_minutes is not None else missing_next
    return f"{d.route_id} | {direction_label(d.direction)} | {d.minutes_away} | {nxt} |"


# Approximate official NYC subway route colors (RGB).
_LINE_COLORS: dict[str, tuple[int, int, int]] = {
    # 8 Av (blue)
    "A": (0, 57, 166), "C": (0, 57, 166), "E": (0, 57, 166),
    # 6 Av (orange)
    "B": (255, 99, 27), "D": (255, 99, 27), "F": (255, 99, 27), "M": (255, 99, 27),
    # Broadway (yellow), plus the aggregated "NRW" badge
    "N": (252, 204, 10), "Q": (252, 204, 10), "R": (252, 204, 10), "W": (252, 204, 10),
    "NRW": (252, 204, 10),
    # IRT reds / greens / purple
    "1": (238, 53, 63), "2": (238, 53, 63), "3": (238, 53, 63),
    "4": (0, 147, 69),  "5": (0, 147, 69),  "6": (0, 147, 69),
    "7": (116, 44, 148),
    # Misc
    "G": (108, 190, 69),
    "J": (153, 102, 51), "Z": (153, 102, 51),
    "L": (167, 169, 172),
    "S": (128, 128, 128), "H": (128, 128, 128),
}
_DEFAULT_LINE_COLOR = (180, 180, 180)


def line_rgb(route_id: str) -> tuple[int, int, int]:
    """Return (r, g, b) for a route. Used by terminal color + hardware rendering."""
    return _LINE_COLORS.get(route_id.upper(), _DEFAULT_LINE_COLOR)


def terminal_color_enabled() -> bool:
    """True when printing to a real terminal without NO_COLOR set."""
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def format_departure_terminal(
    d: Departure,
    *,
    missing_next: str = "—",
    use_color: bool | None = None,
    route_width: int = 4,
) -> str:
    """One terminal row: colored route badge, U/D, then two minute columns."""
    if use_color is None:
        use_color = terminal_color_enabled()

    nxt = d.next_train_minutes if d.next_train_minutes is not None else missing_next
    width = max(route_width, len(d.route_id))
    route = f"{d.route_id:<{width}}"
    if use_color:
        r, g, b = line_rgb(d.route_id)
        # Single combined SGR sequence = one fewer escape pair per row.
        route = f"\033[1;38;2;{r};{g};{b}m{route}\033[0m"

    return f"  {route}  {direction_letter(d.direction)}   {d.minutes_away:>2}   {nxt:>2}"


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------

# Shared HTTP session: one TCP+TLS connection stays warm between polls, which
# is the single biggest win on a Pi Zero 2 W where TLS handshakes are costly.
# `requests` keeps a connection pool per host when you reuse a Session.
_SESSION = requests.Session()
_SESSION.headers.update({
    "Accept-Encoding": "gzip, deflate",  # MTA gzips feeds; saves bandwidth + CPU.
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
# One long-lived pool, sized to the number of feeds (3). Reusing it avoids
# spinning up and tearing down threads on every refresh — meaningful on a
# 512 MB Pi. Threads are daemonic by default in ThreadPoolExecutor.
_FEED_POOL = ThreadPoolExecutor(max_workers=len(_FEED_KEYS), thread_name_prefix="mta-feed")


def fetch_all_subway_feeds(timeout: float = 10.0) -> dict[str, gtfs_realtime_pb2.FeedMessage]:
    """Download every subway feed we care about, in parallel (I/O-bound).

    Each feed is a separate HTTPS call; fetching them concurrently collapses
    total wall time to roughly ``max(feed latency)`` instead of the sum.
    """
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
            # Parent-stop check inlined to avoid a function call per update.
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


# ---------------------------------------------------------------------------
# CLI preview
# ---------------------------------------------------------------------------

def print_departures(results: dict[str, list[Departure]]) -> None:
    """Print one colored row per departure, grouped by station section."""
    for group_name, deps in results.items():
        print(f"\n=== {group_name} ===")
        if not deps:
            print("  (no upcoming departures in feed)")
            continue
        print("  line   dir   1st  2nd")
        for d in deps:
            print(format_departure_terminal(d))


if __name__ == "__main__":
    print("Fetching live MTA GTFS-Realtime data...")
    try:
        print_departures(get_all_departures())
    except requests.RequestException as exc:
        print(f"Network error talking to MTA: {exc}")
        raise SystemExit(1)
