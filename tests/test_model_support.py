"""Shared helpers for the model tests: the ingested tables, fakes for the quality layer, and a
synthetic history in which cold snaps drive the counts up once the gauge drops below 20 degrees C.

The tables follow the swarm contract's shapes. The storage module owns the real DDL; these
copies keep the model tests independent of it.
"""
from __future__ import annotations

import math
import sys
import types
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd
import pytest

from roll_call import config

T = config.TABLES
WEATHER_FIELDS = ("temperature_2m_min DOUBLE, temperature_2m_max DOUBLE, "
                  "temperature_2m_mean DOUBLE, precipitation_sum DOUBLE, "
                  "wind_speed_10m_max DOUBLE, shortwave_radiation_sum DOUBLE, units VARCHAR")
STAMP = "run_id VARCHAR, ingested_at TIMESTAMP"


def make_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""CREATE TABLE {T.counts_daily} (report_date DATE PRIMARY KEY,
        count_researchers INTEGER, count_park INTEGER, not_counted BOOLEAN, is_estimate BOOLEAN,
        river_temp_f DOUBLE, spring_temp_f DOUBLE, post_url VARCHAR, count_text VARCHAR,
        {STAMP})""")
    con.execute(f"""CREATE TABLE {T.gauge_daily} (obs_date DATE, water_temp_c DOUBLE,
        unit VARCHAR, approval_status VARCHAR, {STAMP},
        PRIMARY KEY (obs_date, approval_status))""")
    con.execute(f"CREATE TABLE {T.weather_daily} (obs_date DATE PRIMARY KEY, {WEATHER_FIELDS}, "
                f"{STAMP})")
    con.execute(f"""CREATE TABLE {T.weather_forecast} (issue_date DATE, target_date DATE,
        {WEATHER_FIELDS}, {STAMP}, PRIMARY KEY (issue_date, target_date))""")
    # Stands in for the quarantine table and its clearing decisions.
    con.execute("CREATE TABLE test_held_out (observation_date DATE)")


def add_count(con, day: date, n: int | None, not_counted: bool = False,
              is_estimate: bool = False) -> None:
    con.execute(f"""INSERT OR REPLACE INTO {T.counts_daily}
        (report_date, count_researchers, not_counted, is_estimate) VALUES (?, ?, ?, ?)""",
                [day, n, not_counted, is_estimate])


def add_gauge(con, day: date, temp_c: float, status: str = "Approved") -> None:
    con.execute(f"INSERT INTO {T.gauge_daily} (obs_date, water_temp_c, unit, approval_status) "
                "VALUES (?, ?, 'degC', ?)", [day, temp_c, status])


def add_air(con, day: date, low: float, mean: float) -> None:
    con.execute(f"INSERT INTO {T.weather_daily} (obs_date, temperature_2m_min, "
                "temperature_2m_mean) VALUES (?, ?, ?)", [day, low, mean])


def add_forecast(con, issue: date, target: date, low: float, mean: float) -> None:
    con.execute(f"""INSERT INTO {T.weather_forecast}
        (issue_date, target_date, temperature_2m_min, temperature_2m_mean) VALUES (?, ?, ?, ?)""",
                [issue, target, low, mean])


def hold_out(con, day: date) -> None:
    """Quarantine the count on this day (open or rejected, as far as the fake gate knows)."""
    con.execute("INSERT INTO test_held_out VALUES (?)", [day])


class FakeSeason:
    def __init__(self, open_: bool = True):
        self.open = open_

    def is_open(self, con, today):
        return self.open


def install_fake_quality(monkeypatch, season: FakeSeason) -> None:
    """Replace roll_call.quality.gate and roll_call.quality.season, which other agents write."""
    gate = types.ModuleType("roll_call.quality.gate")
    gate.training_mask_sql = (
        lambda: "report_date NOT IN (SELECT observation_date FROM test_held_out)")
    gate.held_out_weather = lambda con: set()
    season_mod = types.ModuleType("roll_call.quality.season")
    season_mod.is_open = season.is_open
    monkeypatch.setitem(sys.modules, "roll_call.quality.gate", gate)
    monkeypatch.setitem(sys.modules, "roll_call.quality.season", season_mod)


@pytest.fixture
def season(monkeypatch):
    s = FakeSeason(open_=True)
    install_fake_quality(monkeypatch, s)
    return s


@pytest.fixture
def con(tmp_path):
    c = duckdb.connect(str(tmp_path / "model.duckdb"))
    make_tables(c)
    yield c
    c.close()


def _bulk(con, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
    frame = pd.DataFrame(rows, columns=list(columns))  # noqa: F841  read by DuckDB below
    con.execute(f"INSERT INTO {table} ({', '.join(columns)}) SELECT * FROM frame")


def synthetic_history(con, first_winter: int = 2021, winters: int = 3, seed: int = 7,
                      forecast_from: date | None = None) -> dict[date, float]:
    """Write synthetic winters of gauge, air weather and weekday counts. Returns the gauge series.

    Air temperature follows a winter curve with cold fronts. The river follows the air with a lag.
    The expected count rises by exp(0.45) per degree the gauge sits below 20 degrees C.
    Weekends are unreported, about 3% of weekdays are not counted and 5% are estimates.
    Forecasts are the archive plus noise that grows with lead time."""
    rng = np.random.default_rng(seed)
    gauge: dict[date, float] = {}
    counts, air = [], []
    for year in range(first_winter, first_winter + winters):
        start, end = date(year, 11, 1), date(year + 1, 3, 31)
        days = (end - start).days + 1
        g = 22.0
        front = 0.0
        for k in range(days):
            day = start + timedelta(days=k)
            phase = k / days
            if rng.random() < 0.08:
                front = rng.uniform(6, 11)
            front *= 0.7
            mean = 21.0 - 7.0 * math.sin(math.pi * phase) - front + rng.normal(0, 1.0)
            low = mean - 5.0 + rng.normal(0, 1.0)
            g += 0.35 * (mean + 1.5 - g) + rng.normal(0, 0.2)
            gauge[day] = round(g, 2)
            air.append((day, round(low, 2), round(mean, 2)))
            if day.weekday() >= 5 or not (date(year, 11, 20) <= day <= date(year + 1, 3, 10)):
                continue
            if rng.random() < 0.03:
                counts.append((day, None, True, False))
                continue
            lam = 12.0 * math.exp(0.45 * max(0.0, 20.0 - g))
            n = int(rng.poisson(rng.gamma(1 / 0.08, lam * 0.08)))
            counts.append((day, n, False, bool(rng.random() < 0.05)))
    gauge_rows = [(d, t, "degC", "Approved") for d, t in gauge.items()]
    _bulk(con, T.gauge_daily, ("obs_date", "water_temp_c", "unit", "approval_status"), gauge_rows)
    _bulk(con, T.weather_daily, ("obs_date", "temperature_2m_min", "temperature_2m_mean"), air)
    _bulk(con, T.counts_daily, ("report_date", "count_researchers", "not_counted", "is_estimate"),
          counts)
    if forecast_from is not None:
        by_day = {d: (lo, m) for d, lo, m in air}
        rows = []
        for issue in sorted(d for d in by_day if d >= forecast_from):
            for lead in range(7):
                target = issue + timedelta(days=lead)
                if target in by_day:
                    lo, m = by_day[target]
                    noise = rng.normal(0, 0.5 * (lead + 1))
                    rows.append((issue, target, lo + noise, m + noise))
        _bulk(con, T.weather_forecast,
              ("issue_date", "target_date", "temperature_2m_min", "temperature_2m_mean"), rows)
    return gauge


def test_synthetic_counts_rise_when_the_gauge_is_cold(con):
    gauge = synthetic_history(con, winters=2)
    rows = con.execute(f"SELECT report_date, count_researchers FROM {T.counts_daily} "
                       "WHERE count_researchers IS NOT NULL").fetchall()
    cold = [n for d, n in rows if gauge[d] < 17]
    warm = [n for d, n in rows if gauge[d] >= 20]
    assert cold and warm
    assert np.mean(cold) > 3 * np.mean(warm)
