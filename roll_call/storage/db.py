"""The DuckDB file: connections, the run log, and ingest writes.

Every parsed row carries `run_id` and `ingested_at`; that pair is how the quality layer
traces a row back to the run that produced it. Timestamps are stored as UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from roll_call import config
from roll_call.ingest.base import IngestResult, IngestRun

INGEST_RUNS_DDL = f"""
CREATE TABLE IF NOT EXISTS {config.TABLES.ingest_runs} (
    run_id         VARCHAR PRIMARY KEY,
    source         VARCHAR NOT NULL,
    started_at     TIMESTAMP NOT NULL,  -- UTC
    finished_at    TIMESTAMP,           -- UTC
    status         VARCHAR NOT NULL,    -- running | succeeded | failed
    row_count      INTEGER,
    payload_sha256 VARCHAR,
    source_url     VARCHAR,
    error          VARCHAR
)
"""


def connect(path: Path | str | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the database file, creating it and the run log on first use.

    DuckDB allows one writing process at a time. The systemd units wrap every job in
    `flock` so the daily run and the weekly backup can never collide (docs/scheduling.md).
    """
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(INGEST_RUNS_DDL)
    return con


def _utc(ts: datetime | None) -> datetime | None:
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts else None


def record_run(con: duckdb.DuckDBPyConnection, run: IngestRun) -> None:
    """Insert or update one run. Failed runs are recorded too: the freshness check's first
    question is when a source last succeeded, and failures must leave a trace."""
    con.execute(
        f"INSERT OR REPLACE INTO {config.TABLES.ingest_runs} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [run.run_id, run.source, _utc(run.started_at), _utc(run.finished_at), run.status,
         run.row_count, run.payload_sha256, run.source_url, run.error],
    )


def last_successful_run(con: duckdb.DuckDBPyConnection, source: str) -> datetime | None:
    """Start time (UTC) of the source's latest successful run, or None if it never succeeded.
    Each run catches up from here rather than fetching only yesterday (spec §2)."""
    row = con.execute(
        f"SELECT max(started_at) FROM {config.TABLES.ingest_runs} WHERE source = ? AND status = 'succeeded'",
        [source],
    ).fetchone()
    return row[0] if row else None


def write_ingest_result(con: duckdb.DuckDBPyConnection, result: IngestResult,
                        raw_table: str, parsed_table: str) -> None:
    """Persist one IngestResult: one raw row, N parsed rows, then the run row.

    TODO(owner): implement. The interesting decision is idempotency. Runs catch up from the last
    success, so windows overlap by design, and a retry after a failure repeats work.
      - A plain INSERT duplicates rows, and the duplicate check in stage 2 fires on your own
        pipeline. Instructive once, not what you want to keep.
      - Give each parsed table a PRIMARY KEY on its natural key and use
        `INSERT OR REPLACE` (or `INSERT ... ON CONFLICT DO UPDATE`). Pick the key
        deliberately: spec §5 says surrogate keys on stable identifiers (site, date, counter).
      - Forecasts and gauge values get revised. Should a revision replace the old value, or
        add a row with the revision's own key (issue date; approval status)? The spec says
        revisions are drift worth keeping.
    Wrap the writes in one transaction (`con.begin()` / `con.commit()`), and call
    `record_run` last, so a run marked succeeded always has its rows behind it.
    """
    raise NotImplementedError


def stamp(records: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    """Add run_id and ingested_at (UTC) to every record."""
    now = _utc(datetime.now(timezone.utc))
    return [{**r, "run_id": run_id, "ingested_at": now} for r in records]
