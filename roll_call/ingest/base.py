"""Shared shapes for every ingestion source.

The quality layer never talks to a source directly. It reads the run metadata that
every source records here (row counts, payload hash, timestamps) and the parsed tables.
Getting this contract right in stage 1 is what makes stages 2 and 3 cheap.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class IngestRun:
    """One execution of one source. Persisted to `ingest_runs` whether or not it succeeded.

    A failed run is still a row. The freshness check's first question is "when did this
    source last succeed", and it can't answer that if failures leave no trace.
    """

    source: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    status: str = "running"  # running | succeeded | failed
    row_count: int | None = None
    payload_sha256: str | None = None
    source_url: str | None = None
    error: str | None = None

    def succeed(self, row_count: int) -> "IngestRun":
        self.finished_at = datetime.now(timezone.utc)
        self.status = "succeeded"
        self.row_count = row_count
        return self

    def fail(self, exc: BaseException) -> "IngestRun":
        self.finished_at = datetime.now(timezone.utc)
        self.status = "failed"
        self.error = f"{type(exc).__name__}: {exc}"[:2000]
        return self


@dataclass
class IngestResult:
    """What a source hands back: the raw payload, the parsed rows, and the run record.

    `raw` is kept verbatim (response text or bytes). When the scraper breaks because the
    page changed shape, the raw table is what lets you re-parse history without re-fetching.
    """

    run: IngestRun
    raw: str
    records: list[dict[str, Any]]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
