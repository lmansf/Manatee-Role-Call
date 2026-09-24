"""Daily run: ingest every source, catching up from each one's last successful run.

Started by the systemd user timer at 19:00 (docs/scheduling.md), or by hand:

    .venv/bin/python jobs/ingest_daily.py

Each source is wrapped separately so one failing doesn't stop the others, and every failure
still leaves an `ingest_runs` row (IngestRun.fail + db.record_run). The run exits non-zero if
any source failed, so systemd marks it failed and retries (the service has Restart=on-failure).

TODO(you): implement main(). The shape:

    con = db.connect()
    for each source:
        since = db.last_successful_run(con, SOURCE_NAME)    # None on the very first run
        window = from `since` (minus an overlap) to today
        result = <source>.fetch_...(window)
        db.write_ingest_result(con, result, raw_table, parsed_table)

Decisions to make as you write it:
  - The overlap. The Open-Meteo archive fills in ~5 days late, and USGS values get revised
    from provisional to approved. How far back should each window reach so late and revised
    data are picked up? The idempotency choice in db.write_ingest_result decides whether
    overlap is safe.
  - The first run. `since` is None. Fetch from the season start? A fixed date? The weather
    history belongs to jobs/backfill_weather.py, not here.
  - The forecast has no window: fetch today's issue. A missed day's issue is lost, unless
    Open-Meteo's forecast history (docs/sources.md §1c) can fill it.
  - Order: counts, weather archive, forecast, gauge. Then (stage 2) the checks and the alert
    email. The last step publishes the dashboard, passing the open connection:
    `publish_dashboard.publish(con)`. It exports the summary CSVs, then commits and pushes them.
    Decide whether a failed publish should fail the run.
"""
from __future__ import annotations

import logging

from roll_call import config  # noqa: F401  (loads .env)
from roll_call.ingest import blue_spring, open_meteo, usgs_gauge  # noqa: F401
from roll_call.storage import db  # noqa: F401
import publish_dashboard  # noqa: F401  (jobs/publish_dashboard.py; this folder is on sys.path)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("roll_call.jobs.ingest_daily")


def main() -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
