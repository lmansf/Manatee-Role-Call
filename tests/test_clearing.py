"""Clearing quarantined observations: listing open items and recording decisions."""
from datetime import date, datetime, timezone

import duckdb
import pytest

from roll_call.quality import clearing

# The quarantine table as the contract defines it. roll_call/quality/store.py owns the real DDL.
QUARANTINE_DDL = """
CREATE TABLE quarantine (
    obs_id VARCHAR PRIMARY KEY, source VARCHAR, table_name VARCHAR, obs_key VARCHAR,
    observation_date DATE, check_name VARCHAR, value VARCHAR, opened_at TIMESTAMP,
    alerted_at TIMESTAMP
)
"""


def quarantine(con, day, check="count_range", value="2400", opened=None):
    key = day.isoformat()
    con.execute(
        "INSERT INTO quarantine VALUES (?, 'blue_spring_counts', 'blue_spring_counts_daily', ?, ?, ?, ?, ?, NULL)",
        [f"blue_spring_counts_daily:{key}", key, day, check, value,
         opened or datetime(day.year, day.month, day.day, 14, 0)],
    )
    return f"blue_spring_counts_daily:{key}"


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(QUARANTINE_DDL)
    yield c
    c.close()


def test_open_items_without_quarantine_table():
    c = duckdb.connect(":memory:")
    assert clearing.open_items(c) == []
    c.close()


def test_open_items_lists_undecided_oldest_first(con):
    later = quarantine(con, date(2026, 1, 9))
    earlier = quarantine(con, date(2026, 1, 5))
    decided = quarantine(con, date(2026, 1, 7))
    clearing.decide(con, decided, "confirmed", "Matches the park count.")
    items = clearing.open_items(con)
    assert [i["obs_id"] for i in items] == [earlier, later]
    assert items[0]["observation_date"] == date(2026, 1, 5)
    assert items[0]["check_name"] == "count_range" and items[0]["value"] == "2400"


def test_decide_records_decision_and_reason(con):
    obs = quarantine(con, date(2026, 1, 5))
    now = datetime(2026, 1, 8, 15, 30, tzinfo=timezone.utc)
    record = clearing.decide(con, obs, "rejected", "  Typo in the report: 24, not 2400. ", now=now)
    assert record["reason"] == "Typo in the report: 24, not 2400."
    assert con.execute("SELECT * FROM clearing_decisions").fetchall() == [
        (obs, "rejected", "Typo in the report: 24, not 2400.", datetime(2026, 1, 8, 15, 30))]
    assert clearing.open_items(con) == []


@pytest.mark.parametrize("decision", ["", "open", "Confirmed", "accept", None])
def test_decide_rejects_unknown_decisions(con, decision):
    obs = quarantine(con, date(2026, 1, 5))
    with pytest.raises(ValueError):
        clearing.decide(con, obs, decision, "a reason")


@pytest.mark.parametrize("reason", ["", "   ", "\n", None])
def test_decide_requires_a_reason(con, reason):
    obs = quarantine(con, date(2026, 1, 5))
    with pytest.raises(ValueError):
        clearing.decide(con, obs, "confirmed", reason)
    assert len(clearing.open_items(con)) == 1


def test_decide_requires_a_quarantined_observation(con):
    with pytest.raises(KeyError):
        clearing.decide(con, "blue_spring_counts_daily:2026-01-05", "confirmed", "Seen on video.")


def test_decide_refuses_a_second_decision_unless_replacing(con):
    obs = quarantine(con, date(2026, 1, 5))
    clearing.decide(con, obs, "rejected", "Looked like a typo.")
    with pytest.raises(ValueError):
        clearing.decide(con, obs, "confirmed", "The park count agrees.")
    clearing.decide(con, obs, "confirmed", "The park count agrees.", replace=True)
    assert con.execute("SELECT decision, reason FROM clearing_decisions").fetchall() == [
        ("confirmed", "The park count agrees.")]
