"""Feature rows: weekend gaps, not counted days, the quarantine gate and both air sources."""
import math
from datetime import date, timedelta

import pytest

from roll_call.model import features, train
from roll_call.quality import baselines, clearing, gate
from roll_call.quality import store as quality_store
from tests.test_model_support import (  # noqa: F401  fixtures
    add_air, add_count, add_forecast, add_gauge, con, hold_out, season)

FRI, SAT, SUN, MON, TUE = (date(2026, 1, d) for d in (9, 10, 11, 12, 13))


def _week(con):
    """A week in January: counts Mon to Fri, nothing at the weekend, the gauge every day."""
    for d, n in zip(range(5, 10), (40, 45, 50, 55, 60)):
        add_count(con, date(2026, 1, d), n)
    add_count(con, TUE, 90)
    temps = {date(2026, 1, 1) + timedelta(days=k): 21.0 - 0.5 * k for k in range(14)}
    for d, t in temps.items():
        add_gauge(con, d, t)
        add_air(con, d, low=t - 8, mean=t - 3)
    return temps


def test_monday_after_an_unreported_weekend(con, season):
    temps = _week(con)
    row, reason = features.compute(features.load_inputs(con, air="archive"), SUN)
    assert reason is None
    assert row.target_date == MON
    assert row.last_count == 60 and row.last_count_date == FRI
    v = row.values
    assert v["log_last_count"] == pytest.approx(math.log1p(60))
    assert v["days_since_last_count"] == 3
    assert v["gauge_temp_c"] == temps[SUN]
    assert v["gauge_change_3d_c"] == pytest.approx(temps[SUN] - temps[date(2026, 1, 8)])
    expected_dh = sum(24 * max(0.0, 20 - temps[SUN - timedelta(days=k)]) for k in range(3))
    assert v["gauge_degree_hours_below"] == pytest.approx(expected_dh)
    assert v["air_min_c"] == temps[MON] - 8
    assert v["air_degree_days_below"] == pytest.approx(
        (20 - (temps[SUN] - 3)) + (20 - (temps[MON] - 3)))


def test_saturday_prediction_counts_two_days_from_friday(con, season):
    _week(con)
    row, _ = features.compute(features.load_inputs(con, air="archive"), SAT)
    assert row.target_date == SUN
    assert row.last_count_date == FRI
    assert row.values["days_since_last_count"] == 2


def test_not_counted_and_quarantined_days_are_not_the_last_count(con, season):
    _week(con)
    add_count(con, FRI, None, not_counted=True)
    hold_out(con, date(2026, 1, 8))
    row, _ = features.compute(features.load_inputs(con, air="archive"), SUN)
    assert row.last_count_date == date(2026, 1, 7)
    assert row.last_count == 50
    assert row.values["days_since_last_count"] == 5


def test_approved_gauge_value_wins_over_provisional(con, season):
    add_gauge(con, SUN, 30.0, status="Provisional")
    add_gauge(con, SUN, 18.0, status="Approved")
    add_gauge(con, MON, 17.0, status="Provisional")
    gauge = features.load_gauge(con)
    assert gauge[SUN] == 18.0 and gauge[MON] == 17.0


def test_unavailable_inputs_give_a_reason(con, season):
    _week(con)
    _, reason = features.compute(features.load_inputs(con, air="archive"), date(2026, 1, 4))
    assert "no count" in reason
    con.execute("DELETE FROM gauge_daily WHERE obs_date >= ?", [date(2026, 1, 7)])
    _, reason = features.compute(features.load_inputs(con, air="archive"), SUN)
    assert "gauge" in reason


def test_live_features_read_the_latest_forecast(con, season):
    _week(con)
    add_forecast(con, SAT, SUN, low=0.0, mean=5.0)
    add_forecast(con, SAT, MON, low=1.0, mean=6.0)
    add_forecast(con, SUN, SUN, low=2.0, mean=7.0)
    add_forecast(con, SUN, MON, low=3.0, mean=8.0)
    add_forecast(con, MON, MON, low=9.0, mean=9.0)  # issued after the prediction day
    row, _ = features.compute(features.load_inputs(con, air="forecast", made_on=SUN), SUN)
    assert row.values["air_min_c"] == 3.0
    assert row.values["air_degree_days_below"] == pytest.approx((20 - 7.0) + (20 - 8.0))


def test_stale_forecast_is_not_used(con, season):
    _week(con)
    add_forecast(con, date(2026, 1, 7), SUN, low=0.0, mean=5.0)
    add_forecast(con, date(2026, 1, 7), MON, low=1.0, mean=6.0)
    row, reason = features.compute(features.load_inputs(con, air="forecast", made_on=SUN), SUN)
    assert row is None and "air temperature" in reason


def test_training_rows_are_counted_days_without_quarantine(con, season):
    _week(con)
    add_count(con, date(2026, 1, 7), 5, is_estimate=True)
    add_count(con, date(2026, 1, 8), None, not_counted=True)
    hold_out(con, date(2026, 1, 6))
    frame = train.training_frame(con, today=date(2026, 1, 20))
    targets = set(frame["target_date"])
    # Jan 5 has no earlier count, Jan 6 is quarantined, Jan 8 was not counted.
    assert targets == {date(2026, 1, 7), FRI, TUE}
    assert frame.set_index("target_date").loc[date(2026, 1, 7), "is_estimate"]
    # The Tuesday row was predicted on Monday: last count Friday, four days before Tuesday.
    tue = frame.set_index("target_date").loc[TUE]
    assert tue["days_since_last_count"] == 4 and tue["count"] == 90
    # Training stops at today.
    assert set(train.training_frame(con, today=MON)["target_date"]) == {date(2026, 1, 7), FRI}


# The weather gate, with the real quarantine and clearing tables in place of the fake gate.

@pytest.fixture
def gated(con):
    quality_store.ensure_tables(con)
    baselines.ensure_tables(con)
    return con


def _hold_air(con, day, measure):
    quality_store.quarantine_observation(con, "open_meteo_archive", "weather_daily",
                                         f"{day.isoformat()}:{measure}", day, "weather_normal",
                                         -40.0)
    return gate.weather_obs_id(day, measure)


def _targets(con):
    return set(train.training_frame(con, today=date(2026, 1, 20))["target_date"])


ALL_TARGETS = {date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8), FRI, TUE}


def test_quarantined_air_values_leave_the_training_frame(gated):
    _week(gated)
    assert _targets(gated) == ALL_TARGETS
    _hold_air(gated, date(2026, 1, 8), "temperature_2m_min")  # the target date's low
    _hold_air(gated, MON, "temperature_2m_mean")  # the prediction day's mean for Tuesday
    _hold_air(gated, date(2026, 1, 6), "temperature_2m_max")  # a measure no feature reads
    assert _targets(gated) == ALL_TARGETS - {date(2026, 1, 8), TUE}
    _, reason = features.compute(features.load_inputs(gated, air="archive"), MON)
    assert reason == f"air temperature for {MON} is quarantined (temperature_2m_mean)"


def test_confirmed_air_value_trains_again_and_rejected_stays_out(gated):
    _week(gated)
    confirmed = _hold_air(gated, date(2026, 1, 8), "temperature_2m_min")
    rejected = _hold_air(gated, MON, "temperature_2m_mean")
    clearing.decide(gated, confirmed, "confirmed", "A real cold front.")
    clearing.decide(gated, rejected, "rejected", "Sensor fault.")
    assert _targets(gated) == ALL_TARGETS - {TUE}
    frame = train.training_frame(gated, today=date(2026, 1, 20)).set_index("target_date")
    assert frame.loc[date(2026, 1, 8), "air_min_c"] == pytest.approx(21.0 - 0.5 * 7 - 8)


def test_quarantined_archive_value_leaves_the_forecast_alone(gated):
    _week(gated)
    _hold_air(gated, MON, "temperature_2m_min")
    _hold_air(gated, SUN, "temperature_2m_mean")
    add_forecast(gated, SUN, SUN, low=2.0, mean=7.0)
    add_forecast(gated, SUN, MON, low=3.0, mean=8.0)
    row, reason = features.compute(features.load_inputs(gated, air="forecast", made_on=SUN), SUN)
    assert reason is None
    assert row.values["air_min_c"] == 3.0
