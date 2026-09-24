"""Every check against small in-memory DuckDB fixtures. The baseline normal is faked."""
import importlib
import json
import sys
import types
from datetime import date, datetime, timedelta

import duckdb
import pytest

from roll_call import config
from roll_call.quality import checks, store

TODAY = date(2027, 1, 13)  # a Wednesday in an open season

DDL = [
    """CREATE TABLE ingest_runs (run_id VARCHAR, source VARCHAR, started_at TIMESTAMP,
       finished_at TIMESTAMP, status VARCHAR, row_count INTEGER, payload_sha256 VARCHAR,
       source_url VARCHAR, error VARCHAR)""",
    """CREATE TABLE weather_daily (obs_date DATE, temperature_2m_min DOUBLE,
       temperature_2m_max DOUBLE, temperature_2m_mean DOUBLE, precipitation_sum DOUBLE,
       wind_speed_10m_max DOUBLE, shortwave_radiation_sum DOUBLE, units VARCHAR,
       run_id VARCHAR, ingested_at TIMESTAMP)""",
    """CREATE TABLE weather_hourly (obs_ts TIMESTAMP, temperature_2m DOUBLE, unit VARCHAR,
       run_id VARCHAR, ingested_at TIMESTAMP)""",
    """CREATE TABLE weather_forecast_daily (issue_date DATE, target_date DATE,
       temperature_2m_min DOUBLE, temperature_2m_max DOUBLE, temperature_2m_mean DOUBLE,
       precipitation_sum DOUBLE, wind_speed_10m_max DOUBLE, shortwave_radiation_sum DOUBLE,
       units VARCHAR, run_id VARCHAR, ingested_at TIMESTAMP)""",
    """CREATE TABLE blue_spring_counts_daily (report_date DATE, count_researchers INTEGER,
       count_park INTEGER, not_counted BOOLEAN, is_estimate BOOLEAN, river_temp_f DOUBLE,
       spring_temp_f DOUBLE, post_url VARCHAR, count_text VARCHAR, run_id VARCHAR,
       ingested_at TIMESTAMP)""",
    """CREATE TABLE gauge_daily (obs_date DATE, water_temp_c DOUBLE, unit VARCHAR,
       approval_status VARCHAR, run_id VARCHAR, ingested_at TIMESTAMP)""",
]

UNITS = json.dumps({"time": "iso8601", **checks.PINNED_DAILY_UNITS})


@pytest.fixture(autouse=True)
def baselines(monkeypatch):
    """Swap in a fake baselines.normal. The real module may not exist in this worktree."""
    name = "roll_call.quality.baselines"
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError:
        module = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(module, "normal", lambda con, source, measure, day: None, raising=False)
    return module


@pytest.fixture(autouse=True)
def pinned_config(monkeypatch):
    monkeypatch.setattr(config, "LATEST_PLAUSIBLE_START", (11, 15))
    monkeypatch.setattr(config, "SEASON_CLOSE_NOT_BEFORE", (3, 1))
    monkeypatch.setattr(config, "SEASON_CLOSE_SILENT_WEEKDAYS", 5)
    monkeypatch.setattr(config, "COUNT_PLAUSIBLE_RANGE", (0, 1500))


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    for ddl in DDL:
        c.execute(ddl)
    yield c
    c.close()


def add_run(con, source, day, rows=1, status="succeeded", run_id=None):
    run_id = run_id or f"{source}-{day}-{status}"
    started = datetime.combine(day, datetime.min.time()) + timedelta(hours=12)
    con.execute("INSERT INTO ingest_runs VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                [run_id, source, started, started, status, rows])
    return run_id


def add_weather(con, day, run_id="w", units=UNITS, **values):
    row = {f: 20.0 for f in checks.WEATHER_FIELDS}
    row.update(values)
    con.execute("INSERT INTO weather_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                [day, *[row[f] for f in checks.WEATHER_FIELDS], units, run_id])


def add_count(con, day, researchers=None, park=None, river_f=None, not_counted=False):
    con.execute(
        "INSERT INTO blue_spring_counts_daily (report_date, count_researchers, count_park, "
        "not_counted, river_temp_f) VALUES (?, ?, ?, ?, ?)",
        [day, researchers, park, not_counted, river_f])


def add_gauge(con, day, temp_c, status="Provisional", unit="degC", run_id="g"):
    con.execute("INSERT INTO gauge_daily VALUES (?, ?, ?, ?, ?, NULL)",
                [day, temp_c, unit, status, run_id])


def healthy(con, today=TODAY):
    """Fresh, well-formed runs for every source and an open season."""
    add_run(con, checks.ARCHIVE, today, rows=3, run_id="w")
    add_run(con, checks.FORECAST, today, rows=7, run_id="f")
    add_run(con, checks.COUNTS, today, rows=1, run_id="c")
    add_run(con, checks.GAUGE, today, rows=1, run_id="g")
    add_count(con, date(2026, 11, 2), researchers=10)


def incident_keys(items):
    return {(i["source"], i["check_name"]) for i in items}


def open_keys(con):
    return incident_keys(store.open_incidents(con))


# --- Freshness ---


def test_healthy_database_opens_nothing(con):
    healthy(con)
    report = checks.run_checks(con, TODAY)
    assert report.new_incidents == [] and report.new_quarantine == []
    assert report.season.status == "open"


def test_stale_source_opens_and_then_closes(con):
    healthy(con)
    con.execute("DELETE FROM ingest_runs WHERE source = ?", [checks.GAUGE])
    add_run(con, checks.GAUGE, TODAY - timedelta(days=5))
    add_run(con, checks.GAUGE, TODAY, status="failed")  # a failure is not a success
    report = checks.run_checks(con, TODAY)
    assert incident_keys(report.new_incidents) == {(checks.GAUGE, "freshness")}

    add_run(con, checks.GAUGE, TODAY, run_id="g-fresh")
    report = checks.run_checks(con, TODAY)
    assert incident_keys(report.closed_incidents) == {(checks.GAUGE, "freshness")}
    assert open_keys(con) == set()


def test_never_succeeded_is_stale(con):
    healthy(con)
    con.execute("DELETE FROM ingest_runs WHERE source = ?", [checks.FORECAST])
    report = checks.run_checks(con, TODAY)
    assert (checks.FORECAST, "freshness") in incident_keys(report.new_incidents)


def test_counts_freshness_counts_weekdays_only(con):
    healthy(con)
    con.execute("DELETE FROM ingest_runs WHERE source = ?", [checks.COUNTS])
    add_run(con, checks.COUNTS, date(2027, 1, 8))  # Friday
    # Monday 11 January: one weekday since Friday, not stale.
    checks.run_checks(con, date(2027, 1, 11))
    assert (checks.COUNTS, "freshness") not in open_keys(con)
    # Wednesday 13 January: three weekdays since Friday, stale.
    checks.run_checks(con, date(2027, 1, 13))
    assert (checks.COUNTS, "freshness") in open_keys(con)


def test_counts_freshness_is_skipped_out_of_season(con):
    summer = date(2027, 6, 16)
    for s in (checks.ARCHIVE, checks.FORECAST, checks.GAUGE):
        add_run(con, s, summer, rows=7)
    add_run(con, checks.COUNTS, date(2027, 3, 1))
    add_count(con, date(2026, 11, 2), researchers=10)
    report = checks.run_checks(con, summer)
    assert report.season.status == "closed"
    assert (checks.COUNTS, "freshness") not in open_keys(con)


def test_overdue_season_is_an_incident_that_closes_on_first_report(con):
    day = date(2026, 11, 20)
    healthy(con, day)
    con.execute("DELETE FROM blue_spring_counts_daily")
    report = checks.run_checks(con, day)
    assert report.season.status == "overdue"
    assert (checks.COUNTS, "season_overdue") in incident_keys(report.new_incidents)
    # Overdue keeps the counts freshness check on.
    con.execute("DELETE FROM ingest_runs WHERE source = ?", [checks.COUNTS])
    checks.run_checks(con, day)
    assert (checks.COUNTS, "freshness") in open_keys(con)

    add_count(con, day, researchers=12)
    add_run(con, checks.COUNTS, day, run_id="c-new")
    report = checks.run_checks(con, day)
    assert {(checks.COUNTS, "season_overdue"), (checks.COUNTS, "freshness")} <= incident_keys(
        report.closed_incidents)


# --- Volume ---


def test_forecast_volume_against_expectation(con):
    healthy(con)
    con.execute("UPDATE ingest_runs SET row_count = 3 WHERE source = ?", [checks.FORECAST])
    report = checks.run_checks(con, TODAY)
    assert incident_keys(report.new_incidents) == {(checks.FORECAST, "volume")}
    con.execute("UPDATE ingest_runs SET row_count = 7 WHERE source = ?", [checks.FORECAST])
    report = checks.run_checks(con, TODAY)
    assert incident_keys(report.closed_incidents) == {(checks.FORECAST, "volume")}


def test_duplicates_open_an_incident(con):
    healthy(con)
    day = TODAY - timedelta(days=10)
    add_weather(con, day)
    add_weather(con, day)
    report = checks.run_checks(con, TODAY)
    assert (checks.ARCHIVE, "duplicates") in incident_keys(report.new_incidents)
    con.execute("DELETE FROM weather_daily")
    add_weather(con, day)
    report = checks.run_checks(con, TODAY)
    assert (checks.ARCHIVE, "duplicates") in incident_keys(report.closed_incidents)


# --- Distribution ---


def test_null_rate_per_column_ignores_the_archive_lag(con):
    healthy(con)
    for i in range(checks.ARCHIVE_LAG_DAYS + 1, checks.ARCHIVE_LAG_DAYS + 11):
        add_weather(con, TODAY - timedelta(days=i), precipitation_sum=None if i % 2 else 1.0)
    for i in range(checks.ARCHIVE_LAG_DAYS):  # inside the lag: null by design
        add_weather(con, TODAY - timedelta(days=i), temperature_2m_max=None)
    report = checks.run_checks(con, TODAY)
    assert incident_keys(report.new_incidents) == {(checks.ARCHIVE, "null_rate:precipitation_sum")}


def test_forecast_null_rate_uses_the_latest_issue(con):
    healthy(con)
    for issue, wind in ((TODAY - timedelta(days=1), None), (TODAY, 10.0)):
        for k in range(7):
            con.execute(
                "INSERT INTO weather_forecast_daily VALUES (?, ?, 1, 2, 3, 4, ?, 6, ?, 'f', NULL)",
                [issue, issue + timedelta(days=k), wind, UNITS])
    assert checks.run_checks(con, TODAY).new_incidents == []


def test_units_against_pinned_units(con):
    healthy(con)
    fahrenheit = json.dumps({**checks.PINNED_DAILY_UNITS, "temperature_2m_max": "°F"})
    add_weather(con, TODAY - timedelta(days=8), run_id="w", units=fahrenheit)
    add_weather(con, TODAY - timedelta(days=30), run_id="old", units="{}")  # older run ignored
    add_gauge(con, TODAY, 18.0, unit="degF", run_id="g")
    report = checks.run_checks(con, TODAY)
    assert incident_keys(report.new_incidents) == {(checks.ARCHIVE, "units"), (checks.GAUGE, "units")}
    detail = {i["source"]: i["detail"] for i in report.new_incidents}
    assert "temperature_2m_max" in detail[checks.ARCHIVE] and "°F" in detail[checks.ARCHIVE]


def test_weather_beyond_three_sd_is_quarantined(con, baselines, monkeypatch):
    healthy(con)
    calls = []

    def normal(c, source, measure, day):
        calls.append((source, measure))
        if measure == "shortwave_radiation_sum":
            return None  # no baseline: skipped
        return (20.0, 2.0)

    monkeypatch.setattr(baselines, "normal", normal)
    day = TODAY - timedelta(days=7)
    add_weather(con, day, temperature_2m_max=26.5, temperature_2m_min=26.0,
                shortwave_radiation_sum=500.0, precipitation_sum=None)
    report = checks.run_checks(con, TODAY)
    ids = {q["obs_id"] for q in report.new_quarantine}
    assert ids == {f"weather_daily:{day}:temperature_2m_max"}
    assert report.new_quarantine[0]["value"] == "26.5"
    assert report.new_quarantine[0]["source"] == checks.ARCHIVE
    assert (checks.ARCHIVE, "precipitation_sum") not in calls  # null values are not judged
    # Idempotent on the next run.
    assert checks.run_checks(con, TODAY).new_quarantine == []


# --- Counts ---


def test_count_outside_plausible_range_is_quarantined(con):
    healthy(con)
    add_count(con, TODAY - timedelta(days=2), researchers=2000)
    add_count(con, TODAY - timedelta(days=1), researchers=40, park=-1)
    for i in (1, 2):  # joined to the gauge, so join retention stays quiet
        add_gauge(con, TODAY - timedelta(days=i), 18.0)
    report = checks.run_checks(con, TODAY)
    values = {q["obs_id"]: q["value"] for q in report.new_quarantine}
    assert values == {
        f"blue_spring_counts_daily:{TODAY - timedelta(days=2)}": "count_researchers=2000",
        f"blue_spring_counts_daily:{TODAY - timedelta(days=1)}": "count_park=-1",
    }
    assert all(q["check_name"] == "count_range" for q in report.new_quarantine)
    assert report.new_incidents == []


def test_rise_while_the_gauge_warms_is_quarantined(con):
    healthy(con)
    d0, d1 = date(2027, 1, 11), date(2027, 1, 12)
    add_count(con, d0, researchers=100)
    add_count(con, d1, researchers=300)
    add_gauge(con, d0, 18.0)
    add_gauge(con, d1, 19.0)
    report = checks.run_checks(con, TODAY)
    assert [(q["obs_id"], q["check_name"]) for q in report.new_quarantine] == [
        (f"blue_spring_counts_daily:{d1}", "count_jump")]


def test_rise_while_the_river_cools_is_kept(con):
    healthy(con)
    d0, d1 = date(2027, 1, 11), date(2027, 1, 12)
    add_count(con, d0, researchers=100, river_f=66.0)
    add_count(con, d1, researchers=300, river_f=62.0)
    add_gauge(con, d0, 19.0)
    add_gauge(con, d1, 17.0)
    assert checks.run_checks(con, TODAY).new_quarantine == []


def test_fall_while_the_report_temperature_cools_is_quarantined(con):
    healthy(con)
    d0, d1 = date(2027, 1, 8), date(2027, 1, 11)  # Friday to Monday
    add_count(con, d0, researchers=300, river_f=66.0)
    add_count(con, d1, researchers=100, river_f=64.0)
    report = checks.run_checks(con, TODAY)
    assert [q["obs_id"] for q in report.new_quarantine] == [f"blue_spring_counts_daily:{d1}"]


def test_jump_without_any_temperature_is_kept(con):
    healthy(con)
    add_count(con, date(2027, 1, 11), researchers=100)
    add_count(con, date(2027, 1, 12), researchers=300)
    assert checks.run_checks(con, TODAY).new_quarantine == []


def test_counts_too_far_apart_are_not_a_jump(con):
    healthy(con)
    d0, d1 = date(2027, 1, 4), date(2027, 1, 12)
    add_count(con, d0, researchers=100)
    add_count(con, d1, researchers=300)
    add_gauge(con, d0, 18.0)
    add_gauge(con, d1, 19.0)
    assert checks.run_checks(con, TODAY).new_quarantine == []


def test_counter_disagreement_is_a_measure_and_never_quarantines(con):
    healthy(con)
    day = TODAY - timedelta(days=1)
    add_count(con, day, researchers=100, park=900)
    add_count(con, TODAY - timedelta(days=2), not_counted=True)
    add_gauge(con, day, 18.0)
    report = checks.run_checks(con, TODAY)
    assert report.new_quarantine == [] and report.new_incidents == []
    assert report.measures["counter_disagreement"] == [
        {"report_date": day, "count_researchers": 100, "count_park": 900,
         "counter_disagreement": -800}]
    assert con.execute("SELECT count(*) FROM quarantine").fetchone()[0] == 0
    assert store.open_incidents(con) == []


def stored(con, measure):
    return con.execute(
        "SELECT obs_key, observation_date, value FROM measures WHERE measure = ? ORDER BY obs_key",
        [measure]).fetchall()


def test_counter_disagreement_is_stored_and_overwritten_on_rerun(con):
    healthy(con)
    d0, d1 = TODAY - timedelta(days=2), TODAY - timedelta(days=1)
    add_count(con, d0, researchers=100, park=90)
    add_count(con, d1, researchers=50)  # no park count: no disagreement
    checks.run_checks(con, TODAY)
    assert stored(con, "counter_disagreement") == [(d0.isoformat(), d0, 10.0)]
    first_at = con.execute("SELECT computed_at FROM measures").fetchone()[0]
    assert first_at is not None

    # The park count is corrected and the park reports the next day: the re-run overwrites
    # its own row and the history grows by one.
    con.execute("UPDATE blue_spring_counts_daily SET count_park = 70 WHERE report_date = ?", [d0])
    con.execute("UPDATE blue_spring_counts_daily SET count_park = 80 WHERE report_date = ?", [d1])
    checks.run_checks(con, TODAY)
    assert stored(con, "counter_disagreement") == [(d0.isoformat(), d0, 30.0),
                                                   (d1.isoformat(), d1, -30.0)]


# --- Join retention ---

RETENTION = ((checks.COUNTS, "join_retention:gauge"), (checks.COUNTS, "join_retention:weather"))


def _joined_days(con, days, gauge=True, weather=True):
    for d in days:
        add_count(con, d, researchers=100)
        if gauge:
            add_gauge(con, d, 18.0)
        if weather:
            add_weather(con, d)


def test_join_retention_passes_when_every_counted_day_joins(con):
    healthy(con)
    days = [TODAY - timedelta(days=i) for i in range(1, 16)]
    _joined_days(con, days)
    report = checks.run_checks(con, TODAY)
    assert not set(RETENTION) & incident_keys(report.new_incidents)
    assert stored(con, "join_retention:gauge") == [(TODAY.isoformat(), TODAY, 1.0)]
    assert stored(con, "join_retention:weather") == [(TODAY.isoformat(), TODAY, 1.0)]


def test_join_retention_opens_then_closes_for_the_gauge_and_the_weather(con):
    healthy(con)
    days = [TODAY - timedelta(days=i) for i in range(checks.ARCHIVE_LAG_DAYS + 1,
                                                        checks.ARCHIVE_LAG_DAYS + 11)]
    _joined_days(con, days[:8])
    _joined_days(con, days[8:], gauge=False, weather=False)  # 8 of 10 join: 80%
    report = checks.run_checks(con, TODAY)
    assert set(RETENTION) <= incident_keys(report.new_incidents)
    detail = {i["check_name"]: i["detail"] for i in report.new_incidents}
    assert "8 of 10" in detail["join_retention:gauge"]
    assert stored(con, "join_retention:gauge") == [(TODAY.isoformat(), TODAY, 0.8)]
    assert stored(con, "join_retention:weather") == [(TODAY.isoformat(), TODAY, 0.8)]

    for d in days[8:]:
        add_gauge(con, d, 18.0)
        add_weather(con, d)
    report = checks.run_checks(con, TODAY)
    assert set(RETENTION) <= incident_keys(report.closed_incidents)
    assert stored(con, "join_retention:gauge") == [(TODAY.isoformat(), TODAY, 1.0)]


def test_join_retention_skips_the_archive_lag(con):
    healthy(con)
    old = [TODAY - timedelta(days=i) for i in range(checks.ARCHIVE_LAG_DAYS + 1,
                                                       checks.ARCHIVE_LAG_DAYS + 4)]
    _joined_days(con, old)
    # Inside the lag: gauge present, weather rows null by design or not there yet.
    recent = [TODAY - timedelta(days=i) for i in range(1, checks.ARCHIVE_LAG_DAYS + 1)]
    _joined_days(con, recent, weather=False)
    for d in recent[:2]:
        add_weather(con, d, **{f: None for f in checks.WEATHER_FIELDS})
    # Today's gauge daily mean does not exist yet.
    add_count(con, TODAY, researchers=100)
    report = checks.run_checks(con, TODAY)
    assert not set(RETENTION) & incident_keys(report.new_incidents)
    assert stored(con, "join_retention:weather") == [(TODAY.isoformat(), TODAY, 1.0)]
    assert stored(con, "join_retention:gauge") == [(TODAY.isoformat(), TODAY, 1.0)]


def test_join_retention_ignores_days_that_were_not_counted(con):
    healthy(con)
    days = [TODAY - timedelta(days=i) for i in range(checks.ARCHIVE_LAG_DAYS + 1,
                                                        checks.ARCHIVE_LAG_DAYS + 4)]
    _joined_days(con, days)
    for i in range(checks.ARCHIVE_LAG_DAYS + 4, checks.ARCHIVE_LAG_DAYS + 10):
        add_count(con, TODAY - timedelta(days=i), not_counted=True)  # nothing joins
    report = checks.run_checks(con, TODAY)
    assert not set(RETENTION) & incident_keys(report.new_incidents)
    assert stored(con, "join_retention:gauge") == [(TODAY.isoformat(), TODAY, 1.0)]


def test_join_retention_with_nothing_counted_stores_nothing(con):
    healthy(con)  # its only count is outside the lookback window
    checks.run_checks(con, TODAY)
    assert stored(con, "join_retention:gauge") == []
    assert stored(con, "join_retention:weather") == []


# --- Cross-source ---


def _cross_source_days(con, n, shift_last):
    days = [date(2026, 12, 1) + timedelta(days=i) for i in range(n)]
    for i, d in enumerate(days):
        gauge_c = 18.0
        report_c = gauge_c + 0.5 + (shift_last if i == n - 1 else 0.0)
        add_count(con, d, researchers=50, river_f=report_c * 9 / 5 + 32)
        add_gauge(con, d, gauge_c)
    return days


def test_report_versus_gauge_shift_opens_then_closes(con):
    healthy(con)
    days = _cross_source_days(con, 10, shift_last=2.5)
    report = checks.run_checks(con, TODAY)
    assert (checks.GAUGE, "report_vs_gauge_temperature") in incident_keys(report.new_incidents)
    assert str(days[-1]) in report.new_incidents[0]["detail"]

    nxt = days[-1] + timedelta(days=1)
    add_count(con, nxt, researchers=50, river_f=18.6 * 9 / 5 + 32)
    add_gauge(con, nxt, 18.0)
    report = checks.run_checks(con, TODAY)
    assert (checks.GAUGE, "report_vs_gauge_temperature") in incident_keys(report.closed_incidents)


def test_report_versus_gauge_small_shift_passes(con):
    healthy(con)
    _cross_source_days(con, 10, shift_last=1.5)
    assert checks.run_checks(con, TODAY).new_incidents == []


def test_approved_gauge_value_wins_over_provisional(con):
    healthy(con)
    days = _cross_source_days(con, 10, shift_last=2.5)
    # The approved value for the last day matches the report: no shift.
    add_gauge(con, days[-1], 20.5, status="Approved")
    assert checks.run_checks(con, TODAY).new_incidents == []


# --- Schema drift and idempotency ---


def test_renamed_column_is_a_schema_incident(con):
    healthy(con)
    con.execute("ALTER TABLE weather_daily RENAME COLUMN precipitation_sum TO precip")
    add_weather_sql = ("INSERT INTO weather_daily (obs_date, units, run_id) VALUES (?, ?, 'w')")
    con.execute(add_weather_sql, [TODAY - timedelta(days=8), UNITS])
    report = checks.run_checks(con, TODAY)
    assert (checks.ARCHIVE, "schema") in incident_keys(report.new_incidents)
    # Other checks still ran.
    add_count(con, TODAY - timedelta(days=1), researchers=5000)
    report = checks.run_checks(con, TODAY)
    assert len(report.new_quarantine) == 1


def test_second_run_reports_nothing_new(con):
    healthy(con)
    con.execute("UPDATE ingest_runs SET row_count = 0 WHERE source = ?", [checks.GAUGE])
    add_count(con, TODAY - timedelta(days=1), researchers=5000)
    first = checks.run_checks(con, TODAY)
    assert first.new_incidents and first.new_quarantine
    second = checks.run_checks(con, TODAY)
    assert second.new_incidents == [] and second.new_quarantine == []
    assert second.closed_incidents == []
