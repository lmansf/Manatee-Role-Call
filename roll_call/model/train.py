"""Training the count regression, and deciding when to retrain (spec section 4, Retraining).

The model is a negative binomial GLM with a log link over the hand-picked features in
`features.FEATURES`. No feature selection runs here, so the performance history stays readable.
The negative binomial dispersion comes from the Poisson fit by the Cameron and Trivedi auxiliary
regression. When the negative binomial fit fails or does not converge, the Poisson fit is kept.

A retrain happens for one of three reasons, recorded with the model version:
- first_model: no model exists and at least MIN_TRAINING_ROWS training rows do.
- lost_to_persistence: the current model's last PERFORMANCE_WINDOW scored predictions had a higher
  mean absolute error than persistence.
- season_closed: the season is closed and no model has been trained since its last count while
  the season was closed. This fires once per season.
"""
from __future__ import annotations

import logging
import math
import warnings
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.tools.sm_exceptions import ConvergenceWarning, PerfectSeparationWarning

from roll_call.model import features, score, store

log = logging.getLogger(__name__)

MIN_TRAINING_ROWS = 30
PERFORMANCE_WINDOW = 10

REASON_FIRST = "first_model"
REASON_LOST = "lost_to_persistence"
REASON_SEASON_CLOSED = "season_closed"

FRAME_COLUMNS = ("made_on", "target_date", "count", "is_estimate", *features.FEATURES)


class TrainingFailed(RuntimeError):
    pass


def training_frame(con: duckdb.DuckDBPyConnection, today: date) -> pd.DataFrame:
    """One row per counted target date up to today, with the features as of the day before.

    Only counted days are targets. Quarantined observations that are open or rejected are gone
    already (`features.usable_counts_sql`). Estimates stay in, flagged by is_estimate. Rows whose
    features cannot be computed, such as the first count of a season, are dropped."""
    inputs = features.load_inputs(con, air="archive")
    rows = []
    for target in sorted(inputs.counts):
        if target > today:
            continue
        row, _reason = features.compute(inputs, target - timedelta(days=1))
        if row is None:
            continue
        count, is_estimate = inputs.counts[target]
        rows.append({"made_on": row.made_on, "target_date": target, "count": count,
                     "is_estimate": is_estimate, **row.values})
    return pd.DataFrame(rows, columns=list(FRAME_COLUMNS))


def _design(frame: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]:
    x = sm.add_constant(frame[list(features.FEATURES)].astype(float), has_constant="add")
    return frame["count"].astype(float), x


def _glm(y: pd.Series, x: pd.DataFrame, family: sm.families.Family):
    """Fit a GLM and fail loudly when it does not converge."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        warnings.simplefilter("error", PerfectSeparationWarning)
        result = sm.GLM(y, x, family=family).fit(maxiter=200)
    converged = getattr(result, "converged", True)
    if not converged or not np.all(np.isfinite(result.params)):
        raise TrainingFailed("the fit did not converge")
    return result


def _dispersion(y: pd.Series, mu: np.ndarray) -> float:
    """Cameron and Trivedi: regress ((y - mu)^2 - y) / mu on mu, with no intercept."""
    z = ((y.to_numpy() - mu) ** 2 - y.to_numpy()) / mu
    return float(np.sum(z * mu) / np.sum(mu * mu))


def fit(frame: pd.DataFrame) -> dict:
    """Fit the model and return its parameters as plain JSON-ready values:
    {"family", "alpha", "features", "coefficients", "fallback_reason"}."""
    y, x = _design(frame)
    log_link = sm.families.links.Log()
    try:
        poisson = _glm(y, x, sm.families.Poisson(link=log_link))
    except Exception as e:  # noqa: BLE001  any failure here leaves no model to keep
        raise TrainingFailed(f"the Poisson fit failed: {e}") from e

    family, result, alpha, fallback_reason = "poisson", poisson, None, None
    try:
        alpha = _dispersion(y, poisson.fittedvalues.to_numpy())
        if not math.isfinite(alpha) or alpha <= 0:
            raise TrainingFailed(f"no overdispersion (alpha {alpha:.4g})")
        result = _glm(y, x, sm.families.NegativeBinomial(alpha=alpha, link=log_link))
        family = "negative_binomial"
    except Exception as e:  # noqa: BLE001  the Poisson fit is the planned fallback
        fallback_reason = f"negative binomial failed, kept Poisson: {e}"
        alpha = None
        log.warning(fallback_reason)

    return {
        "family": family,
        "alpha": alpha,
        "features": list(features.FEATURES),
        "coefficients": {name: float(v) for name, v in result.params.items()},
        "fallback_reason": fallback_reason,
    }


def expected_count(params: dict, values: dict[str, float]) -> float:
    """The model's prediction: exp of the linear predictor, from the stored parameters alone."""
    coef = params["coefficients"]
    eta = coef["const"] + sum(coef[name] * values[name] for name in params["features"])
    return math.exp(eta)


def train(con: duckdb.DuckDBPyConnection, today: date, reason: str,
          season_open: bool | None = None) -> store.ModelVersion | None:
    """Fit on everything usable up to today and store a new model version. Returns None, and
    stores nothing, when there are fewer than MIN_TRAINING_ROWS rows."""
    frame = training_frame(con, today)
    if len(frame) < MIN_TRAINING_ROWS:
        log.info("not training: %d training rows, %d needed", len(frame), MIN_TRAINING_ROWS)
        return None
    params = fit(frame)
    version = store.ModelVersion(
        model_version=store.next_version_id(con, today),
        trained_on=today,
        family=params["family"],
        params=params,
        training_rows=len(frame),
        estimate_rows=int(frame["is_estimate"].sum()),
        first_target=frame["target_date"].min(),
        last_target=frame["target_date"].max(),
        season_open=season_open,
        reason=reason,
    )
    store.save_model_version(con, version)
    log.info("trained %s (%s) on %d rows: %s", version.model_version, version.family,
             version.training_rows, reason)
    return version


def _last_count_date(con: duckdb.DuckDBPyConnection, today: date) -> date | None:
    counts = features.load_counts(con)
    days = [d for d in counts if d <= today]
    return max(days) if days else None


def _season_close_due(con: duckdb.DuckDBPyConnection, today: date) -> str | None:
    last = _last_count_date(con, today)
    if last is None:
        return None
    (done,) = con.execute(
        f"SELECT count(*) FROM {store.MODEL_VERSIONS} WHERE trained_on > ? AND season_open = false",
        [last]).fetchone()
    if done:
        return None
    return f"{REASON_SEASON_CLOSED}: season closed after the last count on {last}"


def _lost_to_persistence(con: duckdb.DuckDBPyConnection, model_version: str) -> str | None:
    recent = score.scored_predictions(con, model_version).head(PERFORMANCE_WINDOW)
    if len(recent) < PERFORMANCE_WINDOW:
        return None
    model_mae = float(recent["model_error"].mean())
    persistence_mae = float(recent["persistence_error"].mean())
    if model_mae <= persistence_mae:
        return None
    return (f"{REASON_LOST}: mean absolute error {model_mae:.1f} against persistence "
            f"{persistence_mae:.1f} over the last {PERFORMANCE_WINDOW} scored predictions")


def retrain_if_needed(con: duckdb.DuckDBPyConnection, today: date) -> bool:
    """Retrain when one of the three reasons in the module docstring holds. Returns whether a new
    model version was stored."""
    store.ensure_tables(con)
    season_open = features.quality_module("season").is_open(con, today)
    current = store.current_model(con)

    if current is None:
        reason = f"{REASON_FIRST}: no model yet"
    else:
        reason = _lost_to_persistence(con, current.model_version)
        if reason is None and not season_open:
            reason = _season_close_due(con, today)
    if reason is None:
        return False

    log.info("retraining: %s", reason)
    return train(con, today, reason, season_open=season_open) is not None
