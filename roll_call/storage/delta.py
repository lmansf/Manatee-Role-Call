"""Delta table writes.

Runs on Databricks (Spark available) and degrades to a no-op with a warning elsewhere so
the ingestion modules stay testable locally. Every parsed row gets `run_id` and
`ingested_at` stamped on it. Those two columns are how the quality layer joins a row back
to the run that produced it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from roll_call.ingest.base import IngestResult

log = logging.getLogger(__name__)


def _spark():
    try:
        from pyspark.sql import SparkSession  # type: ignore

        return SparkSession.getActiveSession()
    except ImportError:
        return None


def write_ingest_result(result: IngestResult, raw_table: str, parsed_table: str) -> None:
    """Persist one IngestResult: one raw row, N parsed rows, one ingest_runs row.

    TODO(you): implement. The interesting decision is idempotency. If the job runs twice
    on the same day (retry, manual re-run), what happens?
      - `append` duplicates every row, and the duplicate-match check in stage 2 fires on
        your own pipeline. Instructive, but not what you want in production.
      - MERGE on a natural key (source + obs_date / report_date) makes re-runs safe.
        Pick the key deliberately; spec §5 says surrogate keys on stable identifiers.
    Write `ingest_runs` last, and only after the data writes succeed, so a run row with
    status=succeeded always has its rows behind it.
    """
    spark = _spark()
    if spark is None:
        log.warning("No active SparkSession; skipping write of %d rows to %s", len(result.records), parsed_table)
        return
    raise NotImplementedError


def stamp(records: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    """Add run_id and ingested_at to every record."""
    now = datetime.now(timezone.utc)
    return [{**r, "run_id": run_id, "ingested_at": now} for r in records]
