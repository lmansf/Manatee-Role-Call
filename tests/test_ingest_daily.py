"""The daily run's order, catch-up windows and failure handling. Every fetcher and every
downstream interface (quality, model, publish) is a fake; nothing touches the network."""
import importlib
import importlib.util
import sys
import types
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from roll_call import config
from roll_call.ingest import blue_spring, open_meteo, usgs_gauge
from roll_call.ingest.base import IngestResult, IngestRun
from roll_call.storage import db

_spec = importlib.util.spec_from_file_location(
    "ingest_daily", Path(__file__).resolve().parents[1] / "jobs" / "ingest_daily.py")
ingest_daily = importlib.util.module_from_spec(_spec)
sys.modules["ingest_daily"] = ingest_daily
_spec.loader.exec_module(ingest_daily)

T = config.TABLES
TODAY = date(2026, 1, 15)


def install_fake(monkeypatch, dotted: str, **attrs) -> types.ModuleType:
    """Put a fake module at `dotted` for this test. Parent packages that do not exist yet in
    this checkout are faked too; real ones are kept."""
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent in sys.modules:
            continue
        try:
            importlib.import_module(parent)
        except ImportError:
            pkg = types.ModuleType(parent)
            pkg.__path__ = []
            monkeypatch.setitem(sys.modules, parent, pkg)
            if i > 1:
                monkeypatch.setattr(sys.modules[".".join(parts[:i - 1])], parts[i - 1], pkg, raising=False)
    fake = types.ModuleType(dotted)
    for name, value in attrs.items():
        setattr(fake, name, value)
    monkeypatch.setitem(sys.modules, dotted, fake)
    monkeypatch.setattr(sys.modules[".".join(parts[:-1])], parts[-1], fake, raising=False)
    return fake


def _result(source, records):
    return IngestResult(run=IngestRun(source=source).succeed(len(records)), raw="{}", records=records)


def _weather(**key):
    return {**key, **{f: 10.0 for f in open_meteo.DAILY_FIELDS}, "units": "{}"}


class Pipeline:
    """Fakes for every fetcher and downstream interface. `calls` records the order."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple] = []
        self.fail: set[str] = set()
        calls, fail = self.calls, self.fail

        def step(name, *args, value=None):
            calls.append((name, *args))
            if name in fail:
                raise RuntimeError(f"{name} broke")
            return value

        def fetch_reports(since=None, session=None):
            step("fetch_reports", since)
            return _result("blue_spring_counts", [{
                "report_date": date(2026, 1, 14), "count_researchers": 300, "count_park": 310,
                "not_counted": False, "is_estimate": False, "river_temp_f": 60.0,
                "spring_temp_f": 72.0, "post_url": "https://example.org/r", "count_text": "x"}])

        def fetch_archive(start, end, session=None):
            step("fetch_archive", start, end)
            return _result("open_meteo_archive", [_weather(obs_date=date(2026, 1, 9))])

        def fetch_forecast(issue_date=None, session=None):
            step("fetch_forecast", issue_date)
            return _result("open_meteo_forecast", [_weather(issue_date=issue_date, target_date=date(2026, 1, 16))])

        def fetch_daily(start, end, session=None):
            step("fetch_daily", start, end)
            return _result("usgs_gauge_daily", [{"obs_date": date(2026, 1, 14), "water_temp_c": 16.0,
                                                 "unit": "degC", "approval_status": "Provisional"}])

        report = types.SimpleNamespace(new_incidents=[], closed_incidents=[], new_quarantine=[])
        monkeypatch.setattr(blue_spring, "fetch_reports", fetch_reports)
        monkeypatch.setattr(open_meteo, "fetch_archive", fetch_archive)
        monkeypatch.setattr(open_meteo, "fetch_forecast", fetch_forecast)
        monkeypatch.setattr(open_meteo, "parse_hourly", lambda payload: [])
        monkeypatch.setattr(usgs_gauge, "fetch_daily", fetch_daily)
        install_fake(monkeypatch, "roll_call.quality.checks",
                     run_checks=lambda con, today: step("run_checks", today, value=report))
        install_fake(monkeypatch, "roll_call.quality.alert",
                     send_alert=lambda r: step("send_alert", r is report, value=False))
        install_fake(monkeypatch, "roll_call.quality.baselines",
                     refresh_if_due=lambda con, today: step("refresh_if_due", today, value=False),
                     ensure_tables=lambda con: None)
        install_fake(monkeypatch, "roll_call.quality.store", ensure_tables=lambda con: None)
        install_fake(monkeypatch, "roll_call.model.store", ensure_tables=lambda con: None)
        install_fake(monkeypatch, "roll_call.model.train",
                     retrain_if_needed=lambda con, today: step("retrain_if_needed", today, value=False))
        install_fake(monkeypatch, "roll_call.model.predict",
                     run=lambda con, today: step("predict.run", today, value={"predicted": 290.0}))
        monkeypatch.setitem(sys.modules, "publish_dashboard", types.SimpleNamespace(
            publish=lambda con: step("publish", con is not None, value=True)))

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def pipeline(monkeypatch):
    return Pipeline(monkeypatch)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "roll_call.duckdb")


def run(db_path, today=TODAY):
    return ingest_daily.main(["--today", today.isoformat()], db_path=db_path)


def _rows(db_path, sql):
    con = db.connect(db_path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_stages_run_in_contract_order(pipeline, db_path):
    assert run(db_path) == 0
    assert pipeline.names() == [
        "fetch_reports", "fetch_archive", "fetch_forecast", "fetch_daily", "run_checks",
        "send_alert", "refresh_if_due", "retrain_if_needed", "predict.run", "publish"]
    assert ("fetch_forecast", TODAY) in pipeline.calls
    assert ("send_alert", True) in pipeline.calls  # the checks' report is what gets alerted on


def test_first_run_windows(pipeline, db_path):
    run(db_path)
    calls = {c[0]: c[1:] for c in pipeline.calls}
    assert calls["fetch_reports"] == (date(2025, 7, 1),)  # the current season year
    assert calls["fetch_archive"] == (date(2025, 12, 16), date(2026, 1, 14))  # 30 days, to yesterday
    assert calls["fetch_daily"] == (date(2025, 12, 16), TODAY)


def test_catch_up_windows_reach_back_past_the_last_success(pipeline, db_path):
    con = db.connect(db_path)
    # 00:30 UTC on 10 January is still 9 January in Florida.
    last = datetime(2026, 1, 10, 0, 30, tzinfo=timezone.utc)
    for source in ("blue_spring_counts", "open_meteo_archive", "usgs_gauge_daily"):
        db.record_run(con, IngestRun(source=source, started_at=last).succeed(1))
    con.close()
    run(db_path)
    calls = {c[0]: c[1:] for c in pipeline.calls}
    assert calls["fetch_reports"] == (date(2026, 1, 2),)  # 9 Jan minus 7 days
    assert calls["fetch_archive"] == (date(2025, 12, 30), date(2026, 1, 14))  # minus 10 days
    assert calls["fetch_daily"] == (date(2025, 12, 26), TODAY)  # minus 14 days


def test_overlaps_cover_the_known_lags():
    assert ingest_daily.ARCHIVE_OVERLAP_DAYS >= 7
    assert ingest_daily.GAUGE_OVERLAP_DAYS >= 14


@pytest.mark.parametrize("today, start", [
    (date(2026, 1, 15), date(2025, 7, 1)),
    (date(2026, 7, 1), date(2026, 7, 1)),
    (date(2026, 9, 24), date(2026, 7, 1)),
])
def test_season_year_start(today, start):
    assert ingest_daily.season_year_start(today) == start


def test_running_twice_gives_the_same_rows(pipeline, db_path):
    tables = (T.counts_daily, T.weather_daily, T.weather_forecast, T.gauge_daily)
    run(db_path)
    first = {t: _rows(db_path, f"SELECT * EXCLUDE (run_id, ingested_at) FROM {t} ORDER BY ALL") for t in tables}
    run(db_path)
    second = {t: _rows(db_path, f"SELECT * EXCLUDE (run_id, ingested_at) FROM {t} ORDER BY ALL") for t in tables}
    assert first == second
    assert all(len(rows) == 1 for rows in second.values())


def test_failing_fetch_is_recorded_and_later_stages_still_run(pipeline, db_path):
    pipeline.fail.add("fetch_archive")
    assert run(db_path) == 1
    assert pipeline.names()[-1] == "publish"
    assert "fetch_daily" in pipeline.names()
    runs = dict(_rows(db_path, f"SELECT source, status FROM {T.ingest_runs}"))
    assert runs == {"blue_spring_counts": "succeeded", "open_meteo_archive": "failed",
                    "open_meteo_forecast": "succeeded", "usgs_gauge_daily": "succeeded"}


def test_failed_checks_skip_the_alert_but_not_the_rest(pipeline, db_path):
    pipeline.fail.add("run_checks")
    assert run(db_path) == 1
    names = pipeline.names()
    assert "send_alert" not in names
    assert names[-4:] == ["refresh_if_due", "retrain_if_needed", "predict.run", "publish"]


def test_failed_publish_fails_the_run(pipeline, db_path):
    pipeline.fail.add("publish")
    assert run(db_path) == 1


def test_catch_up_never_starts_after_the_day_being_run():
    """A re-run of a past day (--today) measures its window from that day, not from the
    wall-clock time of a later successful run."""
    from datetime import datetime, timezone
    later_run = datetime(2026, 9, 24, 23, 0, tzinfo=timezone.utc)
    start = ingest_daily.catch_up_start(later_run, 7, date(2025, 7, 1), today=date(2025, 12, 1))
    assert start == date(2025, 11, 24)
