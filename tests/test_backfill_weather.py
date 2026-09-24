"""The weather backfill: one archive request per calendar year, written like any other run."""
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from roll_call import config
from roll_call.ingest import open_meteo
from roll_call.ingest.base import IngestResult, IngestRun
from roll_call.storage import db

_spec = importlib.util.spec_from_file_location(
    "backfill_weather", Path(__file__).resolve().parents[1] / "jobs" / "backfill_weather.py")
backfill_weather = importlib.util.module_from_spec(_spec)
sys.modules["backfill_weather"] = backfill_weather
_spec.loader.exec_module(backfill_weather)

T = config.TABLES


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "test.duckdb")
    yield c
    c.close()


@pytest.fixture
def archive(monkeypatch):
    """A fake archive that returns the first and last day of the window, with one hourly row
    each. Years listed in `broken` raise."""
    calls: list[tuple[date, date]] = []
    broken: set[int] = set()

    def fetch_archive(start, end, session=None):
        calls.append((start, end))
        if start.year in broken:
            raise ConnectionError("timed out")
        days = [start, end]
        raw = json.dumps({"hourly": {"time": [f"{d.isoformat()}T12:00" for d in days]}})
        records = [{"obs_date": d, **{f: 1.0 for f in open_meteo.DAILY_FIELDS}, "units": "{}"} for d in days]
        return IngestResult(run=IngestRun(source=open_meteo.SOURCE_NAME).succeed(len(records)),
                            raw=raw, records=records)

    def parse_hourly(payload):
        return [{"obs_ts": datetime.fromisoformat(t), "temperature_2m": 5.0, "unit": "°C"}
                for t in payload["hourly"]["time"]]

    monkeypatch.setattr(open_meteo, "fetch_archive", fetch_archive)
    monkeypatch.setattr(open_meteo, "parse_hourly", parse_hourly)
    return calls, broken


def test_one_request_per_calendar_year(con, archive):
    calls, _ = archive
    failed = backfill_weather.backfill(con, 1988, 1990, today=date(2026, 9, 24), pause=0)
    assert failed == []
    assert calls == [(date(y, 1, 1), date(y, 12, 31)) for y in (1988, 1989, 1990)]
    assert con.execute(f"SELECT count(*) FROM {T.weather_daily}").fetchone() == (6,)
    assert con.execute(f"SELECT count(*) FROM {T.weather_hourly}").fetchone() == (6,)
    assert con.execute(f"SELECT count(*) FROM {T.weather_raw}").fetchone() == (3,)


def test_current_year_stops_yesterday_and_future_years_are_skipped(con, archive):
    calls, _ = archive
    today = date(2026, 9, 24)
    backfill_weather.backfill(con, 2025, 2027, today=today, pause=0)
    assert calls == [(date(2025, 1, 1), date(2025, 12, 31)), (date(2026, 1, 1), today - timedelta(days=1))]


def test_rerun_replaces_rows(con, archive):
    for _ in range(2):
        backfill_weather.backfill(con, 2000, 2001, today=date(2026, 9, 24), pause=0)
    assert con.execute(f"SELECT count(*) FROM {T.weather_daily}").fetchone() == (4,)
    assert con.execute(f"SELECT count(*) FROM {T.weather_hourly}").fetchone() == (4,)


def test_failed_year_is_recorded_and_the_loop_goes_on(con, archive):
    calls, broken = archive
    broken.add(1989)
    failed = backfill_weather.backfill(con, 1988, 1990, today=date(2026, 9, 24), pause=0)
    assert failed == [1989]
    assert len(calls) == 3
    statuses = sorted(r[0] for r in con.execute(f"SELECT status FROM {T.ingest_runs}").fetchall())
    assert statuses == ["failed", "succeeded", "succeeded"]


def test_cli_exit_code(tmp_path, archive, monkeypatch):
    _, broken = archive
    real_main = backfill_weather.main
    monkeypatch.setattr(backfill_weather, "main", lambda s, e, pause: real_main(
        s, e, db_path=str(tmp_path / "cli.duckdb"), pause=pause))
    assert backfill_weather.cli(["2001", "2002", "--pause", "0"]) == 0
    broken.add(2002)
    assert backfill_weather.cli(["2001", "2002", "--pause", "0"]) == 1


def test_start_after_end_is_refused(con):
    with pytest.raises(ValueError):
        backfill_weather.backfill(con, 2001, 2000, pause=0)
