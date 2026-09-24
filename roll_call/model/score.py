"""Scoring predictions against persistence.

A prediction is scored once its target date turns out counted. Its error is the absolute
difference from the researchers' count. Persistence predicts the last count known when the
prediction was made, and it is scored the same way. Counts held back by the quarantine gate score
nothing, so a doubted count cannot trigger a retrain.
"""
from __future__ import annotations

import duckdb
import pandas as pd

from roll_call.model import features, store

SCORE_COLUMNS = ("made_on", "target_date", "model_version", "predicted", "persistence", "count",
                 "is_estimate", "model_error", "persistence_error")


def scored_predictions(con: duckdb.DuckDBPyConnection, model_version: str | None = None,
                       include_estimates: bool = True) -> pd.DataFrame:
    """Every scored prediction, newest target date first. Filter to one model version, or leave
    estimates out, to compare scores with and without them."""
    store.ensure_tables(con)
    where = ["TRUE"]
    params: list = []
    if model_version is not None:
        where.append("p.model_version = ?")
        params.append(model_version)
    if not include_estimates:
        where.append("NOT u.is_estimate")
    rows = con.execute(f"""
        WITH usable AS ({features.usable_counts_sql()})
        SELECT p.made_on, p.target_date, p.model_version, p.predicted,
               i.last_count AS persistence,
               u.count_researchers AS count,
               u.is_estimate,
               abs(p.predicted - u.count_researchers) AS model_error,
               abs(i.last_count - u.count_researchers) AS persistence_error
        FROM {store.PREDICTIONS} p
        JOIN {store.PREDICTION_INPUTS} i USING (made_on, target_date)
        JOIN usable u ON u.report_date = p.target_date
        WHERE {' AND '.join(where)}
        ORDER BY p.target_date DESC, p.made_on DESC
    """, params).fetchall()
    return pd.DataFrame(rows, columns=list(SCORE_COLUMNS))


def summary(con: duckdb.DuckDBPyConnection, model_version: str | None = None,
            include_estimates: bool = True) -> dict:
    """Scored count and mean absolute error of the model and of persistence."""
    frame = scored_predictions(con, model_version, include_estimates)
    if frame.empty:
        return {"scored": 0, "model_mae": None, "persistence_mae": None}
    return {"scored": len(frame),
            "model_mae": float(frame["model_error"].mean()),
            "persistence_mae": float(frame["persistence_error"].mean())}
