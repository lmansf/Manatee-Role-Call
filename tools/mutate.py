"""Synthetic mutation generator. Standalone, on demand, never scheduled.

Decision (spec §3.4): this is a script you point at a *copy* of a table to prove that
every quality check fires. It is not part of the daily job and not part of the test suite.

Usage sketch (fill in once stage 2 exists and there are checks to fire):

    python tools/mutate.py --table workspace.roll_call_scratch.weather_daily_copy \
        --mutation null_spike --column temperature_2m_mean --rate 0.3

Mutations to support, one function each: schema_drift (drop/add a column), null_spike,
unit_change (°C -> °F on one column), duplicates, renamed_key. Each should print what it
did, so the run log can be compared with what the checks reported.

GUARDRAIL, non-negotiable: refuse any target whose schema is not a scratch schema.
`REAL_SCHEMAS` below is the deny-list. A typo here costs you real data.
"""
from __future__ import annotations

REAL_SCHEMAS = {"roll_call"}


def assert_safe_target(table: str) -> None:
    """Raise unless `table` is in a scratch schema. TODO(you): implement and test first,
    before any mutation function exists."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit("not implemented; see module docstring")
