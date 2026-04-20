"""
mta_api.py
----------
Fetches live NYC subway departure times from the MTA's free GTFS-Realtime API
and returns them as clean Python data structures.

HOW THE MTA API WORKS (the important concepts):

1. GTFS-Realtime is a transit data standard built on top of Google Protocol
   Buffers ("protobuf"). It is NOT JSON. Protobuf is a compact binary format,
   so we need a decoder library (`gtfs-realtime-bindings`) that knows how to
   turn those bytes into Python objects.

2. The MTA splits its realtime data into multiple "feeds", one per group of
   related lines. We only download the feeds we actually need:
       - A/C/E       -> FEED_ACE
       - N/Q/R/W     -> FEED_NQRW
       - 1/2/3/4/5/6/7 (numbered lines) -> FEED_NUMBERED
   Each feed is a single HTTPS URL that returns the full current snapshot of
   predicted arrivals for every train on that line group.

3. A feed is organized into "entities". The entity type we care about is
   `trip_update`. Each trip_update represents one train (a single run),
   and it contains an ordered list of `stop_time_update` items — one per
   station that train will visit, with a predicted arrival/departure time.

4. To find "when is the next C train leaving 50 St", we:
       a. Download the ACE feed
       b. Walk through every trip_update
       c. For each trip, look at its stop_time_update list
       d. Keep only the entries whose stop_id matches our station
       e. Convert the POSIX timestamp into "minutes from now"
       f. Sort by soonest

5. Stop IDs in the MTA GTFS feed use a letter/number base plus a direction
   suffix:
       - "A25"  = the station 50 St on the A/C/E line (parent stop)
       - "A25N" = northbound (uptown) platform
       - "A25S" = southbound (downtown) platform
   Realtime feeds reference the platform-specific IDs (A25N, A25S), so we
   strip the suffix to detect direction.

NO API KEY REQUIRED — the MTA made these feeds fully public in 2021.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Iterable

import requests
from google.transit import gtfs_realtime_pb2


# ---------------------------------------------------------------------------
# Feed URLs
# ---------------------------------------------------------------------------
# The path segment after "gtfs" tells the MTA which subset of lines to return.
# Leaving it blank (just "/nyct%2Fgtfs") returns the numbered-lines feed.
# "%2F" is just a URL-encoded forward slash ("/").

FEED_BASE = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds"

FEED_URLS: dict[str, str] = {
    "ACE":       f"{FEED_BASE}/nyct%2Fgtfs-ace",
    "NQRW":      f"{FEED_BASE}/nyct%2Fgtfs-nqrw",
    "NUMBERED":  f"{FEED_BASE}/nyct%2Fgtfs",
}

# Predictions with minutes_away below this are dropped: "0 min" usually means
# under a minute away, which isn't actionable for getting to the platform.
MIN_MINUTES_ACHIEVABLE = 1


# ---------------------------------------------------------------------------
# Station / line configuration
# ---------------------------------------------------------------------------
# Each entry says: "for this group name, look at THIS MTA feed, filter to
# trip_updates whose stop_time_updates include any of these stop_ids, and
# keep only trains on these routes".
#
# stop_ids here are the PARENT stop IDs (no N/S suffix). We match both
# directions by stripping the last character off the feed's stop_id.

@dataclass(frozen=True)
class StationGroup:
    name: str                  # short label we'll show, e.g. "ACE @ 50 St"
    feed_key: str              # which feed in FEED_URLS to download
    stop_ids: tuple[str, ...]  # parent stop IDs to match (both directions)
    routes: tuple[str, ...]    # which route_ids count (e.g. A, C, E)


STATION_GROUPS: list[StationGroup] = [
    StationGroup(
        name="ACE @ 50 St",
        feed_key="ACE",
        stop_ids=("A25",),
        routes=("A", "C", "E"),
    ),
    StationGroup(
        name="1/2 @ 50 St",
        feed_key="NUMBERED",
        # 50 St on the 1 line is stop 120. The 2 does not stop here, but we
        # keep "2" in the allowed routes so you can later add a nearby
        # express stop (e.g. Times Sq "127") without changing the filter.
        stop_ids=("120",),
        routes=("1", "2"),
    ),
    StationGroup(
        name="NRQW @ 49 St",
        feed_key="NQRW",
        # 49 St is R16. Served by N/R/W. Q runs express via 7 Ave and does
        # not stop here, but we include it in routes so a future stop can be
        # added without refactoring.
        stop_ids=("R16",),
        routes=("N", "Q", "R", "W"),
    ),
]


# ---------------------------------------------------------------------------
# The normalized shape we return to the rest of the app
# ---------------------------------------------------------------------------
# Everything above this line deals with the MTA's raw data model. Everything
# below deals with OUR simplified model. Keeping these separate means the
# simulator and main.py never have to know what "trip_update" means.

@dataclass(frozen=True)
class Departure:
    route_id: str      # "A", "C", "1", "N", etc.
    stop_id: str       # platform-level id e.g. "A25N"
    direction: str     # "N" (uptown) or "S" (downtown)
    arrival_epoch: int # POSIX timestamp (seconds) when train arrives
    minutes_away: int  # convenience: how many whole minutes from "now"
    # Minutes until the *following* train at this station on the same route
    # and direction (from the live feed). None if no later prediction exists.
    next_train_minutes: int | None = None


# ---------------------------------------------------------------------------
# Low-level fetch
# ---------------------------------------------------------------------------

def fetch_feed(feed_key: str, timeout: float = 10.0) -> gtfs_realtime_pb2.FeedMessage:
    """Download one MTA feed and decode the protobuf bytes into an object.

    Steps:
        1. HTTP GET the feed URL (returns binary protobuf, not JSON).
        2. Create an empty FeedMessage object (the protobuf schema class).
        3. Let it parse the raw bytes into nested Python-accessible fields.

    Raises requests.HTTPError on HTTP failure, and protobuf DecodeError on
    malformed data.
    """
    url = FEED_URLS[feed_key]
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    return feed


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _direction_from_stop_id(stop_id: str) -> str:
    """Return 'N' or 'S' from a platform-level stop id like 'A25N'.

    If the id does not end in N or S we return '?'. Realtime feeds should
    always include the suffix, but defensive code keeps us safe.
    """
    if stop_id.endswith("N") or stop_id.endswith("S"):
        return stop_id[-1]
    return "?"


def _parent_stop(stop_id: str) -> str:
    """Strip direction suffix: 'A25N' -> 'A25'. Leaves unsuffixed ids alone."""
    if stop_id and stop_id[-1] in ("N", "S"):
        return stop_id[:-1]
    return stop_id


def extract_departures(
    feed: gtfs_realtime_pb2.FeedMessage,
    parent_stop_ids: Iterable[str],
    routes: Iterable[str],
    now_epoch: int | None = None,
) -> list[Departure]:
    """Walk a decoded feed and pull out departures matching our filters.

    The feed contains many `entity` objects. We only care about the ones that
    have a `trip_update` (i.e. a running train with predicted stop times).

    For each matching stop_time_update we build a `Departure`. We skip any
    that are in the past (already-departed trains still appear in the feed
    for a short while).

    We also drop departures with minutes_away < MIN_MINUTES_ACHIEVABLE so the
    first row and "next train" column reflect trips you can still catch.
    """
    if now_epoch is None:
        now_epoch = int(time.time())

    allowed_stops = set(parent_stop_ids)
    allowed_routes = set(routes)
    departures: list[Departure] = []

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        trip_update = entity.trip_update
        route_id = trip_update.trip.route_id
        if route_id not in allowed_routes:
            continue

        for stu in trip_update.stop_time_update:
            if _parent_stop(stu.stop_id) not in allowed_stops:
                continue

            # Prefer the departure time; fall back to arrival time if absent.
            # Both are protobuf sub-messages — a 0 timestamp means "not set".
            when = 0
            if stu.HasField("departure") and stu.departure.time:
                when = stu.departure.time
            elif stu.HasField("arrival") and stu.arrival.time:
                when = stu.arrival.time

            if when <= now_epoch:
                continue  # train already left / stale prediction

            departures.append(
                Departure(
                    route_id=route_id,
                    stop_id=stu.stop_id,
                    direction=_direction_from_stop_id(stu.stop_id),
                    arrival_epoch=when,
                    minutes_away=max(0, (when - now_epoch) // 60),
                )
            )

    departures.sort(key=lambda d: d.arrival_epoch)
    return [d for d in departures if d.minutes_away >= MIN_MINUTES_ACHIEVABLE]


def limit_per_route_and_direction(
    departures: list[Departure],
    per_slot: int,
) -> list[Departure]:
    """Keep the soonest `per_slot` departures for each (route_id, direction).

    Example: two uptown C trains at 3 min and 7 min — both are (C, N); with
    per_slot=2 you keep both rows. With per_slot=1 (default at call sites) you
    keep only the 3 min train; use `next_train_minutes` for the 7 min follow-up.

    Rows are returned sorted by route_id, direction, then time so they read
    like a small table (A/C/E blocks, then 1/2, etc. within each group
    you already split by station elsewhere).
    """
    if per_slot < 1:
        raise ValueError("per_slot must be at least 1")

    buckets: dict[tuple[str, str], list[Departure]] = {}
    # `departures` is sorted by time globally; append in that order per key
    # so the first N per bucket are the soonest N.
    for d in departures:
        key = (d.route_id, d.direction)
        lst = buckets.setdefault(key, [])
        if len(lst) < per_slot:
            lst.append(d)

    out: list[Departure] = []
    for key in sorted(buckets.keys(), key=lambda k: (k[0], k[1])):
        out.extend(buckets[key])
    return out


def annotate_next_train_minutes(
    capped: list[Departure],
    full: list[Departure],
) -> list[Departure]:
    """Fill `next_train_minutes` using the chronology of `full` per route+dir.

    Each row in `capped` is a `Departure` instance also present in `full`.
    The following train on the same route and direction is the next entry
    in `full`'s sorted list for that key; we store its `minutes_away`.

    Using `full` (not just `capped`) means the 2nd displayed row can still
    show when the 3rd train is due, even though that 3rd train has no row.
    """
    by_rd: dict[tuple[str, str], list[Departure]] = {}
    for d in full:
        by_rd.setdefault((d.route_id, d.direction), []).append(d)
    for seq in by_rd.values():
        seq.sort(key=lambda x: x.arrival_epoch)

    out: list[Departure] = []
    for d in capped:
        seq = by_rd[(d.route_id, d.direction)]
        try:
            idx = seq.index(d)
        except ValueError:
            idx = -1
        nxt: int | None = None
        if idx >= 0 and idx + 1 < len(seq):
            nxt = seq[idx + 1].minutes_away
        out.append(replace(d, next_train_minutes=nxt))
    return out


# ---------------------------------------------------------------------------
# High-level API used by main.py and the simulator
# ---------------------------------------------------------------------------

def get_departures_for_group(
    group: StationGroup,
    *,
    per_route_direction: int = 1,
) -> list[Departure]:
    """Fetch the group's feed and return departures capped per route+direction.

    `per_route_direction` is how many rows to show per (route, uptown/downtown)
    pair. Default 1 = only the soonest train; `next_train_minutes` still comes
    from the feed for the train after that.
    """
    feed = fetch_feed(group.feed_key)
    full = extract_departures(feed, group.stop_ids, group.routes)
    capped = limit_per_route_and_direction(full, per_route_direction)
    return annotate_next_train_minutes(capped, full)


def get_all_departures(
    *,
    per_route_direction: int = 1,
) -> dict[str, list[Departure]]:
    """Convenience: fetch every configured group and return a dict keyed by
    group name. Each feed is downloaded only once even if multiple groups
    share a feed (tiny optimization; currently every group uses a unique
    feed, but this future-proofs the code).
    """
    # Cache: feed_key -> parsed FeedMessage
    feed_cache: dict[str, gtfs_realtime_pb2.FeedMessage] = {}
    results: dict[str, list[Departure]] = {}

    for group in STATION_GROUPS:
        if group.feed_key not in feed_cache:
            feed_cache[group.feed_key] = fetch_feed(group.feed_key)
        feed = feed_cache[group.feed_key]
        full = extract_departures(feed, group.stop_ids, group.routes)
        capped = limit_per_route_and_direction(full, per_route_direction)
        results[group.name] = annotate_next_train_minutes(capped, full)

    return results


# ---------------------------------------------------------------------------
# CLI: run `python mta_api.py` to verify Step 1 works end-to-end
# ---------------------------------------------------------------------------

def _print_departures(results: dict[str, list[Departure]]) -> None:
    """Pretty-print in a format that also serves as a learning checkpoint."""
    for group_name, deps in results.items():
        print(f"\n=== {group_name} ===")
        if not deps:
            print("  (no upcoming departures in feed)")
            continue
        hdr = f"{'Rt':<3} {'Dir':<9} {'Min':>3}  {'Next trn':>8}  Our plat"
        print(f"  {hdr}")
        print(f"  {'-' * len(hdr)}")
        for d in deps:
            dir_word = {"N": "uptown", "S": "downtown"}.get(d.direction, d.direction)
            if d.next_train_minutes is None:
                nxt = "   —"
            else:
                nxt = f"{d.next_train_minutes:>3} min"
            print(
                f"  {d.route_id:<3} {dir_word:<9} {d.minutes_away:>3}  "
                f"{nxt:>8}  {d.stop_id}"
            )


if __name__ == "__main__":
    print("Fetching live MTA GTFS-Realtime data...")
    try:
        results = get_all_departures()
    except requests.RequestException as exc:
        print(f"Network error talking to MTA: {exc}")
        raise SystemExit(1)

    _print_departures(results)