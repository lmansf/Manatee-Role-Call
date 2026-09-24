"""Fitting the count regression on synthetic winters, and storing model versions."""
import json
from datetime import date

import numpy as np
import statsmodels.api as sm

from roll_call.model import store, train
from tests.test_model_support import con, season, synthetic_history  # noqa: F401  fixtures

TODAY = date(2024, 6, 1)


def test_negative_binomial_fits_and_predicts_more_manatees_in_the_cold(con, season):
    synthetic_history(con)
    frame = train.training_frame(con, TODAY)
    assert len(frame) >= train.MIN_TRAINING_ROWS
    params = train.fit(frame)
    assert params["family"] == "negative_binomial"
    assert params["alpha"] > 0
    assert params["fallback_reason"] is None
    assert params["features"] == list(frame.columns[4:])

    base = {"log_last_count": np.log1p(40), "days_since_last_count": 1.0,
            "gauge_change_3d_c": 0.0, "air_min_c": 10.0}
    warm = {**base, "gauge_temp_c": 22.0, "gauge_degree_hours_below": 0.0,
            "air_degree_days_below": 0.0}
    cold = {**base, "gauge_temp_c": 15.0, "gauge_degree_hours_below": 3 * 24 * 5.0,
            "air_degree_days_below": 16.0, "air_min_c": 2.0}
    assert train.expected_count(params, cold) > 2 * train.expected_count(params, warm) > 0


def test_stored_parameters_reproduce_the_fitted_model(con, season):
    synthetic_history(con)
    version = train.train(con, TODAY, reason="first_model: test", season_open=False)
    stored = store.current_model(con)
    assert stored.model_version == version.model_version == "m20240601-1"
    assert stored.params == json.loads(json.dumps(version.params))

    frame = train.training_frame(con, TODAY)
    x = sm.add_constant(frame[stored.params["features"]], has_constant="add")
    coef = np.array([stored.params["coefficients"][c] for c in x.columns])
    by_hand = np.exp(x.to_numpy() @ coef)
    from_store = [train.expected_count(stored.params, r) for r in frame.to_dict("records")]
    np.testing.assert_allclose(from_store, by_hand)


def test_model_version_records_its_training_set(con, season):
    synthetic_history(con)
    frame = train.training_frame(con, TODAY)
    v = train.train(con, TODAY, reason="first_model: test", season_open=False)
    assert v.training_rows == len(frame)
    assert v.estimate_rows == int(frame["is_estimate"].sum()) > 0
    assert v.first_target == frame["target_date"].min()
    assert v.last_target == frame["target_date"].max()
    assert v.reason == "first_model: test"
    assert v.season_open is False
    second = train.train(con, TODAY, reason="again")
    assert second.model_version == "m20240601-2"
    assert [m.model_version for m in store.model_versions(con)] == ["m20240601-1", "m20240601-2"]


def test_poisson_fallback_when_negative_binomial_does_not_converge(con, season, monkeypatch):
    synthetic_history(con)
    real_glm = train._glm

    def failing_nb(y, x, family):
        if isinstance(family, sm.families.NegativeBinomial):
            raise train.TrainingFailed("the fit did not converge")
        return real_glm(y, x, family)

    monkeypatch.setattr(train, "_glm", failing_nb)
    params = train.fit(train.training_frame(con, TODAY))
    assert params["family"] == "poisson"
    assert params["alpha"] is None
    assert "did not converge" in params["fallback_reason"]


def test_poisson_fallback_without_overdispersion(con, season, monkeypatch):
    synthetic_history(con)
    monkeypatch.setattr(train, "_dispersion", lambda y, mu: -0.5)
    params = train.fit(train.training_frame(con, TODAY))
    assert params["family"] == "poisson"
    assert "overdispersion" in params["fallback_reason"]


def test_too_few_rows_trains_nothing(con, season):
    synthetic_history(con, winters=1)
    assert train.train(con, date(2021, 11, 26), reason="first_model: test") is None
    assert store.current_model(con) is None
