"""Weather baselines: day-of-year normals, refreshes, the refresh log and replays."""
import math
import sys
import types
from datetime import date
from types import SimpleNamespace

import duckdb
import pytest

import roll_call.quality
from roll_call.quality import baselines

MEASURES = baselines.WEATHER_MEASURES
SOURCE = baselines.WEATHER_SOURCE


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE weather_daily (obs_date DATE PRIMARY KEY, "
              + ", ".join(f"{m} DOUBLE" for m in MEASURES) + ", units VARCHAR)")
    yield c
    c.close()


def fill(con, start, end, value_sql, leap_days=False):
    """One row per day in [start, end), every measure set to value_sql of the date d."""
    leap = "" if leap_days else "WHERE NOT (month(d) = 2 AND day(d) = 29)"
    values = ", ".join(f"{value_sql} AS {m}" for m in MEASURES)
    con.execute(
        f"INSERT INTO weather_daily SELECT d::DATE AS obs_date, {values}, '{{}}' AS units "
        f"FROM (SELECT range AS d FROM range(DATE '{start}', DATE '{end}', INTERVAL 1 DAY)) {leap}"
    )


def put(con, day, value):
    con.execute("INSERT INTO weather_daily VALUES (?, " + ", ".join("?" for _ in MEASURES)
                + ", '{}')", [day, *([value] * len(MEASURES))])


def test_day_of_year_folds_leap_day():
    assert baselines.day_of_year(date(2003, 2, 28)) == 59
    assert baselines.day_of_year(date(2004, 2, 28)) == 59
    assert baselines.day_of_year(date(2004, 2, 29)) == 59
    assert baselines.day_of_year(date(2004, 3, 1)) == 60 == baselines.day_of_year(date(2003, 3, 1))
    assert baselines.day_of_year(date(2004, 12, 31)) == 365


def test_normal_for_each_day_of_year(con):
    # Every day of year y reads y - 2000, so each day of the year sees 0..9.
    fill(con, "2000-01-01", "2010-01-01", "year(d) - 2000")
    baselines.refresh(con, date(2010, 1, 1))
    sd = math.sqrt(sum((v - 4.5) ** 2 for v in range(10)) / 9)
    for day in (date(2011, 1, 1), date(2011, 7, 4), date(2011, 12, 31), date(2012, 2, 29)):
        mean, got_sd = baselines.normal(con, SOURCE, "temperature_2m_mean", day)
        assert mean == pytest.approx(4.5)
        assert got_sd == pytest.approx(sd)
    assert con.execute("SELECT count(DISTINCT day_of_year) FROM baselines").fetchone()[0] == 365


def test_normal_is_none_without_a_baseline(con):
    assert baselines.normal(con, SOURCE, "temperature_2m_mean", date(2011, 1, 1)) is None
    baselines.ensure_tables(con)
    assert baselines.normal(con, SOURCE, "temperature_2m_mean", date(2011, 1, 1)) is None
    assert baselines.normal(con, "other", "temperature_2m_mean", date(2011, 1, 1)) is None


def test_leap_day_counts_towards_day_59(con, monkeypatch):
    monkeypatch.setattr(baselines, "SMOOTHING_HALF_WIDTH", 0)
    put(con, date(2003, 2, 28), 1.0)
    put(con, date(2004, 2, 28), 2.0)
    put(con, date(2004, 2, 29), 6.0)
    put(con, date(2004, 3, 1), 50.0)
    put(con, date(2005, 3, 1), 70.0)
    rows = {doy: (m, sd) for doy, m, sd in baselines.compute(con, "temperature_2m_max", date(2006, 1, 1))}
    assert sorted(rows) == [59, 60]
    assert rows[59][0] == pytest.approx(3.0)
    assert rows[59][1] == pytest.approx(math.sqrt(((1 - 3) ** 2 + (2 - 3) ** 2 + (6 - 3) ** 2) / 2))
    assert rows[60][0] == pytest.approx(60.0)


def test_smoothing_spreads_one_day_over_fifteen(con):
    # Alternate 0 and 2 by year so every day has the same spread, then add 15 on 10 April
    # (day 100) only.
    fill(con, "2000-01-01", "2010-01-01", "(year(d) % 2) * 2 + CASE WHEN month(d) = 4 AND day(d) = 10 THEN 15 ELSE 0 END")
    rows = {doy: m for doy, m, _ in baselines.compute(con, "temperature_2m_mean", date(2010, 1, 1))}
    assert rows[100] == pytest.approx(1.0 + 1.0)
    assert rows[93] == pytest.approx(2.0) and rows[107] == pytest.approx(2.0)
    assert rows[92] == pytest.approx(1.0) and rows[108] == pytest.approx(1.0)


def test_smoothing_wraps_the_year_end(con):
    fill(con, "2000-01-01", "2010-01-01",
         "(year(d) % 2) * 2 + CASE WHEN month(d) = 12 AND day(d) = 31 THEN 15 ELSE 0 END")
    rows = {doy: m for doy, m, _ in baselines.compute(con, "temperature_2m_mean", date(2010, 1, 1))}
    assert rows[7] == pytest.approx(2.0)
    assert rows[8] == pytest.approx(1.0)


def test_window_is_the_trailing_thirty_years(con, monkeypatch):
    monkeypatch.setattr(baselines, "SMOOTHING_HALF_WIDTH", 0)
    put(con, date(1994, 1, 5), 1000.0)   # more than 30 years before
    put(con, date(1996, 1, 5), 1.0)
    put(con, date(2020, 1, 5), 3.0)
    put(con, date(2025, 1, 5), 500.0)    # the refresh date itself
    rows = baselines.compute(con, "precipitation_sum", date(2025, 1, 5))
    assert rows == [(5, pytest.approx(2.0), pytest.approx(math.sqrt(2.0)))]


def test_refresh_logs_old_and_new(con):
    fill(con, "2000-01-01", "2010-01-01", "year(d) - 2000")
    first = baselines.refresh(con, date(2005, 1, 1))
    assert {e["measure"] for e in first} == set(MEASURES)
    assert all(e["old_value"] is None and e["new_value"] == pytest.approx(2.0) for e in first)

    baselines.refresh(con, date(2010, 1, 1))
    log = con.execute(
        "SELECT refreshed_on, replayed, old_value, new_value FROM baseline_refreshes "
        "WHERE measure = 'temperature_2m_min' ORDER BY refreshed_on").fetchall()
    assert log == [
        (date(2005, 1, 1), False, None, pytest.approx(2.0)),
        (date(2010, 1, 1), False, pytest.approx(2.0), pytest.approx(4.5)),
    ]
    # The baselines table holds only the latest refresh.
    assert con.execute("SELECT DISTINCT refreshed_on FROM baselines").fetchall() == [(date(2010, 1, 1),)]
    assert con.execute("SELECT count(*) FROM baselines").fetchone()[0] == 365 * len(MEASURES)


def test_refresh_without_data_keeps_the_old_baseline(con):
    fill(con, "2000-01-01", "2005-01-01", "1.0")
    con.execute("UPDATE weather_daily SET precipitation_sum = NULL WHERE obs_date >= DATE '2003-01-01'")
    baselines.refresh(con, date(2005, 1, 1))
    con.execute("DELETE FROM weather_daily")
    assert baselines.refresh(con, date(2006, 1, 1)) == []
    assert baselines.normal(con, SOURCE, "precipitation_sum", date(2007, 6, 1)) == (1.0, 0.0)


@pytest.fixture
def season(monkeypatch):
    """A fake roll_call.quality.season whose state the test sets."""
    fake = types.ModuleType("roll_call.quality.season")
    fake.current = SimpleNamespace(is_open=True, closed_on=None)
    fake.state = lambda con, today: fake.current
    monkeypatch.setitem(sys.modules, "roll_call.quality.season", fake)
    monkeypatch.setattr(roll_call.quality, "season", fake, raising=False)
    return fake


def test_refresh_if_due_once_per_close(con, season):
    fill(con, "2000-01-01", "2010-01-01", "year(d) - 2000")
    assert baselines.refresh_if_due(con, date(2009, 3, 2)) is False  # season open

    season.current = SimpleNamespace(is_open=False, closed_on=date(2009, 3, 20))
    assert baselines.refresh_if_due(con, date(2009, 3, 21)) is True
    assert baselines.refresh_if_due(con, date(2009, 3, 21)) is False
    assert baselines.refresh_if_due(con, date(2009, 3, 22)) is False
    assert con.execute("SELECT DISTINCT refreshed_on FROM baseline_refreshes").fetchall() == [
        (date(2009, 3, 21),)]

    season.current = SimpleNamespace(is_open=False, closed_on=date(2010, 3, 25))
    assert baselines.refresh_if_due(con, date(2010, 3, 26)) is True
    assert baselines.refresh_if_due(con, date(2010, 3, 27)) is False


def test_refresh_if_due_ignores_replayed_refreshes(con, season):
    fill(con, "2000-01-01", "2010-01-01", "year(d) - 2000")
    baselines.replay(con, 2009, 2009)  # 1 April 2009, after the close below
    season.current = SimpleNamespace(is_open=False, closed_on=date(2009, 3, 20))
    assert baselines.refresh_if_due(con, date(2009, 4, 2)) is True


def test_refresh_if_due_needs_a_close(con, season):
    fill(con, "2000-01-01", "2010-01-01", "1.0")
    season.current = SimpleNamespace(is_open=False, closed_on=None)
    assert baselines.refresh_if_due(con, date(2009, 9, 1)) is False


def test_replay_uses_only_prior_data(con):
    fill(con, "2000-01-01", "2010-01-01", "year(d) - 2000 + dayofyear(d) / 100.0")
    assert baselines.replay(con, 2003, 2005) == [date(2003, 4, 1), date(2004, 4, 1), date(2005, 4, 1)]

    for year in (2003, 2004, 2005):
        cutoff = date(year, 4, 1)
        # Rebuild the same data without anything on or after the cutoff and compare.
        only = duckdb.connect(":memory:")
        only.execute("CREATE TABLE weather_daily (obs_date DATE PRIMARY KEY, "
                     + ", ".join(f"{m} DOUBLE" for m in MEASURES) + ", units VARCHAR)")
        fill(only, "2000-01-01", cutoff.isoformat(), "year(d) - 2000 + dayofyear(d) / 100.0")
        expected = baselines.season_average(baselines.compute(only, "temperature_2m_mean", cutoff))
        only.close()
        logged = con.execute(
            "SELECT replayed, new_value FROM baseline_refreshes "
            "WHERE refreshed_on = ? AND measure = 'temperature_2m_mean'", [cutoff]).fetchone()
        assert logged == (True, pytest.approx(expected))

    chain = con.execute(
        "SELECT old_value, new_value FROM baseline_refreshes "
        "WHERE measure = 'wind_speed_10m_max' ORDER BY refreshed_on").fetchall()
    assert chain[0][0] is None
    assert chain[1][0] == pytest.approx(chain[0][1])
    assert chain[2][0] == pytest.approx(chain[1][1])
    assert con.execute("SELECT DISTINCT refreshed_on FROM baselines").fetchall() == [(date(2005, 4, 1),)]


def test_replay_never_overwrites_a_later_baseline(con):
    fill(con, "2000-01-01", "2010-01-01", "year(d) - 2000")
    baselines.refresh(con, date(2010, 1, 1))
    baselines.replay(con, 2003, 2004)
    assert con.execute("SELECT DISTINCT refreshed_on FROM baselines").fetchall() == [(date(2010, 1, 1),)]
    assert baselines.normal(con, SOURCE, "temperature_2m_mean", date(2011, 1, 1))[0] == pytest.approx(4.5)
    assert con.execute("SELECT count(*) FROM baseline_refreshes WHERE replayed").fetchone()[0] == 2 * len(MEASURES)


def test_replay_skips_years_without_data(con):
    fill(con, "2000-01-01", "2002-01-01", "year(d) - 2000")
    assert baselines.replay(con, 1995, 2001) == [date(2001, 4, 1)]
