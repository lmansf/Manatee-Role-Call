"""One-off: replay the yearly baseline refresh for past seasons, so the drift history starts
where the data does.

    .venv/bin/python jobs/replay_baselines.py            # 2019 through the last closed season
    .venv/bin/python jobs/replay_baselines.py 2019 2025

Run it once, after jobs/backfill_weather.py has loaded the weather history. Each replayed year
refreshes on 1 April using only data from before that date, and is labelled replayed, so the
dashboard's drift view can tell it from a live refresh. Years that already have a live refresh
are left alone, so running it again is safe.

Run it while the daily timer is idle: DuckDB allows one writer at a time.
"""
from __future__ import annotations

import argparse
import logging
from datetime import date

from roll_call.quality import baselines
from roll_call.storage import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("roll_call.jobs.replay_baselines")

# The first season with extracted counts is 2018-19, whose refresh falls in spring 2019.
FIRST_YEAR = 2019


def last_closed_year(today: date) -> int:
    """The latest year whose nominal refresh date has passed."""
    return today.year if today >= baselines.replay_date(today.year) else today.year - 1


def main(argv: list[str] | None = None, db_path: str | None = None, today: date | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("first_year", type=int, nargs="?", default=FIRST_YEAR)
    parser.add_argument("last_year", type=int, nargs="?", default=None)
    args = parser.parse_args(argv)
    last = args.last_year or last_closed_year(today or date.today())
    if args.first_year > last:
        parser.error(f"first year {args.first_year} is after last year {last}")
    con = db.connect(db_path)
    try:
        done = baselines.replay(con, args.first_year, last)
    finally:
        con.close()
    log.info("replayed %d refreshes: %s", len(done), ", ".join(d.isoformat() for d in done) or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
