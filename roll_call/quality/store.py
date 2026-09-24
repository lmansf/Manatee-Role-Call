"""Incidents and quarantined observations: the tables and the writes that keep them honest.

Both writes are idempotent. The checks run every day over overlapping windows, so the same
failure or the same doubtful value is seen many times. It must open once and alert once.
Detail and value columns hold names and numbers only: the repo and the site are public, and
report text stays on the owner's machine.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

import duckdb

INCIDENTS = "incidents"
QUARANTINE = "quarantine"

INCIDENTS_DDL = f"""
CREATE TABLE IF NOT EXISTS {INCIDENTS} (
    incident_id VARCHAR PRIMARY KEY,
    source      VARCHAR NOT NULL,
    check_name  VARCHAR NOT NULL,
    opened_at   TIMESTAMP NOT NULL,  -- UTC
    closed_at   TIMESTAMP,           -- UTC; null while open
    detail      VARCHAR,             -- names and numbers only
    alerted_at  TIMESTAMP            -- UTC; null until an alert listed it
)
"""

QUARANTINE_DDL = f"""
CREATE TABLE IF NOT EXISTS {QUARANTINE} (
    obs_id           VARCHAR PRIMARY KEY,  -- <table>:<key>
    source           VARCHAR NOT NULL,
    table_name       VARCHAR NOT NULL,
    obs_key          VARCHAR NOT NULL,
    observation_date DATE,
    check_name       VARCHAR NOT NULL,
    value            VARCHAR,              -- the doubted value, as text
    opened_at        TIMESTAMP NOT NULL,   -- UTC
    alerted_at       TIMESTAMP             -- UTC; null until an alert listed it
)
"""

_INCIDENT_COLUMNS = ("incident_id", "source", "check_name", "opened_at", "closed_at",
                     "detail", "alerted_at")
_QUARANTINE_COLUMNS = ("obs_id", "source", "table_name", "obs_key", "observation_date",
                       "check_name", "value", "opened_at", "alerted_at")


def utc_now() -> datetime:
    """Now in UTC as a naive timestamp, the way every table stores time."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    """Whether a table exists. A source that has never been ingested has no table yet."""
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()
    return bool(row and row[0])


def ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create the incidents and quarantine tables if they do not exist."""
    con.execute(INCIDENTS_DDL)
    con.execute(QUARANTINE_DDL)


def obs_id(table_name: str, obs_key: str) -> str:
    """The quarantine key: `<table>:<key>`, such as `blue_spring_counts_daily:2026-01-05`."""
    return f"{table_name}:{obs_key}"


def _incident_dict(row: tuple) -> dict[str, Any]:
    return dict(zip(_INCIDENT_COLUMNS, row))


def open_incidents(con: duckdb.DuckDBPyConnection, source: str | None = None,
                   check_name: str | None = None) -> list[dict[str, Any]]:
    """Open incidents, optionally narrowed to one source and check."""
    sql = f"SELECT {', '.join(_INCIDENT_COLUMNS)} FROM {INCIDENTS} WHERE closed_at IS NULL"
    params: list[Any] = []
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    if check_name is not None:
        sql += " AND check_name = ?"
        params.append(check_name)
    return [_incident_dict(r) for r in con.execute(sql + " ORDER BY opened_at", params).fetchall()]


def open_incident(con: duckdb.DuckDBPyConnection, source: str, check_name: str, detail: str,
                  now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    """Open an incident for a failed check, unless one is already open for it.

    Returns the incident and whether this call created it. A check that keeps failing keeps
    its first incident, so the alert lists it once.
    """
    existing = open_incidents(con, source, check_name)
    if existing:
        return existing[0], False
    row = {
        "incident_id": uuid.uuid4().hex,
        "source": source,
        "check_name": check_name,
        "opened_at": now or utc_now(),
        "closed_at": None,
        "detail": detail,
        "alerted_at": None,
    }
    con.execute(f"INSERT INTO {INCIDENTS} VALUES (?, ?, ?, ?, ?, ?, ?)",
                [row[c] for c in _INCIDENT_COLUMNS])
    return row, True


def close_incidents(con: duckdb.DuckDBPyConnection, source: str, check_name: str,
                    now: datetime | None = None) -> list[dict[str, Any]]:
    """Close every open incident for a check that passed. Returns the incidents it closed."""
    closing = open_incidents(con, source, check_name)
    if not closing:
        return []
    closed_at = now or utc_now()
    con.execute(
        f"UPDATE {INCIDENTS} SET closed_at = ? WHERE closed_at IS NULL AND source = ? AND check_name = ?",
        [closed_at, source, check_name],
    )
    return [{**i, "closed_at": closed_at} for i in closing]


def quarantine_observation(con: duckdb.DuckDBPyConnection, source: str, table_name: str,
                           obs_key: str, observation_date: date | None, check_name: str,
                           value: Any, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    """Hold one observation out of training. Idempotent by obs_id.

    Returns the quarantine row and whether this call created it. An observation already
    quarantined keeps its first row, whatever check doubted it first and whatever clearing
    decision it has since received.
    """
    oid = obs_id(table_name, obs_key)
    found = con.execute(
        f"SELECT {', '.join(_QUARANTINE_COLUMNS)} FROM {QUARANTINE} WHERE obs_id = ?", [oid]
    ).fetchone()
    if found:
        return dict(zip(_QUARANTINE_COLUMNS, found)), False
    row = {
        "obs_id": oid,
        "source": source,
        "table_name": table_name,
        "obs_key": obs_key,
        "observation_date": observation_date,
        "check_name": check_name,
        "value": None if value is None else str(value),
        "opened_at": now or utc_now(),
        "alerted_at": None,
    }
    con.execute(f"INSERT INTO {QUARANTINE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [row[c] for c in _QUARANTINE_COLUMNS])
    return row, True


def mark_alerted(con: duckdb.DuckDBPyConnection, incident_ids: list[str], obs_ids: list[str],
                 now: datetime | None = None) -> None:
    """Stamp alerted_at on the incidents and quarantined observations an alert listed."""
    at = now or utc_now()
    for iid in incident_ids:
        con.execute(f"UPDATE {INCIDENTS} SET alerted_at = ? WHERE incident_id = ?", [at, iid])
    for oid in obs_ids:
        con.execute(f"UPDATE {QUARANTINE} SET alerted_at = ? WHERE obs_id = ?", [at, oid])
