"""Where the season stands on a given day, read from the reports alone.

The season is not a date range (CONTEXT.md). It opens at the first report of the winter,
counted or not counted. It closes after a run of silent weekdays once March has begun.
Unreported weekends are normal, so weekends never count as silence.

Each winter is anchored at 1 August: a report after July belongs to the next season. Four
states follow:

- awaiting: no report since the anchor, and the latest plausible start has not passed.
- overdue: no report since the anchor, and the latest plausible start has passed. The
  source has gone quiet, which is an incident.
- open: the first report has arrived and the season has not closed.
- closed: SEASON_CLOSE_SILENT_WEEKDAYS weekdays passed without a report, all of them on or
  after SEASON_CLOSE_NOT_BEFORE.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import duckdb

from roll_call import config
from roll_call.quality.store import table_exists

AWAITING = "awaiting"
OPEN = "open"
OVERDUE = "overdue"
CLOSED = "closed"

# A report after July belongs to the coming winter.
SEASON_ANCHOR = (8, 1)  # month, day


@dataclass(frozen=True)
class SeasonState:
    """The season on one day. `winter` is the year the season starts in (2025 for 2025-26)."""

    status: str
    winter: int
    opened_on: date | None = None
    closed_on: date | None = None
    last_report: date | None = None
    latest_plausible_start: date | None = None

    @property
    def is_open(self) -> bool:
        return self.status == OPEN


def season_anchor(today: date) -> date:
    """1 August of the winter that `today` belongs to."""
    month, day = SEASON_ANCHOR
    year = today.year if (today.month, today.day) >= (month, day) else today.year - 1
    return date(year, month, day)


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def report_dates(con: duckdb.DuckDBPyConnection, start: date, end: date) -> list[date]:
    """Days from start to end (inclusive) with a counted or not-counted report, in order."""
    table = config.TABLES.counts_daily
    if not table_exists(con, table):
        return []
    rows = con.execute(
        f"""
        SELECT DISTINCT report_date FROM {table}
        WHERE report_date BETWEEN ? AND ?
          AND (count_researchers IS NOT NULL OR count_park IS NOT NULL OR not_counted)
        ORDER BY report_date
        """,
        [start, end],
    ).fetchall()
    return [r[0] for r in rows]


def _silence_closes(after: date, before: date, not_before: date) -> date | None:
    """The weekday on which silence closes the season, or None.

    Counts weekdays strictly between two days (`after` is the last report, `before` the next
    report or today). Only weekdays on or after `not_before` count. Today is left out: its
    report may still arrive later in the day.
    """
    needed = config.SEASON_CLOSE_SILENT_WEEKDAYS
    silent = 0
    d = max(after + timedelta(days=1), not_before)
    while d < before:
        if is_weekday(d):
            silent += 1
            if silent >= needed:
                return d
        d += timedelta(days=1)
    return None


def state(con: duckdb.DuckDBPyConnection, today: date) -> SeasonState:
    """The season's state on `today`, from the reports stored so far."""
    anchor = season_anchor(today)
    winter = anchor.year
    lps = date(winter, *config.LATEST_PLAUSIBLE_START)
    reports = report_dates(con, anchor, today)
    if not reports:
        status = AWAITING if today <= lps else OVERDUE
        return SeasonState(status, winter, latest_plausible_start=lps)

    not_before = date(winter + 1, *config.SEASON_CLOSE_NOT_BEFORE)
    opened_on = reports[0]
    # Walk each gap in order. The first long enough silence closes the season; reports after
    # it do not reopen the same winter.
    for last, nxt in zip(reports, reports[1:] + [today]):
        closed_on = _silence_closes(last, nxt, not_before)
        if closed_on is not None:
            return SeasonState(CLOSED, winter, opened_on, closed_on, last, lps)
    return SeasonState(OPEN, winter, opened_on, None, reports[-1], lps)


def is_open(con: duckdb.DuckDBPyConnection, today: date) -> bool:
    """Whether a season is open on `today`. Predictions run only then."""
    return state(con, today).is_open
