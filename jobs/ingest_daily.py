"""Databricks job entrypoint: daily ingestion of both sources.

Schedule this as a single-task job. Keep it small; free-tier compute is time-limited.
Each source is wrapped separately so one failing does not stop the other, and each
failure still leaves an `ingest_runs` row (see IngestRun.fail).

TODO(you): fill in the two calls once the ingest modules are implemented. Decide the
date window for Open-Meteo: yesterday only, or a trailing week so late-arriving archive
days get picked up (the archive lags ~5 days)? The MERGE decision in storage/delta.py
determines whether a trailing window is safe.
"""
from __future__ import annotations

import logging

from roll_call import config
from roll_call.ingest import blue_spring, open_meteo
from roll_call.storage import delta

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("roll_call.jobs.ingest_daily")


def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    main()
