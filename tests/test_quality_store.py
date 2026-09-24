"""Incidents and quarantine: idempotent writes against an in-memory DuckDB."""
from datetime import date

import duckdb
import pytest

from roll_call.quality import store


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    store.ensure_tables(c)
    yield c
    c.close()


def test_ensure_tables_is_repeatable(con):
    store.ensure_tables(con)
    assert store.table_exists(con, "incidents")
    assert store.table_exists(con, "quarantine")
    assert not store.table_exists(con, "nope")


def test_open_incident_is_idempotent_while_open(con):
    first, created = store.open_incident(con, "usgs_gauge_daily", "freshness", "3 days")
    again, created_again = store.open_incident(con, "usgs_gauge_daily", "freshness", "4 days")
    assert created and not created_again
    assert again["incident_id"] == first["incident_id"]
    assert len(store.open_incidents(con)) == 1


def test_close_then_reopen_makes_a_new_incident(con):
    first, _ = store.open_incident(con, "s", "volume", "0 rows")
    closed = store.close_incidents(con, "s", "volume")
    assert [c["incident_id"] for c in closed] == [first["incident_id"]]
    assert closed[0]["closed_at"] is not None
    assert store.close_incidents(con, "s", "volume") == []
    second, created = store.open_incident(con, "s", "volume", "0 rows")
    assert created and second["incident_id"] != first["incident_id"]
    assert con.execute("SELECT count(*) FROM incidents").fetchone()[0] == 2


def test_close_touches_only_its_own_check(con):
    store.open_incident(con, "s", "volume", "")
    store.open_incident(con, "s", "freshness", "")
    store.close_incidents(con, "s", "volume")
    assert [i["check_name"] for i in store.open_incidents(con)] == ["freshness"]


def test_quarantine_is_idempotent_by_obs_id(con):
    day = date(2026, 1, 5)
    row, created = store.quarantine_observation(
        con, "blue_spring_counts", "blue_spring_counts_daily", "2026-01-05", day,
        "count_range", "count_researchers=2000")
    again, created_again = store.quarantine_observation(
        con, "blue_spring_counts", "blue_spring_counts_daily", "2026-01-05", day,
        "count_jump", "count_researchers 10 to 2000")
    assert created and not created_again
    assert row["obs_id"] == "blue_spring_counts_daily:2026-01-05"
    assert again["check_name"] == "count_range"
    assert con.execute("SELECT count(*) FROM quarantine").fetchone()[0] == 1


def test_mark_alerted(con):
    inc, _ = store.open_incident(con, "s", "volume", "")
    store.quarantine_observation(con, "s", "t", "k", None, "c", 1)
    store.mark_alerted(con, [inc["incident_id"]], ["t:k"])
    assert con.execute("SELECT count(*) FROM incidents WHERE alerted_at IS NULL").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM quarantine WHERE alerted_at IS NULL").fetchone()[0] == 0
