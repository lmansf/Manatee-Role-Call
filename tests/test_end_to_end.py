"""The whole daily run, every real module wired together, over one synthetic season.

Only the four network fetchers and the Resend request are replaced. Everything else is the real
code: storage, the quality checks, season state, baselines, the training gate, the model and the
dashboard export. The counts are the owner's hand-transcribed 2025-26 season
(data/reference), so the season opens, peaks and closes on real dates. Gauge and air
temperatures are synthetic and fall when the counts rise, as the refuge physics says they should.

Each module has its own tests against fakes of the others. This test exists to catch the seams
between them: a name, a column or a call order that only breaks when the real pieces meet.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from roll_call.export import dashboard
from roll_call.quality import season as season_module
from roll_call.ingest import blue_spring, open_meteo, usgs_gauge
from roll_call.ingest.base import IngestResult, IngestRun
from roll_call.storage import db

ROOT = Path(__file__).resolve().parents[1]
JOBS = ROOT / "jobs"
REFERENCE = ROOT / "data" / "reference" / "blue_spring_manatee_counts_2025_2026.csv"

SEASON_START = date(2025, 10, 20)
SEASON_END = date(2026, 4, 20)


def _load_job(name: str):
    if str(JOBS) not in sys.path:
        sys.path.insert(0, str(JOBS))
    spec = importlib.util.spec_from_file_location(name, JOBS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- the synthetic world -------------------------------------------------------------------

def _reference_counts() -> dict[date, int]:
    with REFERENCE.open(newline="") as f:
        return {date.fromisoformat(r["date"]): int(r["count"]) for r in csv.DictReader(f)
                if r["count_source"] != "Park staff"}


COUNTS = _reference_counts()


def _count_near(day: date) -> int:
    """The latest real count on or before day, so temperatures stay smooth over weekends."""
    known = [d for d in COUNTS if d <= day]
    return COUNTS[max(known)] if known else 0


def river_temp_c(day: date) -> float:
    """Colder river when more manatees gathered: roughly 23 °C empty, 15 °C at the peak."""
    return round(23.0 - _count_near(day) / 100.0, 2)


def air_mean_c(day: date) -> float:
    seasonal = 21.0 + 6.0 * math.cos((day.timetuple().tm_yday - 200) / 365.0 * 2 * math.pi)
    return round(min(seasonal, river_temp_c(day) + 2.0), 2)


def _weather_payload(start: date, end: date, lag_from: date | None, hourly: bool) -> dict:
    days = [start + timedelta(n) for n in range((end - start).days + 1)]

    def value(day: date, v: float):
        return None if lag_from and day >= lag_from else v

    daily = {
        "time": [d.isoformat() for d in days],
        "temperature_2m_min": [value(d, air_mean_c(d) - 4) for d in days],
        "temperature_2m_max": [value(d, air_mean_c(d) + 4) for d in days],
        "temperature_2m_mean": [value(d, air_mean_c(d)) for d in days],
        "precipitation_sum": [value(d, 1.0) for d in days],
        "wind_speed_10m_max": [value(d, 12.0) for d in days],
        "shortwave_radiation_sum": [value(d, 14.0) for d in days],
    }
    payload = {
        "latitude": 28.95, "longitude": -81.34, "elevation": 8.0, "generationtime_ms": 1.0,
        "utc_offset_seconds": -18000, "timezone": "America/New_York",
        "timezone_abbreviation": "EST",
        "daily_units": {"time": "iso8601", "temperature_2m_min": "°C",
                        "temperature_2m_max": "°C", "temperature_2m_mean": "°C",
                        "precipitation_sum": "mm", "wind_speed_10m_max": "km/h",
                        "shortwave_radiation_sum": "MJ/m²"},
        "daily": daily,
    }
    if hourly:
        stamps = [datetime(d.year, d.month, d.day, h) for d in days for h in range(24)]
        payload["hourly_units"] = {"time": "iso8601", "temperature_2m": "°C"}
        payload["hourly"] = {
            "time": [s.strftime("%Y-%m-%dT%H:%M") for s in stamps],
            "temperature_2m": [value(s.date(), air_mean_c(s.date()) + 3 * math.sin(s.hour / 24 * 2 * math.pi))
                               for s in stamps],
        }
    return payload


class World:
    """The fake network. `today` moves forward one day at a time."""

    def __init__(self) -> None:
        self.today = SEASON_START
        self.emails: list[dict] = []

    def _result(self, source: str, records: list[dict], raw: str) -> IngestResult:
        # Stamp the run at 19:00 Florida time on the simulated day, as the timer would.
        started = datetime(self.today.year, self.today.month, self.today.day, 19,
                           tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        run = IngestRun(source=source, started_at=started).succeed(len(records))
        return IngestResult(run=run, raw=raw, records=records)

    def fetch_reports(self, since=None, session=None):
        rows = []
        for day, count in sorted(COUNTS.items()):
            if day > self.today or (since and day < since):
                continue
            rows.append({"report_date": day, "count_researchers": count, "count_park": None,
                         "not_counted": False, "is_estimate": False,
                         "river_temp_f": round(river_temp_c(day) * 9 / 5 + 32 + 0.5, 1),
                         "spring_temp_f": 72.0, "post_url": blue_spring.LISTING_URL,
                         "count_text": f"{count} manatees for roll call."})
        return self._result(blue_spring.SOURCE_NAME, rows, "<html></html>")

    def fetch_archive(self, start, end, session=None):
        payload = _weather_payload(start, end, lag_from=self.today - timedelta(days=5), hourly=True)
        return self._result(open_meteo.SOURCE_NAME, open_meteo.parse_daily(payload), json.dumps(payload))

    def fetch_forecast(self, issue_date=None, session=None):
        issue = issue_date or self.today
        payload = _weather_payload(issue, issue + timedelta(days=6), lag_from=None, hourly=False)
        return self._result(open_meteo.FORECAST_SOURCE_NAME,
                            open_meteo.parse_forecast(payload, issue), json.dumps(payload))

    def fetch_daily(self, start, end, session=None):
        rows = []
        day = start
        while day <= min(end, self.today):
            recent = (self.today - day).days < 30
            rows.append({"obs_date": day, "water_temp_c": river_temp_c(day), "unit": "degC",
                         "approval_status": "Provisional" if recent else "Approved"})
            day += timedelta(days=1)
        return self._result(usgs_gauge.SOURCE_NAME, rows, json.dumps(["{}"]))


class FakeResend:
    def __init__(self, world: World) -> None:
        self.world = world

    def post(self, url, json=None, headers=None, timeout=None):
        self.world.emails.append(json)

        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"id": "fake"}

        return Response()


# --- the run -------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def season(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield from _run_season(tmp_path_factory.mktemp("season"), monkeypatch)


def _run_season(tmp_path, monkeypatch):
    world = World()
    monkeypatch.setattr(blue_spring, "fetch_reports", world.fetch_reports)
    monkeypatch.setattr(open_meteo, "fetch_archive", world.fetch_archive)
    monkeypatch.setattr(open_meteo, "fetch_forecast", world.fetch_forecast)
    monkeypatch.setattr(usgs_gauge, "fetch_daily", world.fetch_daily)
    for key, value in {"RESEND_API_KEY": "test", "ALERT_EMAIL_FROM": "a@example.com",
                       "ALERT_EMAIL_TO": "b@example.com"}.items():
        monkeypatch.setenv(key, value)
    import requests
    monkeypatch.setattr(requests, "Session", lambda: FakeResend(world))

    ingest_daily = _load_job("ingest_daily")
    backfill_weather = _load_job("backfill_weather")
    publish_dashboard = _load_job("publish_dashboard")
    out_dir = tmp_path / "dashboard"
    monkeypatch.setattr(publish_dashboard, "publish",
                        lambda con, **_: dashboard.export(con, out_dir=out_dir, git_sha="test"))

    con = db.connect(tmp_path / "roll_call.duckdb")
    # Weather history for the baselines, as the one-off backfill would load it.
    world.today = SEASON_START
    failed_years = backfill_weather.backfill(con, SEASON_START.year - 31, SEASON_START.year,
                                             today=SEASON_START, pause=0)
    assert failed_years == []

    failures: dict[date, list[str]] = {}
    day = SEASON_START
    while day <= SEASON_END:
        world.today = day
        state = ingest_daily.run_stages(con, day)
        if state.failed:
            failures[day] = state.failed
        day += timedelta(days=1)
    yield con, world, failures, out_dir
    con.close()


def test_every_stage_succeeds_every_day(season):
    _, _, failures, _ = season
    assert failures == {}


def test_model_trains_and_predicts_in_season(season):
    con, *_ = season
    versions = con.execute("SELECT count(*) FROM model_versions").fetchone()[0]
    predictions = con.execute(
        "SELECT min(target_date), max(target_date), count(*) FROM predictions").fetchone()
    assert versions >= 1
    assert predictions[2] > 30
    # Predictions stop once the season closes: five silent weekdays after the last report.
    closed_on = season_module.state(con, SEASON_END).closed_on
    assert closed_on is not None
    assert predictions[0] > SEASON_START and predictions[1] <= closed_on + timedelta(days=1)


def test_season_closes_and_baselines_refresh_once(season):
    con, *_ = season
    live = con.execute(
        "SELECT DISTINCT refreshed_on FROM baseline_refreshes WHERE NOT replayed").fetchall()
    assert len(live) == 1
    assert date(2026, 3, 1) < live[0][0] <= SEASON_END


def test_dashboard_files_carry_the_season(season):
    _, _, _, out_dir = season
    status = list(csv.DictReader((out_dir / "source_status.csv").open()))
    assert {r["source"] for r in status} >= {"blue_spring_counts", "open_meteo_archive",
                                             "open_meteo_forecast", "usgs_gauge_daily"}
    drift = list(csv.DictReader((out_dir / "baseline_drift.csv").open()))
    assert drift, "the season-close refresh should appear in the drift file"
    for path in out_dir.glob("*.csv"):
        text = path.read_text()
        assert "manatees for roll call" not in text, f"report text leaked into {path.name}"
