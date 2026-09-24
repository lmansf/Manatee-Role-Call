"""One-off: backfill weather history from the Open-Meteo archive.

    .venv/bin/python jobs/backfill_weather.py 1988 2025
    .venv/bin/python jobs/backfill_weather.py 1988 2025 --pause 2

Feeds two things: training features for the historical seasons (2018-19 onward), and the
weather baseline, which is the normal for each day of the year over the trailing 30 years
(spec §5). The replayed baseline refreshes need history 30 years before the earliest replayed
year, so backfill from about 1988.

One archive request per calendar year keeps each raw payload small. Each year is written
through db.write_ingest_result like any other run, daily and hourly rows together, so a
re-run replaces rows instead of duplicating them. A year that fails is recorded as a failed
run and logged, and the loop moves on to the next year. Re-run just the failed years after.

The field set is fixed (open_meteo.DAILY_FIELDS, HOURLY_FIELDS). Changing it later means
re-backfilling, which is fine but should be a conscious act.
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from roll_call import config
from roll_call.ingest import open_meteo
from roll_call.storage import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("roll_call.jobs.backfill_weather")

# Seconds between requests. Open-Meteo is free and asks for fair use.
DEFAULT_PAUSE_S = 1.0


def year_window(year: int, today: date) -> tuple[date, date] | None:
    """1 January to 31 December, cut at yesterday for the current year. None for a future year."""
    start, end = date(year, 1, 1), min(date(year, 12, 31), today - timedelta(days=1))
    return (start, end) if start <= end else None


def backfill(con, start_year: int, end_year: int, today: date | None = None,
             pause: float = DEFAULT_PAUSE_S) -> list[int]:
    """Fetch and write each year from start_year to end_year inclusive. Returns the failed years."""
    if start_year > end_year:
        raise ValueError(f"start year {start_year} is after end year {end_year}")
    today = today or datetime.now(ZoneInfo(config.TIMEZONE)).date()
    failed: list[int] = []
    for i, year in enumerate(range(start_year, end_year + 1)):
        window = year_window(year, today)
        if window is None:
            log.warning("%d is in the future; skipped", year)
            continue
        if i and pause:
            time.sleep(pause)
        start, end = window
        try:
            result = db.fetch_and_write(con, open_meteo.SOURCE_NAME,
                                        lambda: open_meteo.fetch_archive(start, end),
                                        db.write_archive_result)
            log.info("%d: %d daily rows", year, len(result.records))
        except Exception:  # noqa: BLE001 - one bad year must not stop the rest
            log.exception("%d failed", year)
            failed.append(year)
    return failed


def main(start_year: int, end_year: int, db_path: str | None = None,
         pause: float = DEFAULT_PAUSE_S) -> int:
    """Backfill the years into the database. Returns 1 if any year failed, else 0."""
    con = db.connect(db_path)
    try:
        failed = backfill(con, start_year, end_year, pause=pause)
    finally:
        con.close()
    if failed:
        log.error("failed years: %s", ", ".join(map(str, failed)))
        return 1
    return 0


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("start_year", type=int)
    parser.add_argument("end_year", type=int)
    parser.add_argument("--pause", type=float, default=DEFAULT_PAUSE_S,
                        help=f"seconds between yearly requests (default {DEFAULT_PAUSE_S})")
    args = parser.parse_args(argv)
    return main(args.start_year, args.end_year, pause=args.pause)


if __name__ == "__main__":
    raise SystemExit(cli())
