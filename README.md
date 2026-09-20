# Roll Call

A daily manatee-count pipeline for Blue Spring State Park, built so the interesting part is
the data-quality layer: what happens when a source drifts, goes stale, or changes shape.

Full design: [`roll-call-spec.md`](roll-call-spec.md).

## Layout

```
roll_call/
  config.py            coordinates, season dates, table names
  ingest/
    base.py            IngestRun / IngestResult: the contract the quality layer reads
    open_meteo.py      weather (archive + forecast endpoints)
    blue_spring.py     scraped daily counts from Save the Manatee Club reports
    fwc.py             optional statewide survey context (stub)
  storage/delta.py     Delta writes; raw + parsed tables, run stamping
jobs/
  ingest_daily.py      scheduled Databricks entrypoint
  backfill_weather.py  one-off reanalysis backfill for the baseline
tests/                 parser tests against saved fixtures, no network
```

## Stages

1. **Ingestion** (this scaffold). Raw and parsed tables per source, run metadata.
2. Quality checks: freshness, volume, distribution; baselines; quarantine table.
3. Model and retraining trigger.
4. Sheets export and Tableau Public dashboard.

## Local dev

```
pip install -e ".[dev]"
pytest
```
