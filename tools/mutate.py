"""Synthetic mutation generator. Standalone, on demand, never scheduled.

Decision (spec §3.4): point it at a *copy* of the database file to prove that every quality
check fires. It is not part of the daily job and not part of the test suite.

Usage sketch (fill in once stage 2 exists and there are checks to fire):

    cp data/roll_call.duckdb /tmp/scratch.duckdb
    python tools/mutate.py --db /tmp/scratch.duckdb --table weather_daily \
        --mutation null_spike --column temperature_2m_mean --rate 0.3

Mutations to support, one function each: schema_drift (drop/add a column), null_spike,
unit_change (°C -> °F on one column), duplicates, renamed_key. Each should print what it
did, so the run log can be compared with what the checks reported.

GUARDRAIL, non-negotiable: refuse the live database. A typo here costs you real data.
"""
from __future__ import annotations

from pathlib import Path

from roll_call import config


def assert_safe_target(db_path: Path | str) -> None:
    """Raise unless `db_path` is a different file from the live database (config.DB_PATH).

    TODO(you): implement and test first, before any mutation function exists. Compare
    resolved paths, not strings: a relative path, a symlink or a `..` must not slip past.
    Consider also refusing anything inside the backup folder (BACKUP_DIR).
    """
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(f"not implemented; see module docstring (live database: {config.DB_PATH})")
