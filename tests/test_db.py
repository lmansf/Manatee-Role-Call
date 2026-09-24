"""The run log, the ingest writes and the backup job, against a throwaway DuckDB file."""
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from roll_call import config
from roll_call.ingest import open_meteo
from roll_call.ingest.base import IngestResult, IngestRun
from roll_call.storage import db

_spec = importlib.util.spec_from_file_location(
    "backup_db", Path(__file__).resolve().parents[1] / "jobs" / "backup_db.py")
backup_db = importlib.util.module_from_spec(_spec)
sys.modules["backup_db"] = backup_db
_spec.loader.exec_module(backup_db)


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "test.duckdb")
    yield c
    c.close()


def test_last_successful_run_ignores_failures(con):
    t0 = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
    ok = IngestRun(source="s", started_at=t0).succeed(3)
    later_failure = IngestRun(source="s", started_at=t0 + timedelta(days=1)).fail(RuntimeError("x"))
    other_source = IngestRun(source="other", started_at=t0 + timedelta(days=2)).succeed(1)
    for r in (ok, later_failure, other_source):
        db.record_run(con, r)
    assert db.last_successful_run(con, "s") == datetime(2026, 1, 5, 0, 0)
    assert db.last_successful_run(con, "never") is None


def test_record_run_is_idempotent(con):
    run = IngestRun(source="s")
    db.record_run(con, run)
    db.record_run(con, run.succeed(7))
    rows = con.execute("SELECT status, row_count FROM ingest_runs").fetchall()
    assert rows == [("succeeded", 7)]


def test_timestamps_stored_as_utc(con):
    est = timezone(timedelta(hours=-5))
    db.record_run(con, IngestRun(source="s", started_at=datetime(2026, 1, 5, 19, 0, tzinfo=est)).succeed(0))
    assert db.last_successful_run(con, "s") == datetime(2026, 1, 6, 0, 0)


def test_export_records_writes_existing_tables_only(con, tmp_path):
    con.execute("CREATE TABLE clearing_decisions (obs_id VARCHAR, decision VARCHAR, reason VARCHAR)")
    con.execute("INSERT INTO clearing_decisions VALUES ('b', 'rejected', 'parser bug'), ('a', 'confirmed', 'cold snap')")
    written = backup_db.export_records(con, tmp_path / "records")
    assert [p.name for p in written] == ["clearing_decisions.csv"]
    lines = written[0].read_text().splitlines()
    assert lines[0] == "obs_id,decision,reason" and lines[1].startswith("a,")


def test_copy_database_prunes_and_refuses_missing_dir(tmp_path):
    src = tmp_path / "roll_call.duckdb"
    src.write_bytes(b"x")
    dest = tmp_path / "backups"
    with pytest.raises(RuntimeError, match="mounted"):
        backup_db.copy_database(src, dest)
    dest.mkdir()
    for d in ("2026-01-01", "2026-01-08", "2026-01-15"):
        (dest / f"roll_call_{d}.duckdb").write_bytes(b"old")
    target = backup_db.copy_database(src, dest, keep=2)
    assert target.exists()
    assert len(list(dest.glob("roll_call_*.duckdb"))) == 2


# Ingest writes

T = config.TABLES


def _result(source, records, raw="payload"):
    run = IngestRun(source=source).succeed(len(records))
    return IngestResult(run=run, raw=raw, records=records)


def _gauge(day, value, status):
    return {"obs_date": day, "water_temp_c": value, "unit": "degC", "approval_status": status}


def _count(day, researchers):
    return {"report_date": day, "count_researchers": researchers, "count_park": None,
            "not_counted": researchers is None, "is_estimate": False, "river_temp_f": 61.0,
            "spring_temp_f": 72.0, "post_url": "https://example.org/r", "count_text": "x"}


def _weather(**key):
    """A daily weather row. `key` is obs_date, or issue_date and target_date for a forecast."""
    return {**key, **{f: 1.5 for f in open_meteo.DAILY_FIELDS},
            "units": {"temperature_2m_min": "°C"}}


def _count_rows(con, table):
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_connect_creates_every_ingested_table(con):
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert set(db.PRIMARY_KEYS) | {T.ingest_runs} <= tables


def test_write_is_idempotent(con):
    records = [_count(date(2026, 1, 5), 120), _count(date(2026, 1, 6), None)]
    for _ in range(2):
        db.write_ingest_result(con, _result("blue_spring_counts", records), T.counts_raw, T.counts_daily)
    rows = con.execute(f"SELECT report_date, count_researchers, not_counted FROM {T.counts_daily} "
                       "ORDER BY report_date").fetchall()
    assert rows == [(date(2026, 1, 5), 120, False), (date(2026, 1, 6), None, True)]
    assert _count_rows(con, T.counts_raw) == 2  # one raw row per run
    assert con.execute(f"SELECT count(*) FROM {T.ingest_runs} WHERE status = 'succeeded'").fetchone() == (2,)


def test_rewrite_replaces_value_and_stamps_the_new_run(con):
    day = date(2026, 1, 5)
    db.write_ingest_result(con, _result("blue_spring_counts", [_count(day, 100)]), T.counts_raw, T.counts_daily)
    second = _result("blue_spring_counts", [_count(day, 104)])
    db.write_ingest_result(con, second, T.counts_raw, T.counts_daily)
    assert con.execute(f"SELECT count_researchers, run_id FROM {T.counts_daily}").fetchall() == \
        [(104, second.run.run_id)]


def test_gauge_revision_keeps_provisional_and_approved(con):
    day = date(2026, 1, 5)
    for rows in ([_gauge(day, 17.9, "Provisional")],
                 [_gauge(day, 17.9, "Provisional")],
                 [_gauge(day, 18.1, "Approved")]):
        db.write_ingest_result(con, _result("usgs_gauge_daily", rows), T.gauge_raw, T.gauge_daily)
    rows = con.execute(f"SELECT approval_status, water_temp_c FROM {T.gauge_daily} ORDER BY 1").fetchall()
    assert rows == [("Approved", 18.1), ("Provisional", 17.9)]


def test_forecast_issues_are_kept_side_by_side(con):
    target = date(2026, 1, 10)
    for issue in (date(2026, 1, 8), date(2026, 1, 9), date(2026, 1, 9)):
        rec = _weather(issue_date=issue, target_date=target)
        db.write_ingest_result(con, _result("open_meteo_forecast", [rec]), T.weather_raw, T.weather_forecast)
    assert _count_rows(con, T.weather_forecast) == 2
    assert con.execute(f"SELECT DISTINCT source FROM {T.weather_raw}").fetchall() == [("open_meteo_forecast",)]


def test_failed_write_rolls_back_keeps_raw_and_records_failed_run(con):
    bad = _result("usgs_gauge_daily", [_gauge(date(2026, 1, 5), 17.9, "Provisional"),
                                       {"obs_date": date(2026, 1, 6)}], raw="page text")
    with pytest.raises(KeyError):
        db.write_ingest_result(con, bad, T.gauge_raw, T.gauge_daily)
    assert _count_rows(con, T.gauge_daily) == 0
    assert con.execute(f"SELECT payload FROM {T.gauge_raw}").fetchall() == [("page text",)]
    assert con.execute(f"SELECT status FROM {T.ingest_runs}").fetchall() == [("failed",)]


def test_failed_fetch_records_failed_run(con):
    def boom():
        raise ConnectionError("no route to host")

    with pytest.raises(ConnectionError):
        db.fetch_and_write(con, "usgs_gauge_daily", boom, lambda c, r: None)
    rows = con.execute(f"SELECT source, status, error FROM {T.ingest_runs}").fetchall()
    assert rows == [("usgs_gauge_daily", "failed", "ConnectionError: no route to host")]
    assert db.last_successful_run(con, "usgs_gauge_daily") is None


def test_result_with_failed_run_writes_raw_but_no_rows(con):
    run = IngestRun(source="blue_spring_counts").fail(ValueError("layout changed"))
    result = IngestResult(run=run, raw="<html>", records=[_count(date(2026, 1, 5), 1)])
    with pytest.raises(RuntimeError, match="layout changed"):
        db.fetch_and_write(con, "blue_spring_counts", lambda: result,
                           lambda c, r: db.write_ingest_result(c, r, T.counts_raw, T.counts_daily))
    assert _count_rows(con, T.counts_daily) == 0
    assert _count_rows(con, T.counts_raw) == 1
    assert con.execute(f"SELECT status FROM {T.ingest_runs}").fetchall() == [("failed",)]


def _fake_parse_hourly(payload):
    hourly = payload["hourly"]
    return [{"obs_ts": datetime.fromisoformat(t), "temperature_2m": v, "unit": "°C"}
            for t, v in zip(hourly["time"], hourly["temperature_2m"])]


def test_archive_writes_daily_and_hourly_rows_idempotently(con, monkeypatch):
    monkeypatch.setattr(open_meteo, "parse_hourly", _fake_parse_hourly)
    raw = json.dumps({"hourly": {"time": ["2026-01-05T00:00", "2026-01-05T01:00"],
                                 "temperature_2m": [9.5, None]}})
    for _ in range(2):
        db.write_archive_result(con, _result("open_meteo_archive", [_weather(obs_date=date(2026, 1, 5))], raw))
    assert con.execute(f"SELECT obs_date, units FROM {T.weather_daily}").fetchall() == \
        [(date(2026, 1, 5), '{"temperature_2m_min": "°C"}')]
    assert con.execute(f"SELECT obs_ts, temperature_2m FROM {T.weather_hourly} ORDER BY 1").fetchall() == \
        [(datetime(2026, 1, 5, 0, 0), 9.5), (datetime(2026, 1, 5, 1, 0), None)]


def test_archive_hourly_parse_failure_fails_the_run(con, monkeypatch):
    def broken(payload):
        raise ValueError("hourly arrays differ in length")

    monkeypatch.setattr(open_meteo, "parse_hourly", broken)
    with pytest.raises(ValueError):
        db.write_archive_result(con, _result("open_meteo_archive", [_weather(obs_date=date(2026, 1, 5))], "{}"))
    assert _count_rows(con, T.weather_daily) == 0
    assert con.execute(f"SELECT status FROM {T.ingest_runs}").fetchall() == [("failed",)]
    assert con.execute(f"SELECT payload FROM {T.weather_raw}").fetchall() == [("{}",)]


def test_repeated_key_in_one_batch_keeps_the_last(con):
    ts = datetime(2025, 11, 2, 1, 0)  # the hour that occurs twice when daylight saving time ends
    rows = [{"obs_ts": ts, "temperature_2m": 20.0, "unit": "°C"},
            {"obs_ts": ts, "temperature_2m": 19.0, "unit": "°C"}]
    db.write_ingest_result(con, _result("open_meteo_archive", []), T.weather_raw, T.weather_daily,
                           extra={T.weather_hourly: rows})
    assert con.execute(f"SELECT temperature_2m FROM {T.weather_hourly}").fetchall() == [(19.0,)]


def test_null_rate_is_stored_on_the_run_and_skips_the_archive_lag(con):
    from datetime import date
    from roll_call import config
    from roll_call.ingest.base import IngestResult
    from roll_call.ingest.open_meteo import DAILY_FIELDS

    started = datetime(2026, 1, 20, 23, 0, tzinfo=timezone.utc)  # 18:00 in Florida
    full = {f: 1.0 for f in DAILY_FIELDS}
    half = {f: (1.0 if i % 2 else None) for i, f in enumerate(DAILY_FIELDS)}
    lagged = {f: None for f in DAILY_FIELDS}
    records = [
        {"obs_date": date(2026, 1, 10), **full, "units": "{}"},
        {"obs_date": date(2026, 1, 11), **half, "units": "{}"},
        {"obs_date": date(2026, 1, 18), **lagged, "units": "{}"},  # inside the lag: ignored
    ]
    run = IngestRun(source="open_meteo_archive", started_at=started).succeed(len(records))
    db.write_ingest_result(con, IngestResult(run=run, raw="{}", records=records),
                           config.TABLES.weather_raw, config.TABLES.weather_daily)
    stored = con.execute("SELECT null_rate FROM ingest_runs WHERE run_id = ?", [run.run_id]).fetchone()[0]
    assert stored == pytest.approx(3 / 12)


def test_existing_run_log_gains_the_null_rate_column(tmp_path):
    import duckdb
    path = tmp_path / "old.duckdb"
    old = duckdb.connect(str(path))
    old.execute("CREATE TABLE ingest_runs (run_id VARCHAR PRIMARY KEY, source VARCHAR NOT NULL, "
                "started_at TIMESTAMP NOT NULL, finished_at TIMESTAMP, status VARCHAR NOT NULL, "
                "row_count INTEGER, payload_sha256 VARCHAR, source_url VARCHAR, error VARCHAR)")
    old.close()
    c = db.connect(path)
    columns = [r[0] for r in c.execute("DESCRIBE ingest_runs").fetchall()]
    c.close()
    assert "null_rate" in columns
