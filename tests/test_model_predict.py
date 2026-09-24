"""The daily prediction and its scoring against persistence."""
import logging
from datetime import date, timedelta

import pytest

from roll_call.model import predict, score, store, train
from tests.test_model_support import (  # noqa: F401  fixtures
    add_count, con, hold_out, season, synthetic_history)

TODAY = date(2024, 2, 7)  # a Wednesday inside the third synthetic winter


def _trained(con):
    synthetic_history(con, forecast_from=date(2024, 1, 1))
    return train.train(con, date(2024, 1, 31), reason="first_model: test", season_open=True)


def test_no_prediction_outside_an_open_season(con, season):
    _trained(con)
    season.open = False
    assert predict.run(con, TODAY) is None
    assert con.execute("SELECT count(*) FROM predictions").fetchone() == (0,)


def test_no_prediction_before_the_first_model(con, season, caplog):
    synthetic_history(con, forecast_from=date(2024, 1, 1))
    with caplog.at_level(logging.INFO, logger="roll_call.model.predict"):
        assert predict.run(con, TODAY) is None
    assert "no model has been trained yet" in caplog.text


def test_prediction_for_tomorrow_is_stored(con, season):
    version = _trained(con)
    result = predict.run(con, TODAY)
    assert result["made_on"] == TODAY
    assert result["target_date"] == TODAY + timedelta(days=1)
    assert result["model_version"] == version.model_version
    assert result["predicted"] > 0
    stored = con.execute("SELECT * FROM predictions").fetchall()
    assert stored == [(TODAY, TODAY + timedelta(days=1), pytest.approx(result["predicted"]),
                       version.model_version)]
    (persistence,) = con.execute("SELECT last_count FROM prediction_inputs").fetchone()
    assert persistence == result["persistence"]

    predict.run(con, TODAY)  # a rerun replaces the day's prediction
    assert con.execute("SELECT count(*) FROM predictions").fetchone() == (1,)


def test_weekend_predictions_are_made_too(con, season):
    _trained(con)
    saturday = date(2024, 2, 10)
    result = predict.run(con, saturday)
    assert result["target_date"] == date(2024, 2, 11)


def _prediction(con, made_on, predicted, last_count, version="m1"):
    store.save_prediction(con, made_on=made_on, target_date=made_on + timedelta(days=1),
                          predicted=predicted, model_version=version, last_count=last_count,
                          last_count_date=made_on, features={})


def test_scoring_against_persistence(con, season):
    d = date(2026, 1, 5)
    add_count(con, d + timedelta(days=1), 100)
    add_count(con, d + timedelta(days=2), 80, is_estimate=True)
    add_count(con, d + timedelta(days=3), None, not_counted=True)
    add_count(con, d + timedelta(days=4), 70)
    hold_out(con, d + timedelta(days=4))
    _prediction(con, d, predicted=90.0, last_count=60)                      # scored
    _prediction(con, d + timedelta(days=1), predicted=95.0, last_count=100)  # scored, estimate
    _prediction(con, d + timedelta(days=2), predicted=50.0, last_count=80)   # not counted
    _prediction(con, d + timedelta(days=3), predicted=50.0, last_count=80)   # quarantined
    _prediction(con, d + timedelta(days=5), predicted=50.0, last_count=80)   # unreported

    scored = score.scored_predictions(con)
    assert list(scored["target_date"]) == [d + timedelta(days=2), d + timedelta(days=1)]
    assert list(scored["model_error"]) == [15.0, 10.0]
    assert list(scored["persistence_error"]) == [20.0, 40.0]

    assert score.summary(con) == {"scored": 2, "model_mae": 12.5, "persistence_mae": 30.0}
    assert score.summary(con, include_estimates=False) == {
        "scored": 1, "model_mae": 10.0, "persistence_mae": 40.0}
    assert score.summary(con, model_version="other")["scored"] == 0
