"""The alert email, with a fake HTTP session. No network."""
from datetime import date

import duckdb
import pytest
import requests

from roll_call.quality import alert, store
from roll_call.quality.checks import CheckReport


class FakeResponse:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse(self.status)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "Roll Call <alerts@example.com>")
    monkeypatch.setenv("ALERT_EMAIL_TO", "owner@example.com")


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    store.ensure_tables(c)
    yield c
    c.close()


def report_with_items(con):
    incident, _ = store.open_incident(con, "usgs_gauge_daily", "freshness",
                                      "last successful run 2027-01-08, 5 days ago")
    q, _ = store.quarantine_observation(
        con, "blue_spring_counts", "blue_spring_counts_daily", "2027-01-12", date(2027, 1, 12),
        "count_range", "count_researchers=2000")
    return CheckReport(today=date(2027, 1, 13), new_incidents=[incident], new_quarantine=[q],
                       con=con)


def test_nothing_new_sends_nothing(con):
    session = FakeSession()
    report = CheckReport(today=date(2027, 1, 13), closed_incidents=[{"incident_id": "x"}], con=con)
    assert alert.send_alert(report, session=session) is False
    assert session.calls == []


def test_sends_one_email_listing_new_items(con):
    session = FakeSession()
    report = report_with_items(con)
    assert alert.send_alert(report, session=session) is True
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://api.resend.com/emails"
    assert call["headers"]["Authorization"] == "Bearer re_test_key"
    assert call["headers"]["Idempotency-Key"]
    payload = call["json"]
    assert set(payload) == {"from", "to", "subject", "text"}
    assert payload["from"] == "Roll Call <alerts@example.com>"
    assert payload["to"] == ["owner@example.com"]
    assert payload["subject"] == ("Roll Call 2027-01-13: 1 new incident, "
                                  "1 new quarantined observation")
    assert "usgs_gauge_daily / freshness" in payload["text"]
    assert "blue_spring_counts_daily:2027-01-12 (count_range): count_researchers=2000" in payload["text"]


def test_alerted_at_is_stamped_after_sending(con):
    alert.send_alert(report_with_items(con), session=FakeSession())
    assert con.execute("SELECT count(*) FROM incidents WHERE alerted_at IS NULL").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM quarantine WHERE alerted_at IS NULL").fetchone()[0] == 0


def test_failed_send_raises_and_leaves_items_unalerted(con):
    with pytest.raises(requests.HTTPError):
        alert.send_alert(report_with_items(con), session=FakeSession(status=422))
    assert con.execute("SELECT count(*) FROM incidents WHERE alerted_at IS NULL").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM quarantine WHERE alerted_at IS NULL").fetchone()[0] == 1


def test_missing_key_fails_before_sending(con, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY")
    session = FakeSession()
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        alert.send_alert(report_with_items(con), session=session)
    assert session.calls == []
