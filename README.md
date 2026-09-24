# Roll Call

A daily manatee-count pipeline for Blue Spring State Park, built so the interesting part is
the data-quality layer: what happens when a source drifts, goes stale, or changes shape.

Full design: [`roll-call-spec.md`](roll-call-spec.md). Source API calls and scraping notes:
[`docs/sources.md`](docs/sources.md).

## Stack

Python, run daily at 19:00 by a systemd user timer on an Ubuntu machine. One DuckDB file for
storage. Resend for alert emails. A Google Sheet feeding a Tableau Public dashboard. Why not
Databricks: [ADR 0002](docs/adr/0002-local-scheduled-script-with-duckdb.md).

Vocabulary: [`CONTEXT.md`](CONTEXT.md). Decisions: [`docs/adr/`](docs/adr/).
Scheduling setup: [`docs/scheduling.md`](docs/scheduling.md).

## Layout

```
roll_call/
  config.py            coordinates, thresholds, table names; loads .env
  ingest/
    base.py            IngestRun / IngestResult: the contract the quality layer reads
    open_meteo.py      weather: archive (observed) and forecast
    blue_spring.py     researchers' and park counts from Save the Manatee Club reports
    usgs_gauge.py      river temperature from the USGS gauge near DeLand
  storage/db.py        the DuckDB file: connections, run log, ingest writes
jobs/
  ingest_daily.py      the daily run (systemd timer, 19:00)
  backup_db.py         weekly: export human records to CSV, copy the database to a second disk
  backfill_weather.py  one-off: weather history for training and baselines
data/
  reference/           hand-transcribed counts for scoring the parser (never a source)
  seasons/             one extracted CSV per historical season
  records/             clearing decisions and baseline log, exported for version control
tools/
  mutate.py            on-demand mutation generator; works on a copy of the database only
  extract_seasons.py   one-off historical backfill: season pages to per-season CSVs
tests/                 no network; parsers run against saved fixtures
```

## Stages

1. **Ingestion** (in progress). Raw and parsed tables per source, run log, catch-up.
2. Quality checks: freshness, volume, distribution, consistency; baselines; quarantine,
   incidents, clearing notebook; alert email.
3. Model and retraining trigger.
4. Sheets export and Tableau Public dashboard.

## Local dev

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/python -m pytest
```
