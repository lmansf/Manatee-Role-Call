"""The dashboard's data: four small summary CSVs for the Evidence site in dashboard/.

Each run writes them to `config.DASHBOARD_DATA_DIR`, which Evidence reads as a CSV source named
`roll_call`. jobs/publish_dashboard.py then commits and pushes them, and Vercel rebuilds the site.

The files are public by design. They carry dates, source and check names, numbers and statuses
only. Never add report text (it names people), error messages (a URL in one can carry a key), or
anything from `.env`.

The column lists below are a contract with the Evidence project. Change them only together.

Output is deterministic: rows are sorted, and dates and numbers have one fixed format. A day with
no new data therefore writes byte-identical files, and git sees nothing to commit. `meta.csv`
holds the generation time, so it is rewritten only when another file or the code version changed.
"""
from __future__ import annotations

import csv
import io
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import duckdb

from roll_call import config

log = logging.getLogger(__name__)

COLUMNS: dict[str, tuple[str, ...]] = {
    "meta": ("generated_at_utc", "git_sha"),
    "source_status": ("run_date", "source", "status", "last_success_utc", "row_count",
                      "rows_expected", "null_rate", "null_rate_normal"),
    "baseline_drift": ("refreshed_on", "replayed", "source", "measure", "old_value", "new_value",
                       "change", "cumulative_change"),
    "quarantine_queue": ("observation_date", "source", "check_name", "value", "status",
                         "opened_on", "cleared_on", "days_open"),
}

# The code the pipeline runs. meta.csv names the last commit that touched these paths. HEAD
# would not do: each publish adds a data commit, so HEAD changes every day.
PIPELINE_PATHS = ("roll_call", "jobs", "pyproject.toml")

Row = tuple[Any, ...]


def _utc_text(ts: datetime | None) -> str:
    """Format a UTC timestamp as ISO 8601 with a Z, to the second. DuckDB hands back naive
    timestamps; the database stores UTC (see storage/db.py)."""
    if ts is None:
        return ""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_date(ts: datetime) -> str:
    """The America/New_York date of a stored UTC timestamp, as YYYY-MM-DD."""
    aware = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    return aware.astimezone(ZoneInfo(config.TIMEZONE)).date().isoformat()


def _cell(value: Any) -> str:
    """One fixed text form per type, so the same data always writes the same bytes."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    return bool(row and row[0])


def source_status(con: duckdb.DuckDBPyConnection) -> list[Row]:
    """One row per source per local run date, from `ingest_runs`.

    The row reflects the latest run that day. `last_success_utc` is the start of the latest
    succeeded run up to and including that day, matching db.last_successful_run. A run still
    marked `running` never finished, so it counts as failed: the export runs after every source
    has recorded its outcome, and the jobs share a lock, so no other run can be in progress.

    rows_expected, null_rate and null_rate_normal stay blank until the stage 2 checks exist.
    TODO(owner): fill them from the volume and distribution check results once those tables
    exist.
    """
    table = config.TABLES.ingest_runs
    if not _table_exists(con, table):
        return []
    runs = con.execute(
        f"SELECT source, started_at, status, row_count FROM {table} "
        "ORDER BY source, started_at, run_id"
    ).fetchall()

    # Runs arrive in time order per source, so each later run of a day replaces the earlier
    # one, and last_success already covers every run up to it.
    latest: dict[tuple[str, str], Row] = {}
    last_success: dict[str, datetime] = {}
    for source, started_at, status, row_count in runs:
        if status == "succeeded":
            last_success[source] = started_at
        run_date = _local_date(started_at)
        latest[(run_date, source)] = (
            run_date,
            source,
            "succeeded" if status == "succeeded" else "failed",
            _utc_text(last_success.get(source)),
            row_count,
            None,  # rows_expected
            None,  # null_rate
            None,  # null_rate_normal
        )
    return [latest[key] for key in sorted(latest)]


def baseline_drift(con: duckdb.DuckDBPyConnection) -> list[Row]:
    """Cumulative signed change in each baseline across refreshes, replayed years included.

    TODO(owner): stage 2. Read `config.TABLES.baseline_refreshes`, the refresh log that keeps
    each baseline's old and new value side by side, with a flag for replayed refreshes. One row
    per refresh per source per measure: change = new_value - old_value, and cumulative_change is
    the running sum of change per (source, measure) ordered by refreshed_on. Sort by
    (refreshed_on, source, measure). Until that table exists, the file is header-only.
    """
    return []


def quarantine_queue(con: duckdb.DuckDBPyConnection) -> list[Row]:
    """Quarantined observations and how long each took to clear.

    TODO(owner): stage 2. Read the quarantine table (not created yet; add its name to
    `config.Tables` when it is), one row per quarantined observation with the check that doubted
    it and the day it opened. Join `config.TABLES.clearing_decisions` for status (confirmed or
    rejected; open when no decision exists) and cleared_on. days_open runs from opened_on to
    cleared_on, or to the run date while still open. Never export the clearing reason: it is
    free text. Sort by (opened_on, source, check_name, observation_date). Until then, the file
    is header-only.
    """
    return []


def meta(git_sha: str, now: datetime | None = None) -> list[Row]:
    """When the files were generated, and which pipeline code generated them."""
    return [(_utc_text(now or datetime.now(timezone.utc)), git_sha)]


def pipeline_sha(repo: Path = config.REPO_ROOT) -> str:
    """Short SHA of the last commit that touched the pipeline code, or blank outside git."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", *PIPELINE_PATHS],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    return out[:7]


def _csv_text(columns: Iterable[str], rows: Iterable[Row]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return buf.getvalue()


def _write_if_changed(path: Path, text: str) -> bool:
    """Write the file only when its bytes differ. Returns True when it wrote."""
    data = text.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.write_bytes(data)
    return True


def _existing_sha(path: Path) -> str | None:
    """The git_sha in an existing meta.csv, or None when the file is missing or malformed."""
    try:
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))
    except (OSError, UnicodeDecodeError):
        return None
    if len(rows) != 1 or tuple(rows[0]) != COLUMNS["meta"]:
        return None
    return rows[0]["git_sha"]


def export(con: duckdb.DuckDBPyConnection, out_dir: Path | None = None,
           git_sha: str | None = None, now: datetime | None = None) -> list[Path]:
    """Write the four CSVs to out_dir. Returns the files whose contents changed.

    git_sha defaults to pipeline_sha() of this repo. Pass "" to leave it blank.
    """
    out_dir = Path(out_dir or config.DASHBOARD_DATA_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    if git_sha is None:
        git_sha = pipeline_sha()

    summaries = {
        "source_status": source_status(con),
        "baseline_drift": baseline_drift(con),
        "quarantine_queue": quarantine_queue(con),
    }
    changed = []
    for name, rows in summaries.items():
        path = out_dir / f"{name}.csv"
        if _write_if_changed(path, _csv_text(COLUMNS[name], rows)):
            changed.append(path)

    meta_path = out_dir / "meta.csv"
    if changed or _existing_sha(meta_path) != git_sha:
        if _write_if_changed(meta_path, _csv_text(COLUMNS["meta"], meta(git_sha, now))):
            changed.append(meta_path)
    for path in changed:
        log.info("wrote %s", path.name)
    return changed
