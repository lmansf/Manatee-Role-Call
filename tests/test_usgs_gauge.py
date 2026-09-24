"""Tests for the USGS gauge request, parser and page walk. Network-free.

The fixtures `usgs_daily_synthetic_page*.json` are synthetic: hand-built in the shape of the
API's daily collection, because the API was unreachable when they were made.
"""
import json
import logging
from datetime import date
from pathlib import Path

import pytest

from roll_call.ingest import usgs_gauge

FIXTURES = Path(__file__).parent / "fixtures"
PAGE1 = FIXTURES / "usgs_daily_synthetic_page1.json"
PAGE2 = FIXTURES / "usgs_daily_synthetic_page2.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


class FakeResponse:
    def __init__(self, text: str, url: str):
        self.text = text
        self.url = url

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        return None


class FakeSession:
    """Serves pages by URL. The first request (to DAILY_URL with params) gets `first`."""

    def __init__(self, first: str, by_url: dict[str, str]):
        self.first = first
        self.by_url = by_url
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if params is not None:
            return FakeResponse(self.first, url)
        return FakeResponse(self.by_url[url], url)


def _next_href(payload: dict) -> str:
    return next(link["href"] for link in payload["links"] if link["rel"] == "next")


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    monkeypatch.delenv("API_USGS_PAT", raising=False)


def test_daily_params_target_water_temperature_daily_mean():
    p = usgs_gauge.build_daily_params(date(2024, 11, 1), date(2025, 3, 31))
    assert p["monitoring_location_id"] == "USGS-02236000"
    assert (p["parameter_code"], p["statistic_id"]) == ("00010", "00003")
    assert p["time"] == "2024-11-01/2025-03-31"
    assert p["limit"] == 50000


def test_parse_daily_keeps_unit_and_approval_status():
    rows = usgs_gauge.parse_daily(_load(PAGE1))
    assert rows == [
        {"obs_date": date(2025, 1, 6), "water_temp_c": 16.9, "unit": "degC", "approval_status": "Approved"},
        {"obs_date": date(2025, 1, 7), "water_temp_c": 16.4, "unit": "degC", "approval_status": "Approved"},
    ]
    assert all(isinstance(r["water_temp_c"], float) for r in rows)


def test_parse_daily_leaves_missing_day_unreported():
    rows = usgs_gauge.parse_daily(_load(PAGE2))
    assert [r["obs_date"] for r in rows] == [date(2025, 1, 9), date(2025, 1, 10)]
    assert {r["approval_status"] for r in rows} == {"Provisional"}


def test_parse_daily_keeps_a_unit_it_does_not_expect():
    payload = _load(PAGE1)
    payload["features"][0]["properties"]["unit_of_measure"] = "degF"
    payload["features"][0]["properties"]["value"] = "62.4"
    rows = usgs_gauge.parse_daily(payload)
    assert (rows[0]["unit"], rows[0]["water_temp_c"]) == ("degF", 62.4)
    assert rows[1]["unit"] == "degC"


def test_parse_daily_non_numeric_value_becomes_none_and_is_logged(caplog):
    payload = _load(PAGE1)
    payload["features"][0]["properties"]["value"] = "Eqp"
    payload["features"][1]["properties"]["value"] = None
    with caplog.at_level(logging.WARNING, logger=usgs_gauge.__name__):
        rows = usgs_gauge.parse_daily(payload)
    assert [r["water_temp_c"] for r in rows] == [None, None]
    assert any("2025-01-06" in m and "Eqp" in m for m in caplog.messages)


def test_parse_daily_empty_page():
    assert usgs_gauge.parse_daily({"type": "FeatureCollection", "features": []}) == []


def test_fetch_daily_follows_next_link_across_pages():
    page1, page2 = PAGE1.read_text(), PAGE2.read_text()
    sess = FakeSession(page1, {_next_href(json.loads(page1)): page2})

    result = usgs_gauge.fetch_daily(date(2025, 1, 6), date(2025, 1, 10), session=sess)

    assert len(sess.calls) == 2
    assert sess.calls[0][0] == usgs_gauge.DAILY_URL
    assert sess.calls[1][1] is None  # the next link carries the whole query
    assert [r["obs_date"] for r in result.records] == [
        date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 9), date(2025, 1, 10)
    ]
    assert [r["approval_status"] for r in result.records] == [
        "Approved", "Approved", "Provisional", "Provisional"
    ]
    assert json.loads(result.raw) == [page1, page2]
    assert result.run.status == "succeeded"
    assert result.run.row_count == 4
    assert result.run.source == "usgs_gauge_daily"
    assert result.run.payload_sha256


def test_fetch_daily_sends_api_key_on_every_page(monkeypatch):
    monkeypatch.setenv("API_USGS_PAT", "test-key")
    page1, page2 = PAGE1.read_text(), PAGE2.read_text()
    sess = FakeSession(page1, {_next_href(json.loads(page1)): page2})
    usgs_gauge.fetch_daily(date(2025, 1, 6), date(2025, 1, 10), session=sess)
    assert [c[2] for c in sess.calls] == [{"X-Api-Key": "test-key"}] * 2


def test_fetch_daily_stops_at_page_without_features():
    payload = _load(PAGE1)
    payload["features"] = []
    sess = FakeSession(json.dumps(payload), {})
    result = usgs_gauge.fetch_daily(date(2025, 1, 6), date(2025, 1, 10), session=sess)
    assert len(sess.calls) == 1
    assert result.records == []
    assert result.run.status == "succeeded"


def test_fetch_daily_fails_on_repeated_next_link():
    payload = _load(PAGE1)
    loop_url = _next_href(payload)
    for link in payload["links"]:
        link["href"] = loop_url  # the page names itself as next
    text = json.dumps(payload)
    sess = FakeSession(text, {loop_url: text})
    with pytest.raises(usgs_gauge.PaginationError, match="repeats"):
        usgs_gauge.fetch_daily(date(2025, 1, 6), date(2025, 1, 10), session=sess)
    assert len(sess.calls) == 2


def test_fetch_daily_fails_after_max_pages(monkeypatch):
    monkeypatch.setattr(usgs_gauge, "MAX_PAGES", 3)

    class EndlessSession:
        def __init__(self):
            self.calls = 0

        def get(self, url, params=None, headers=None, timeout=None):
            self.calls += 1
            payload = _load(PAGE1)
            for link in payload["links"]:
                if link["rel"] == "next":
                    link["href"] = f"{usgs_gauge.DAILY_URL}?cursor={self.calls}"
            return FakeResponse(json.dumps(payload), url)

    sess = EndlessSession()
    with pytest.raises(usgs_gauge.PaginationError, match="after 3 pages"):
        usgs_gauge.fetch_daily(date(2025, 1, 6), date(2025, 1, 10), session=sess)
    assert sess.calls == 3


def test_fetch_daily_refuses_next_link_to_another_host():
    payload = _load(PAGE1)
    for link in payload["links"]:
        if link["rel"] == "next":
            link["href"] = "https://elsewhere.example/items?cursor=2"
    sess = FakeSession(json.dumps(payload), {})
    with pytest.raises(usgs_gauge.PaginationError, match="elsewhere.example"):
        usgs_gauge.fetch_daily(date(2025, 1, 6), date(2025, 1, 10), session=sess)
    assert len(sess.calls) == 1


def test_next_page_url_resolves_relative_link():
    payload = {"features": [{}], "links": [{"rel": "next", "href": "items?cursor=2"}]}
    url = usgs_gauge.next_page_url(payload, usgs_gauge.DAILY_URL + "?f=json")
    assert url == usgs_gauge.DAILY_URL + "?cursor=2"


def test_next_page_url_empty_href_ends_walk():
    payload = {"features": [{}], "links": [{"rel": "next", "href": ""}]}
    assert usgs_gauge.next_page_url(payload, usgs_gauge.DAILY_URL) is None
