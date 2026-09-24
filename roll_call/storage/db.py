"""The DuckDB file: connections, the run log, and ingest writes.

Every parsed row carries `run_id` and `ingested_at`; that pair is how the quality layer
traces a row back to the run that produced it. Timestamps are stored as UTC, except the
hourly weather timestamps, which are naive local time (America/New_York).

Every ingested table has a primary key on its natural key, and writes use
`INSERT OR REPLACE`. Catch-up windows overlap by design, so re-reading a day replaces its
row instead of duplicating it. Revisions worth keeping get their own key: a forecast is
keyed by its issue date, and a gauge value by its approval status.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

from roll_call import config
from roll_call.ingest.base import IngestResult, IngestRun
from roll_call.ingest.open_meteo import DAILY_FIELDS as WEATHER_FIELDS

log = logging.getLogger(__name__)

T = config.TABLES

INGEST_RUNS_DDL = f"""
CREATE TABLE IF NOT EXISTS {T.ingest_runs} (
    run_id         VARCHAR PRIMARY KEY,
    source         VARCHAR NOT NULL,
    started_at     TIMESTAMP NOT NULL,  -- UTC
    finished_at    TIMESTAMP,           -- UTC
    status         VARCHAR NOT NULL,    -- running | succeeded | failed
    row_count      INTEGER,
    payload_sha256 VARCHAR,
    source_url     VARCHAR,
    error          VARCHAR,
    null_rate      DOUBLE               -- share of nulls in the run's value columns
)
"""

_STAMP_COLUMNS = """
    run_id      VARCHAR NOT NULL,
    ingested_at TIMESTAMP NOT NULL,  -- UTC"""

_WEATHER_FIELDS = """
    temperature_2m_min      DOUBLE,
    temperature_2m_max      DOUBLE,
    temperature_2m_mean     DOUBLE,
    precipitation_sum       DOUBLE,
    wind_speed_10m_max      DOUBLE,
    shortwave_radiation_sum DOUBLE,
    units                   VARCHAR,  -- JSON of Open-Meteo daily_units"""

# Primary key of every ingested table. Used for the DDL and to spot repeated keys in a batch.
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    T.weather_daily: ("obs_date",),
    T.weather_hourly: ("obs_ts",),
    T.weather_forecast: ("issue_date", "target_date"),
    T.counts_daily: ("report_date",),
    T.gauge_daily: ("obs_date", "approval_status"),
    T.weather_raw: ("run_id",),
    T.counts_raw: ("run_id",),
    T.gauge_raw: ("run_id",),
}

_PARSED_COLUMNS: dict[str, str] = {
    T.weather_daily: f"""
    obs_date DATE NOT NULL,{_WEATHER_FIELDS}""",
    T.weather_hourly: """
    obs_ts         TIMESTAMP NOT NULL,  -- local time, America/New_York
    temperature_2m DOUBLE,
    unit           VARCHAR,""",
    T.weather_forecast: f"""
    issue_date  DATE NOT NULL,
    target_date DATE NOT NULL,{_WEATHER_FIELDS}""",
    T.counts_daily: """
    report_date       DATE NOT NULL,
    count_researchers INTEGER,
    count_park        INTEGER,
    not_counted       BOOLEAN,
    is_estimate       BOOLEAN,
    river_temp_f      DOUBLE,
    spring_temp_f     DOUBLE,
    post_url          VARCHAR,
    count_text        VARCHAR,""",
    T.gauge_daily: """
    obs_date        DATE NOT NULL,
    water_temp_c    DOUBLE,
    unit            VARCHAR,
    approval_status VARCHAR NOT NULL,  -- Provisional, later Approved; both rows are kept""",
}

PARSED_DDL: dict[str, str] = {
    table: (f"CREATE TABLE IF NOT EXISTS {table} ({columns}{_STAMP_COLUMNS}\n"
            f"    PRIMARY KEY ({', '.join(PRIMARY_KEYS[table])})\n)")
    for table, columns in _PARSED_COLUMNS.items()
}

RAW_TABLES: tuple[str, ...] = (T.weather_raw, T.counts_raw, T.gauge_raw)

RAW_DDL: dict[str, str] = {
    table: f"""
CREATE TABLE IF NOT EXISTS {table} (
    run_id     VARCHAR PRIMARY KEY,
    source     VARCHAR NOT NULL,
    fetched_at TIMESTAMP NOT NULL,  -- UTC
    payload    VARCHAR
)"""
    for table in RAW_TABLES
}


def connect(path: Path | str | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the database file, creating it, the run log and every ingested table on first use.

    DuckDB allows one writing process at a time. The systemd units wrap every job in
    `flock` so the daily run and the weekly backup can never collide (docs/scheduling.md).
    """
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        ensure_tables(con)
    return con


def ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create the run log, the raw tables and the parsed tables if they are missing."""
    con.execute(INGEST_RUNS_DDL)
    # Databases created before null_rate existed get the column added.
    con.execute(f"ALTER TABLE {T.ingest_runs} ADD COLUMN IF NOT EXISTS null_rate DOUBLE")
    for ddl in (*RAW_DDL.values(), *PARSED_DDL.values()):
        con.execute(ddl)


def _utc(ts: datetime | None) -> datetime | None:
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts else None


def record_run(con: duckdb.DuckDBPyConnection, run: IngestRun) -> None:
    """Insert or update one run. Failed runs are recorded too: the freshness check's first
    question is when a source last succeeded, and failures must leave a trace."""
    con.execute(
        f"INSERT OR REPLACE INTO {T.ingest_runs} (run_id, source, started_at, finished_at, "
        "status, row_count, payload_sha256, source_url, error, null_rate) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [run.run_id, run.source, _utc(run.started_at), _utc(run.finished_at), run.status,
         run.row_count, run.payload_sha256, run.source_url, run.error, run.null_rate],
    )


# The value columns whose nulls count toward a run's null rate. Counts are left out: a blank
# count is a day not counted, which is data, not a gap. Hourly rows repeat the daily story.
NULL_RATE_COLUMNS: dict[str, tuple[str, ...]] = {
    T.weather_daily: WEATHER_FIELDS,
    T.weather_forecast: WEATHER_FIELDS,
    T.gauge_daily: ("water_temp_c",),
}


def run_null_rate(run: IngestRun, parsed_table: str, records: list[dict[str, Any]]) -> float | None:
    """Share of null values across the table's value columns in this run's records.

    Computed once, when the run writes its rows, and stored on the run. Later runs overwrite
    overlapping rows, so a rate computed from the table afterwards would keep changing.
    Archive rows inside the archive's lag are null by design and are left out.
    """
    columns = NULL_RATE_COLUMNS.get(parsed_table)
    if not columns:
        return None
    if parsed_table == T.weather_daily:
        run_day = run.started_at.astimezone(ZoneInfo(config.TIMEZONE)).date()
        cutoff = run_day - timedelta(days=config.ARCHIVE_LAG_DAYS)
        records = [r for r in records if r.get("obs_date") is not None and r["obs_date"] <= cutoff]
    cells = [r.get(c) for r in records for c in columns]
    if not cells:
        return None
    return sum(v is None for v in cells) / len(cells)


def last_successful_run(con: duckdb.DuckDBPyConnection, source: str) -> datetime | None:
    """Start time (UTC) of the source's latest successful run, or None if it never succeeded.
    Each run catches up from here rather than fetching only yesterday (spec §2)."""
    row = con.execute(
        f"SELECT max(started_at) FROM {T.ingest_runs} WHERE source = ? AND status = 'succeeded'",
        [source],
    ).fetchone()
    return row[0] if row else None


def record_failed_fetch(con: duckdb.DuckDBPyConnection, source: str, exc: BaseException,
                        started_at: datetime | None = None) -> IngestRun:
    """Record a fetch that raised before it returned a result.

    Fetchers mark their run failed and re-raise, so the caller never sees the run object.
    This writes a failed run in its place. An exception that carries its run as `exc.run`
    is recorded with that run instead.
    """
    run = getattr(exc, "run", None)
    if not isinstance(run, IngestRun):
        run = IngestRun(source=source, started_at=started_at or datetime.now(timezone.utc))
    if run.status != "failed":
        run.fail(exc)
    record_run(con, run)
    return run


def fetch_and_write(con: duckdb.DuckDBPyConnection, source: str,
                    fetch: Callable[[], IngestResult],
                    write: Callable[[duckdb.DuckDBPyConnection, IngestResult], None]) -> IngestResult:
    """Run one fetch and persist its result. Every outcome leaves a run row.

    A fetch that raises is recorded as a failed run and re-raised. A result whose run
    already says failed is written (raw payload and run row), then raised as an error so
    the caller counts the stage as failed.
    """
    started_at = datetime.now(timezone.utc)
    try:
        result = fetch()
    except Exception as exc:
        record_failed_fetch(con, source, exc, started_at)
        raise
    write(con, result)
    if result.run.status == "failed":
        raise RuntimeError(f"{source} run {result.run.run_id} failed: {result.run.error}")
    return result


def stamp(records: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    """Add run_id and ingested_at (UTC) to every record."""
    now = _utc(datetime.now(timezone.utc))
    return [{**r, "run_id": run_id, "ingested_at": now} for r in records]


def _cell(value: Any) -> Any:
    """Store dicts and lists (such as a units mapping) as JSON text."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _dedupe(table: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the last record per primary key, and log how many were dropped.

    One statement cannot replace the same key twice, so a batch with a repeated key would
    fail the whole run. A repeat can be real, such as the hour that occurs twice when
    daylight saving time ends. The last record wins and the log says so.
    """
    key = PRIMARY_KEYS.get(table)
    if not key:
        return records
    by_key = {tuple(r[k] for k in key): r for r in records}
    if len(by_key) < len(records):
        log.warning("%s: dropped %d record(s) with a repeated key %s",
                    table, len(records) - len(by_key), key)
    return list(by_key.values())


def _insert_records(con: duckdb.DuckDBPyConnection, table: str,
                    records: list[dict[str, Any]]) -> int:
    """INSERT OR REPLACE the records into the table and return how many were written.

    Columns come from the first record. Every record must carry the same keys: a missing
    key fails loudly instead of storing a null, and an unknown key fails the insert. The
    rows go in as one DataFrame scan, because row-by-row inserts take seconds per year of
    hourly weather.
    """
    if not records:
        return 0
    records = _dedupe(table, records)
    columns = list(records[0])
    frame = pd.DataFrame.from_records(
        [[_cell(r[c]) for c in columns] for r in records], columns=columns)
    view = f"_incoming_{uuid.uuid4().hex}"
    cols = ", ".join(columns)
    con.register(view, frame)
    try:
        con.execute(f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM {view}")
    finally:
        con.unregister(view)
    return len(records)


def _write_raw(con: duckdb.DuckDBPyConnection, result: IngestResult, raw_table: str) -> None:
    con.execute(
        f"INSERT OR REPLACE INTO {raw_table} (run_id, source, fetched_at, payload) "
        "VALUES (?, ?, ?, ?)",
        [result.run.run_id, result.run.source, _utc(result.run.started_at), result.raw],
    )


def _record_write_failure(con: duckdb.DuckDBPyConnection, result: IngestResult,
                          raw_table: str, exc: BaseException) -> None:
    """After a failed write, keep the raw payload and mark the run failed.

    The raw payload is what lets a broken parser run again over history without a new
    fetch. If even that write fails, the run row is still recorded on its own.
    """
    result.run.fail(exc)
    try:
        con.begin()
        if result.raw is not None:
            _write_raw(con, result, raw_table)
        record_run(con, result.run)
        con.commit()
    except Exception:  # noqa: BLE001 - keep the original error; at least record the run
        con.rollback()
        log.exception("could not store the raw payload of failed run %s", result.run.run_id)
        record_run(con, result.run)


def write_ingest_result(con: duckdb.DuckDBPyConnection, result: IngestResult,
                        raw_table: str, parsed_table: str,
                        extra: dict[str, list[dict[str, Any]]] | None = None) -> None:
    """Persist one IngestResult in one transaction: the raw row, the stamped parsed rows,
    any `extra` parsed rows (table name to records), then the run row last.

    Writing the run last means a run marked succeeded always has its rows behind it. When a
    write fails, the transaction rolls back, the run is recorded as failed with its raw
    payload, and the error is raised again. A result whose run already failed writes only
    its raw payload and its run row.
    """
    run = result.run
    tables = {} if run.status == "failed" else {parsed_table: result.records, **(extra or {})}
    try:
        con.begin()
        _write_raw(con, result, raw_table)
        for table, records in tables.items():
            _insert_records(con, table, stamp(records, run.run_id))
        if run.status != "failed":
            run.null_rate = run_null_rate(run, parsed_table, result.records)
        record_run(con, run)
        con.commit()
    except Exception as exc:
        con.rollback()
        _record_write_failure(con, result, raw_table, exc)
        raise


def archive_hourly_records(result: IngestResult) -> list[dict[str, Any]]:
    """The archive's hourly rows, parsed from the raw JSON that the archive fetch kept."""
    from roll_call.ingest import open_meteo

    return open_meteo.parse_hourly(json.loads(result.raw))


def write_archive_result(con: duckdb.DuckDBPyConnection, result: IngestResult) -> None:
    """Persist an Open-Meteo archive result: raw, daily and hourly rows in one transaction.

    If the hourly block fails to parse, the run fails and the raw payload is kept.
    """
    extra: dict[str, list[dict[str, Any]]] = {}
    if result.run.status != "failed":
        try:
            extra[T.weather_hourly] = archive_hourly_records(result)
        except Exception as exc:
            _record_write_failure(con, result, T.weather_raw, exc)
            raise
    write_ingest_result(con, result, T.weather_raw, T.weather_daily, extra=extra)
