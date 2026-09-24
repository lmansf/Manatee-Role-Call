"""Clearing: a person's recorded decision on a quarantined observation.

A quarantined observation is open until someone clears it. Clearing records one of two
decisions, with a reason: confirmed (the value is real, train on it) or rejected (the value is
wrong, keep it out). Verify a surprising value against the world before rejecting it. An
unusual count may be real.

The owner clears items in notebooks/clearing.ipynb. The reason is free text and stays in the
database: the dashboard shows the decision, never the reason.
"""
from __future__ import annotations

from datetime import datetime, timezone

import duckdb

from roll_call.quality import baselines

QUARANTINE_TABLE = "quarantine"  # DDL in roll_call/quality/store.py
DECISIONS_TABLE = baselines.DECISIONS_TABLE
DECISIONS = ("confirmed", "rejected")

_ITEM_COLUMNS = ("obs_id", "source", "table_name", "obs_key", "observation_date",
                 "check_name", "value", "opened_at")


def open_items(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Quarantined observations with no clearing decision, oldest first."""
    if not baselines.table_exists(con, QUARANTINE_TABLE):
        return []
    baselines.ensure_tables(con)
    columns = ", ".join(f"q.{c}" for c in _ITEM_COLUMNS)
    rows = con.execute(
        f"SELECT {columns} FROM {QUARANTINE_TABLE} q "
        f"LEFT JOIN {DECISIONS_TABLE} d ON d.obs_id = q.obs_id "
        "WHERE d.obs_id IS NULL ORDER BY q.opened_at, q.obs_id"
    ).fetchall()
    return [dict(zip(_ITEM_COLUMNS, row)) for row in rows]


def decide(con: duckdb.DuckDBPyConnection, obs_id: str, decision: str, reason: str,
           replace: bool = False, now: datetime | None = None) -> dict:
    """Record a decision on one quarantined observation and return it.

    The decision must be confirmed or rejected, and the reason must say something. The
    observation must be in quarantine. A second decision on the same observation is refused
    unless replace is True, so a record is never overwritten by accident.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, not {decision!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a decision needs a reason")
    if not baselines.table_exists(con, QUARANTINE_TABLE):
        raise KeyError(f"{obs_id} is not in quarantine (no quarantine table)")
    if con.execute(f"SELECT count(*) FROM {QUARANTINE_TABLE} WHERE obs_id = ?",
                   [obs_id]).fetchone()[0] == 0:
        raise KeyError(f"{obs_id} is not in quarantine")
    baselines.ensure_tables(con)
    existing = con.execute(f"SELECT decision FROM {DECISIONS_TABLE} WHERE obs_id = ?",
                           [obs_id]).fetchone()
    if existing and not replace:
        raise ValueError(f"{obs_id} is already {existing[0]}; pass replace=True to change it")

    decided_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    record = {"obs_id": obs_id, "decision": decision, "reason": reason.strip(),
              "decided_at": decided_at}
    con.execute(f"INSERT OR REPLACE INTO {DECISIONS_TABLE} VALUES (?, ?, ?, ?)",
                [obs_id, decision, record["reason"], decided_at])
    return record
