"""The daily prediction: tomorrow's researchers' count, made every day of an open season."""
from __future__ import annotations

import logging
from datetime import date

import duckdb

from roll_call.model import features, store, train

log = logging.getLogger(__name__)


def run(con: duckdb.DuckDBPyConnection, today: date) -> dict | None:
    """Predict the count for today + 1 with the current model and store it.

    Returns the stored prediction as a dict, with the persistence prediction beside it. Returns
    None outside an open season, before the first model is trained, and when an input is not
    available. The last two are logged with the reason."""
    if not features.quality_module("season").is_open(con, today):
        return None
    model = store.current_model(con)
    if model is None:
        log.info("no prediction for %s: no model has been trained yet", today)
        return None

    inputs = features.load_inputs(con, air="forecast", made_on=today)
    row, reason = features.compute(inputs, today)
    if row is None:
        log.warning("no prediction for %s: %s", today, reason)
        return None

    predicted = train.expected_count(model.params, row.values)
    store.save_prediction(con, made_on=row.made_on, target_date=row.target_date,
                          predicted=predicted, model_version=model.model_version,
                          last_count=row.last_count, last_count_date=row.last_count_date,
                          features=row.values)
    return {"made_on": row.made_on, "target_date": row.target_date, "predicted": predicted,
            "model_version": model.model_version, "persistence": row.last_count}
