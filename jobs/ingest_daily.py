"""Daily run: ingest every source with catch-up, then check, alert, retrain, predict and publish.

Started by the systemd user timer at 19:00 (docs/scheduling.md), or by hand:

    .venv/bin/python jobs/ingest_daily.py
    .venv/bin/python jobs/ingest_daily.py --today 2026-01-15   # run as of another day

Stage order (spec §1 Flow): counts, weather archive, forecast, gauge, then `run_checks`,
`send_alert`, `refresh_if_due`, `retrain_if_needed`, `predict.run`, and last
`publish_dashboard.publish(con)`.

Each stage runs in its own try/except. A failing stage is logged and the later stages still
run. The process exits 1 if any stage failed, so systemd marks the run failed and retries it
(the service has Restart=on-failure). Every table is keyed, so a retry is safe. A failed
publish fails the run too: a dashboard that silently stopped updating is what the staleness
warning exists to catch, and the retry often fixes a network blip.

Catch-up: each source fetches from its last successful run (as a local date) minus an
overlap, up to today. The overlap re-reads data that arrives late or gets revised:

  - Archive: 10 days. ERA5 lags about 5 days, so the last few days of every window come
    back empty. The overlap reaches past the lag and fills them on a later run.
  - Gauge: 14 days. USGS revises provisional values; approved rows get their own key.
  - Counts: 7 days. A report can be posted days after its roll call, such as a Friday
    report posted on Monday.

On the first run a source has no success to start from. Counts start at the current season
year (1 July), so the whole current season is read. The archive and the gauge start 30 days
back: their history belongs to jobs/backfill_weather.py and a separate gauge backfill.

The forecast has no window. Each run stores today's issue. A missed day's issue is lost.

Interfaces from other packages (quality, model, publish) are imported inside their stage, so
this file imports cleanly while those packages are being built, and tests can fake them.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from roll_call import config  # loads .env
from roll_call.ingest import blue_spring, open_meteo, usgs_gauge
from roll_call.storage import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("roll_call.jobs.ingest_daily")

T = config.TABLES

# Days of overlap before the last successful run. See the module docstring.
COUNTS_OVERLAP_DAYS = 7
ARCHIVE_OVERLAP_DAYS = 10
GAUGE_OVERLAP_DAYS = 14

# Where a source with no successful run starts.
SEASON_YEAR_START = (7, 1)  # month, day: the quiet middle of summer, between two seasons
FIRST_RUN_LOOKBACK_DAYS = 30  # archive and gauge

# Source names, as the fetchers record them (swarm contract).
COUNTS_SOURCE = "blue_spring_counts"
ARCHIVE_SOURCE = "open_meteo_archive"
FORECAST_SOURCE = "open_meteo_forecast"
GAUGE_SOURCE = "usgs_gauge_daily"


def local_today() -> date:
    """Today's date in Florida. A run just after midnight UTC is still the day before there."""
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def local_date(ts_utc: datetime) -> date:
    """The Florida date of a naive UTC timestamp, as stored in ingest_runs."""
    return ts_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(config.TIMEZONE)).date()


def season_year_start(today: date) -> date:
    """The start of the season year that holds `today`: the latest 1 July on or before it.

    A season runs from late autumn into March, so 1 July falls between two seasons.
    """
    month, day = SEASON_YEAR_START
    start = date(today.year, month, day)
    return start if start <= today else date(today.year - 1, month, day)


def catch_up_start(last_success: datetime | None, overlap_days: int, first_run_start: date,
                   today: date | None = None) -> date:
    """First day to fetch: the last success's local date minus the overlap, or
    `first_run_start` when the source has never succeeded.

    The last success is a wall-clock time. When the run is for an earlier day (`--today` in the
    past), that time lies after `today`, so the window is measured from `today` instead.
    Otherwise a re-run of a past day would fetch nothing.
    """
    if last_success is None:
        return first_run_start
    anchor = local_date(last_success)
    if today is not None and anchor > today:
        anchor = today
    return anchor - timedelta(days=overlap_days)


def counts_window_start(con, today: date) -> date:
    return catch_up_start(db.last_successful_run(con, COUNTS_SOURCE),
                          COUNTS_OVERLAP_DAYS, season_year_start(today), today)


def archive_window(con, today: date) -> tuple[date, date]:
    """The archive's window ends yesterday, the last complete day."""
    first = today - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
    start = catch_up_start(db.last_successful_run(con, ARCHIVE_SOURCE), ARCHIVE_OVERLAP_DAYS, first,
                           today)
    return start, today - timedelta(days=1)


def gauge_window(con, today: date) -> tuple[date, date]:
    first = today - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
    start = catch_up_start(db.last_successful_run(con, GAUGE_SOURCE), GAUGE_OVERLAP_DAYS, first, today)
    return start, today


def _write_to(raw_table: str, parsed_table: str) -> Callable:
    return lambda con, result: db.write_ingest_result(con, result, raw_table, parsed_table)


@dataclass
class RunState:
    """What one stage hands to a later one."""

    today: date
    check_report: Any = None
    failed: list[str] = field(default_factory=list)


# Stages. Each takes the open connection and the run state, and raises on failure.

def ingest_counts(con, state: RunState) -> None:
    since = counts_window_start(con, state.today)
    log.info("counts: reports since %s", since)
    db.fetch_and_write(con, COUNTS_SOURCE,
                       lambda: blue_spring.fetch_reports(since=since),
                       _write_to(T.counts_raw, T.counts_daily))


def ingest_archive(con, state: RunState) -> None:
    start, end = archive_window(con, state.today)
    log.info("archive: %s to %s", start, end)
    db.fetch_and_write(con, ARCHIVE_SOURCE,
                       lambda: open_meteo.fetch_archive(start, end),
                       db.write_archive_result)


def ingest_forecast(con, state: RunState) -> None:
    log.info("forecast: issue of %s", state.today)
    db.fetch_and_write(con, FORECAST_SOURCE,
                       lambda: open_meteo.fetch_forecast(issue_date=state.today),
                       _write_to(T.weather_raw, T.weather_forecast))


def ingest_gauge(con, state: RunState) -> None:
    start, end = gauge_window(con, state.today)
    log.info("gauge: %s to %s", start, end)
    db.fetch_and_write(con, GAUGE_SOURCE,
                       lambda: usgs_gauge.fetch_daily(start, end),
                       _write_to(T.gauge_raw, T.gauge_daily))


def check(con, state: RunState) -> None:
    from roll_call.quality import checks

    state.check_report = checks.run_checks(con, state.today)


def alert(con, state: RunState) -> None:
    if state.check_report is None:
        raise RuntimeError("no check report to alert on; the checks stage failed")
    from roll_call.quality import alert as quality_alert

    sent = quality_alert.send_alert(state.check_report)
    log.info("alert %s", "sent" if sent else "not needed")


def refresh_baselines(con, state: RunState) -> None:
    from roll_call.quality import baselines

    if baselines.refresh_if_due(con, state.today):
        log.info("baselines refreshed")


def retrain(con, state: RunState) -> None:
    from roll_call.model import train

    if train.retrain_if_needed(con, state.today):
        log.info("model retrained")


def predict(con, state: RunState) -> None:
    from roll_call.model import predict as model_predict

    prediction = model_predict.run(con, state.today)
    log.info("prediction: %s", prediction if prediction is not None else "none (season closed)")


def publish(con, state: RunState) -> None:
    import publish_dashboard  # jobs/publish_dashboard.py; this folder is on sys.path

    publish_dashboard.publish(con)


STAGES: tuple[tuple[str, Callable[[Any, RunState], None]], ...] = (
    ("ingest counts", ingest_counts),
    ("ingest weather archive", ingest_archive),
    ("ingest forecast", ingest_forecast),
    ("ingest gauge", ingest_gauge),
    ("checks", check),
    ("alert", alert),
    ("baseline refresh", refresh_baselines),
    ("retrain", retrain),
    ("predict", predict),
    ("publish", publish),
)


def ensure_schema(con) -> None:
    """Create every table the stages read, before any stage runs.

    Modules create their own tables on first use, but a stage can read a table another module
    owns before that module has run: the training gate reads clearing_decisions, which the
    baselines module creates. Creating them all up front removes that ordering dependency.
    """
    from roll_call.model import store as model_store
    from roll_call.quality import baselines, store as quality_store

    quality_store.ensure_tables(con)
    baselines.ensure_tables(con)
    model_store.ensure_tables(con)


def run_stages(con, today: date) -> RunState:
    """Run every stage in order. A failure is logged and recorded, and the next stage runs."""
    state = RunState(today=today)
    ensure_schema(con)
    for name, stage in STAGES:
        try:
            stage(con, state)
        except Exception:  # noqa: BLE001 - one stage must not stop the others
            log.exception("stage %r failed", name)
            state.failed.append(name)
    return state


def main(argv: list[str] | None = None, db_path: str | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--today", type=date.fromisoformat, default=None,
                        help="run as of this local date (YYYY-MM-DD); default today in Florida")
    args = parser.parse_args(argv)
    today = args.today or local_today()
    con = db.connect(db_path)
    try:
        state = run_stages(con, today)
    finally:
        con.close()
    if state.failed:
        log.error("run for %s finished with failed stages: %s", today, ", ".join(state.failed))
        return 1
    log.info("run for %s finished", today)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
