"""The replay job logs one replayed refresh per year from the weather history."""
import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

from roll_call import config
from roll_call.storage import db

_spec = importlib.util.spec_from_file_location(
    "replay_baselines", Path(__file__).resolve().parents[1] / "jobs" / "replay_baselines.py")
replay_baselines = importlib.util.module_from_spec(_spec)
sys.modules["replay_baselines"] = replay_baselines
_spec.loader.exec_module(replay_baselines)


def _weather_history(con, first: date, last: date) -> None:
    rows, day = [], first
    while day <= last:
        rows.append((day, 15.0, 25.0, 20.0 + (day.year - 2000) * 0.1, 1.0, 10.0, 12.0, "{}", "r", day))
        day += timedelta(days=1)
    con.executemany(
        f"INSERT INTO {config.TABLES.weather_daily} (obs_date, temperature_2m_min, temperature_2m_max, "
        "temperature_2m_mean, precipitation_sum, wind_speed_10m_max, shortwave_radiation_sum, units, "
        "run_id, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def test_replays_each_year_and_is_safe_to_rerun(tmp_path):
    path = tmp_path / "r.duckdb"
    con = db.connect(path)
    _weather_history(con, date(2015, 1, 1), date(2021, 12, 31))
    con.close()

    assert replay_baselines.main(["2019", "2021"], db_path=str(path)) == 0
    assert replay_baselines.main(["2019", "2021"], db_path=str(path)) == 0

    con = db.connect(path)
    rows = con.execute("SELECT DISTINCT refreshed_on, replayed FROM baseline_refreshes ORDER BY 1").fetchall()
    con.close()
    assert rows == [(date(2019, 4, 1), True), (date(2020, 4, 1), True), (date(2021, 4, 1), True)]


def test_last_closed_year():
    assert replay_baselines.last_closed_year(date(2026, 9, 24)) == 2026
    assert replay_baselines.last_closed_year(date(2026, 2, 1)) == 2025
