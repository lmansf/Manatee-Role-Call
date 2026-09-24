"""The training gate keeps open and rejected quarantined counts and weather values out of training."""
from datetime import date

import duckdb
import pytest

from roll_call.quality import baselines, clearing, gate
from tests.test_clearing import QUARANTINE_DDL, quarantine


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(QUARANTINE_DDL)
    baselines.ensure_tables(c)
    c.execute("CREATE TABLE blue_spring_counts_daily (report_date DATE PRIMARY KEY, "
              "count_researchers INTEGER)")
    for day in range(1, 6):
        c.execute("INSERT INTO blue_spring_counts_daily VALUES (?, ?)", [date(2026, 1, day), day * 100])
    quarantine(c, date(2026, 1, 2))                                   # open
    clearing.decide(c, quarantine(c, date(2026, 1, 3)), "rejected", "Typo.")
    clearing.decide(c, quarantine(c, date(2026, 1, 4)), "confirmed", "Park count agrees.")
    # A weather observation on the same date must not hold back the count.
    c.execute("INSERT INTO quarantine (obs_id, source, table_name, obs_key, observation_date, "
              "check_name) VALUES ('weather_daily:2026-01-05', 'open_meteo_archive', "
              "'weather_daily', '2026-01-05', DATE '2026-01-05', 'beyond_normal')")
    yield c
    c.close()


def trained(con, sql):
    return [r[0].day for r in con.execute(sql).fetchall()]


def test_gate_excludes_open_and_rejected(con):
    sql = f"SELECT report_date FROM blue_spring_counts_daily WHERE {gate.training_mask_sql()} ORDER BY 1"
    assert trained(con, sql) == [1, 4, 5]


def test_gate_with_an_alias(con):
    sql = (f"SELECT c.report_date FROM blue_spring_counts_daily c "
           f"WHERE c.count_researchers > 0 AND {gate.training_mask_sql('c')} ORDER BY 1")
    assert trained(con, sql) == [1, 4, 5]


def test_obs_id_format():
    assert gate.obs_id(date(2026, 1, 2)) == "blue_spring_counts_daily:2026-01-02"


def _quarantine_weather(con, day, measure):
    key = f"{day.isoformat()}:{measure}"
    con.execute("INSERT INTO quarantine (obs_id, source, table_name, obs_key, observation_date, "
                "check_name) VALUES (?, 'open_meteo_archive', 'weather_daily', ?, ?, "
                "'weather_normal')", [f"weather_daily:{key}", key, day])
    return f"weather_daily:{key}"


def test_held_out_weather_is_open_and_rejected_values(con):
    _quarantine_weather(con, date(2026, 1, 1), "temperature_2m_min")        # open
    clearing.decide(con, _quarantine_weather(con, date(2026, 1, 2), "temperature_2m_mean"),
                    "rejected", "Sensor fault.")
    clearing.decide(con, _quarantine_weather(con, date(2026, 1, 3), "temperature_2m_min"),
                    "confirmed", "A real cold front.")
    # The quarantined counts on Jan 2 to 4 and the measure-less weather row are not weather pairs.
    assert gate.held_out_weather(con) == {
        (date(2026, 1, 1), "temperature_2m_min"),
        (date(2026, 1, 2), "temperature_2m_mean"),
    }


def test_held_out_weather_without_a_quarantine_table():
    c = duckdb.connect(":memory:")
    assert gate.held_out_weather(c) == set()
    c.close()


def test_weather_obs_id_format():
    assert (gate.weather_obs_id(date(2026, 1, 2), "temperature_2m_min")
            == "weather_daily:2026-01-02:temperature_2m_min")
