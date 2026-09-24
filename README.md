# Roll Call

A daily manatee-count pipeline for Blue Spring State Park. Its subject is the data-quality
layer, which notices when a source drifts, goes stale or changes shape.

The design is in [`roll-call-spec.md`](roll-call-spec.md). Source API calls and scraping notes
are in [`docs/sources.md`](docs/sources.md).

## Stack

The pipeline is Python, run daily at 19:00 by a systemd user timer on an Ubuntu machine. It
stores everything in one DuckDB file and sends alert emails through Resend. The dashboard is an
Evidence site on Vercel, rebuilt from summary CSVs that each run pushes to the repo.
[ADR 0002](docs/adr/0002-local-scheduled-script-with-duckdb.md) records why the pipeline does
not run on Databricks, and [ADR 0003](docs/adr/0003-evidence-on-vercel-instead-of-tableau.md)
why the dashboard is not on Tableau Public.

The vocabulary is in [`CONTEXT.md`](CONTEXT.md) and the decisions are in
[`docs/adr/`](docs/adr/). [`docs/scheduling.md`](docs/scheduling.md) sets up the timers.

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
  quality/
    checks.py          the daily checks; opens incidents and quarantines observations
    season.py          season state (awaiting, open, overdue, closed) from the reports
    store.py           the incidents and quarantine tables
    baselines.py       weather normals, the yearly refresh and replayed refreshes
    clearing.py        record a person's decision on a quarantined observation
    gate.py            which counts the model may train on
    alert.py           the Resend email listing what newly opened
  model/
    features.py        one feature row per prediction day
    train.py           the count regression and when to retrain
    predict.py         the daily prediction
    score.py           scored predictions against persistence
    store.py           predictions, their inputs and model versions
  export/dashboard.py  summary CSVs for the dashboard; public-safe columns only
jobs/
  ingest_daily.py      the daily run (systemd timer, 19:00)
  publish_dashboard.py the daily run's last step: export, commit and push the dashboard CSVs
  backup_db.py         weekly: export human records to CSV, copy the database to a second disk
  backfill_weather.py  one-off: weather history for training and baselines
  replay_baselines.py  one-off: replayed baseline refreshes for past seasons, after the backfill
notebooks/
  clearing.ipynb       where the owner clears quarantined observations
data/
  reference/           hand-transcribed counts for scoring the parser (never a source)
  seasons/             one extracted CSV per historical season
  records/             clearing decisions and baseline log, exported for version control
tools/
  mutate.py            on-demand mutation generator; works on a copy of the database only
  extract_seasons.py   one-off historical backfill: season pages to per-season CSVs
dashboard/             the Evidence site that Vercel builds
  pages/               source status, baseline drift and quarantine queue
  sources/roll_call/   the exported CSVs, committed by the daily run
tests/                 no network; parsers run against saved fixtures
```

## Stages

All four stages are built, and `tests/test_end_to_end.py` runs a full season through every
real module.

1. Ingestion: raw and parsed tables per source, the run log, catch-up.
2. Quality checks: freshness, volume, distribution, counts and cross-source checks;
   baselines; quarantine, incidents and the clearing notebook; the alert email.
3. The model and its retraining triggers.
4. The dashboard: summary CSV export, the publish step, and the Evidence site on Vercel.

The remaining work waits on real data:

- Saved real source responses, to replace the synthetic fixtures in `tests/fixtures/`.
- The historical season extraction (`tools/extract_seasons.py`).
- Threshold calibration from the extracted seasons.

## Local dev

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/python -m pytest
```
