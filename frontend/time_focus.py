"""
time_focus.py
-------------
Time-of-day focus rules: when the sign should *prioritise* a fixed
"morning commute" trio over normal page cycling.

Window:
  * Mon-Fri 7:00 - 12:00 local time   -> commute focus active
  * All other times                    -> normal cycling

Static commute trio (same order shown every time):

  * NRW  — merged N/R/W uptown (soonest departure on that platform)
  * E    — uptown @ 50 St / 8 Av
  * M50  — eastbound from the configured nearest stop

The backend stays flat JSON; this module pulls the three departures only.
No clock imports — caller passes weekday/hour/minute (CPython + CircuitPython).

Edit the constants below to retarget stops/lines/directions.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Time-window rule
# ---------------------------------------------------------------------------

WEEKDAY_START_HOUR = 7
WEEKDAY_END_HOUR = 12


def is_commute_window(weekday: int, hour: int, minute: int) -> bool:
    """Mon-Fri 7:00-12:00 inclusive. weekday: 0=Mon..6=Sun."""
    if weekday > 4:
        return False
    if hour < WEEKDAY_START_HOUR or hour > WEEKDAY_END_HOUR:
        return False
    if hour == WEEKDAY_END_HOUR and minute > 0:
        return False
    return True


# ---------------------------------------------------------------------------
# Row configuration
# ---------------------------------------------------------------------------

NRW_MERGE_LINES = ("N", "R", "W")
NRW_MERGE_DIR = "U"
NRW_LABEL = "NRW"

COMMUTE_E_LINE = "E"
COMMUTE_E_DIR = "U"

COMMUTE_BUS_LINE = "M50"
COMMUTE_BUS_DIR = "Eastbound"

# Passed to layouts as `commute_board` — one static board, no A/B flipping.
COMMUTE_BOARD_STATIC = "static"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _soonest(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.get("min") if r.get("min") is not None else 999)
    return candidates[0]


def commute_static_rows(payload: dict) -> list[dict]:
    """Up to three rows: NRW (merged uptown), E uptown, M50 east. Omits rows
    with no matching data in `payload`; order fixed."""
    rows: list[dict] = []

    nrw_pool = [
        r
        for r in payload.get("trains", [])
        if r.get("line") in NRW_MERGE_LINES and r.get("dir") == NRW_MERGE_DIR
    ]
    nrw = _soonest(nrw_pool)
    if nrw is not None:
        rows.append(dict(nrw, line=NRW_LABEL))

    e_candidates = [
        r
        for r in payload.get("trains", [])
        if r.get("line") == COMMUTE_E_LINE and r.get("dir") == COMMUTE_E_DIR
    ]
    e_row = _soonest(e_candidates)
    if e_row is not None:
        rows.append(e_row)

    m50_candidates = [
        r
        for r in payload.get("buses", [])
        if r.get("line") == COMMUTE_BUS_LINE and r.get("dir") == COMMUTE_BUS_DIR
    ]
    m50_row = _soonest(m50_candidates)
    if m50_row is not None:
        rows.append(m50_row)

    return rows
