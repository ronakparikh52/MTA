"""
time_schedule.py
----------------
Chooses which screen the sign opens on by default and computes the Bloomberg
commute rows. Everything else (HTTP, parsing, formatting) lives in `mta_api`.

Rules:
  * Mon-Fri 7:00-12:00 America/New_York  -> default = `bloomberg_commute`.
  * Other times                          -> default = `default_scroll`.

The Bloomberg commute screen uses the same home stops as the default board:
  * E trains @ 50 St / 8 Av  (parent stop A25)
  * N/R/W trains merged @ 49 St / 7 Av (parent stop R16)
Uptown only — direction suffix "N".
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from google.transit import gtfs_realtime_pb2

from mta_api import (
    Departure,
    extract_departures,
    fetch_all_subway_feeds,
    format_departure_terminal,
    get_all_departures_from_feed_cache,
    print_departures,
)

NY_TZ = ZoneInfo("America/New_York")

BLOOMBERG_WEEKDAY_START = time(7, 0)
BLOOMBERG_WEEKDAY_END = time(12, 0)  # inclusive through 12:00:00

SuggestedFocus = Literal["bloomberg_commute", "default_scroll"]


@dataclass(frozen=True)
class ScheduleBundle:
    """One refresh worth of data for every UI mode."""
    suggested: SuggestedFocus
    when_local: datetime
    default_groups: dict[str, list[Departure]]
    bloomberg_rows: list[Departure]


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _to_ny(dt: datetime | None) -> datetime:
    """Coerce `dt` to America/New_York; use `now` if None; assume NY if naive."""
    if dt is None:
        return datetime.now(NY_TZ)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=NY_TZ)
    return dt.astimezone(NY_TZ)


def suggested_focus(now_local: datetime | None = None) -> SuggestedFocus:
    """Pick the default screen for the given (or current) NY-local time."""
    now = _to_ny(now_local)
    in_window = BLOOMBERG_WEEKDAY_START <= now.time() <= BLOOMBERG_WEEKDAY_END
    is_weekday = now.weekday() < 5  # Mon-Fri
    return "bloomberg_commute" if (is_weekday and in_window) else "default_scroll"


# ---------------------------------------------------------------------------
# Bloomberg commute screen
# ---------------------------------------------------------------------------

# (feed_key, parent_stops, routes, display_route_id) for each Bloomberg row.
_BLOOMBERG_SOURCES: tuple[tuple[str, tuple[str, ...], tuple[str, ...], str], ...] = (
    ("ACE",  ("A25",), ("E",),                "E"),    # 50 St / 8 Av
    ("NQRW", ("R16",), ("N", "R", "W"),       "NRW"),  # 49 St / 7 Av (no Q)
)


def _aggregate_uptown(
    full: list[Departure],
    display_route_id: str,
) -> Departure | None:
    """Return one uptown row (soonest + next soonest) across all given routes.

    `full` is assumed sorted ascending by `arrival_epoch`. Does a single pass
    and stops after finding the first two uptown departures.
    """
    first: Departure | None = None
    for d in full:
        if d.direction != "N":
            continue
        if first is None:
            first = d
        else:
            return replace(first, route_id=display_route_id, next_train_minutes=d.minutes_away)
    if first is None:
        return None
    return replace(first, route_id=display_route_id, next_train_minutes=None)


def get_bloomberg_commute_rows(
    feed_cache: dict[str, gtfs_realtime_pb2.FeedMessage],
) -> list[Departure]:
    """Uptown E @ A25 + uptown combined N/R/W @ R16 (two times per line)."""
    rows: list[Departure] = []
    for feed_key, stops, routes, label in _BLOOMBERG_SOURCES:
        deps = extract_departures(feed_cache[feed_key], stops, routes)
        row = _aggregate_uptown(deps, label)
        if row is not None:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# One-shot bundle (used by UIs / main loop)
# ---------------------------------------------------------------------------

def get_schedule_bundle(
    *,
    now_local: datetime | None = None,
    per_route_direction: int = 1,
    feed_cache: dict[str, gtfs_realtime_pb2.FeedMessage] | None = None,
) -> ScheduleBundle:
    """Fetch once (or reuse `feed_cache`) and return data for every screen."""
    when = _to_ny(now_local)
    if feed_cache is None:
        feed_cache = fetch_all_subway_feeds()

    return ScheduleBundle(
        suggested=suggested_focus(when),
        when_local=when,
        default_groups=get_all_departures_from_feed_cache(
            feed_cache, per_route_direction=per_route_direction,
        ),
        bloomberg_rows=get_bloomberg_commute_rows(feed_cache),
    )


# ---------------------------------------------------------------------------
# CLI preview
# ---------------------------------------------------------------------------

def _print_bloomberg(rows: list[Departure]) -> None:
    print("\n=== Bloomberg commute (E @ 50 St/8 Av, NRW @ 49 St) ===")
    if not rows:
        print("  (no upcoming departures)")
        return
    print("  line   dir   1st  2nd")
    for d in rows:
        print(format_departure_terminal(d))


if __name__ == "__main__":
    import sys

    try:
        bundle = get_schedule_bundle()
    except Exception as exc:  # pragma: no cover - CLI safety net
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"When (NY): {bundle.when_local.isoformat()}")
    print(f"Suggested default screen: {bundle.suggested}")
    _print_bloomberg(bundle.bloomberg_rows)
    print_departures(bundle.default_groups)
