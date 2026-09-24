"""Tests for the Open-Meteo parser and fetchers. Network-free.

Both fixtures are synthetic. The sandbox that wrote them could not reach Open-Meteo, so they
follow the response shape documented in docs/sources.md section 1, with made-up values.
Replace them with saved real responses when one is available, and keep these as the
regression cases for that shape.

- open_meteo_archive_sample.json: archive call for 2024-01-10 to 2024-01-14, daily and
  hourly blocks. The last two days are null, as inside the archive's ~5 day lag.
- open_meteo_forecast_sample.json: 7-day forecast starting 2024-01-15, daily block only.
"""
import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from roll_call.ingest import open_meteo

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def archive() -> dict:
    return _load("open_meteo_archive_sample.json")


@pytest.fixture
def forecast() -> dict:
    return _load("open_meteo_forecast_sample.json")


class FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.response


def test_archive_params_cover_window():
    p = open_meteo.build_archive_params(date(2024, 1, 1), date(2024, 1, 31))
    assert p["start_date"] == "2024-01-01"
    assert p["end_date"] == "2024-01-31"
    assert "temperature_2m_max" in p["daily"]
    assert p["hourly"] == "temperature_2m"
    assert p["temperature_unit"] == "celsius"


def test_forecast_params_never_request_past_days():
    p = open_meteo.build_forecast_params()
    assert "past_days" not in p
    assert p["forecast_days"] == 7


def test_parse_daily_one_row_per_day(archive):
    rows = open_meteo.parse_daily(archive)
    assert [r["obs_date"] for r in rows] == [date(2024, 1, d) for d in range(10, 15)]
    assert set(rows[0]) == {"obs_date", *open_meteo.DAILY_FIELDS, "units"}
    assert rows[0]["temperature_2m_max"] == 20.0
    assert rows[1]["precipitation_sum"] == 4.2
    assert json.loads(rows[0]["units"]) == archive["daily_units"]
    assert json.loads(rows[0]["units"])["temperature_2m_max"] == "°C"


def test_parse_daily_keeps_null_days(archive):
    rows = open_meteo.parse_daily(archive)
    assert len(rows) == 5
    for row in rows[-2:]:
        assert all(row[f] is None for f in open_meteo.DAILY_FIELDS)
        assert row["units"] == rows[0]["units"]


def test_parse_daily_rejects_misaligned_arrays(archive):
    broken = copy.deepcopy(archive)
    broken["daily"]["precipitation_sum"].pop()
    with pytest.raises(ValueError, match="differ in length.*precipitation_sum=4"):
        open_meteo.parse_daily(broken)


def test_parse_daily_rejects_missing_field(archive):
    broken = copy.deepcopy(archive)
    del broken["daily"]["wind_speed_10m_max"]
    with pytest.raises(ValueError, match="missing wind_speed_10m_max"):
        open_meteo.parse_daily(broken)


def test_parse_hourly_naive_local_timestamps(archive):
    rows = open_meteo.parse_hourly(archive)
    assert len(rows) == 5 * 24
    assert set(rows[0]) == {"obs_ts", "temperature_2m", "unit"}
    assert rows[0]["obs_ts"] == datetime(2024, 1, 10, 0, 0)
    assert rows[0]["obs_ts"].tzinfo is None
    assert rows[-1]["obs_ts"] == datetime(2024, 1, 14, 23, 0)
    assert rows[0]["unit"] == "°C"
    assert rows[0]["temperature_2m"] is not None
    # Hours inside the archive lag stay as rows with None values.
    assert all(r["temperature_2m"] is None for r in rows if r["obs_ts"].day >= 13)


def test_parse_hourly_groups_to_the_daily_mean(archive):
    """Grouping hourly rows by local date lines up with the daily block's days."""
    hourly = open_meteo.parse_hourly(archive)
    daily = {r["obs_date"]: r for r in open_meteo.parse_daily(archive)}
    by_day: dict[date, list[float]] = {}
    for r in hourly:
        if r["temperature_2m"] is not None:
            by_day.setdefault(r["obs_ts"].date(), []).append(r["temperature_2m"])
    assert set(by_day) == {date(2024, 1, d) for d in (10, 11, 12)}
    for day, values in by_day.items():
        assert len(values) == 24
        assert sum(values) / 24 == pytest.approx(daily[day]["temperature_2m_mean"], abs=0.1)


def test_parse_hourly_rejects_misaligned_arrays(archive):
    broken = copy.deepcopy(archive)
    broken["hourly"]["time"].append("2024-01-15T00:00")
    with pytest.raises(ValueError, match="'hourly' arrays differ in length"):
        open_meteo.parse_hourly(broken)


def test_parse_forecast_issue_and_target_dates(forecast):
    rows = open_meteo.parse_forecast(forecast, date(2024, 1, 15))
    assert len(rows) == 7
    assert {r["issue_date"] for r in rows} == {date(2024, 1, 15)}
    assert [r["target_date"] for r in rows] == [date(2024, 1, d) for d in range(15, 22)]
    assert set(rows[0]) == {"issue_date", "target_date", *open_meteo.DAILY_FIELDS, "units"}
    assert rows[4]["precipitation_sum"] == 5.6


def test_fetch_forecast_with_fake_session(forecast):
    text = json.dumps(forecast, ensure_ascii=False)
    session = FakeSession(FakeResponse(text))
    result = open_meteo.fetch_forecast(issue_date=date(2024, 1, 15), session=session)

    url, params = session.calls[0]
    assert url == open_meteo.FORECAST_URL
    assert params == open_meteo.build_forecast_params()
    assert result.raw == text
    assert result.run.source == "open_meteo_forecast"
    assert result.run.status == "succeeded"
    assert result.run.row_count == 7
    assert result.run.payload_sha256 is not None
    assert result.records[0]["issue_date"] == date(2024, 1, 15)
    assert result.records[0]["target_date"] == date(2024, 1, 15)


def test_fetch_forecast_issue_date_defaults_to_local_today(forecast, monkeypatch):
    monkeypatch.setattr(open_meteo, "local_today", lambda: date(2024, 1, 14))
    session = FakeSession(FakeResponse(json.dumps(forecast)))
    result = open_meteo.fetch_forecast(session=session)
    assert {r["issue_date"] for r in result.records} == {date(2024, 1, 14)}


def test_local_today_uses_new_york_time(monkeypatch):
    """03:30 UTC on 15 January is still 14 January in Florida."""

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2024, 1, 15, 3, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(open_meteo, "datetime", FrozenDatetime)
    assert open_meteo.local_today() == date(2024, 1, 14)


def test_fetch_forecast_raises_on_http_error():
    session = FakeSession(FakeResponse("{}", status=500))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        open_meteo.fetch_forecast(issue_date=date(2024, 1, 15), session=session)


def test_fetch_archive_with_fake_session(archive):
    text = json.dumps(archive, ensure_ascii=False)
    session = FakeSession(FakeResponse(text))
    result = open_meteo.fetch_archive(date(2024, 1, 10), date(2024, 1, 14), session=session)

    url, params = session.calls[0]
    assert url == open_meteo.ARCHIVE_URL
    assert params["start_date"] == "2024-01-10"
    assert result.run.source == "open_meteo_archive"
    assert result.run.row_count == 5
    assert len(open_meteo.parse_hourly(json.loads(result.raw))) == 120
