"""The model's tables: predictions, the inputs behind each one, and every fitted model version.

A model version keeps its fitted parameters as JSON, so any past prediction can be recomputed from
the database alone. No pickled objects are stored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import duckdb

PREDICTIONS = "predictions"
PREDICTION_INPUTS = "prediction_inputs"
MODEL_VERSIONS = "model_versions"

DDL = (
    f"""
    CREATE TABLE IF NOT EXISTS {PREDICTIONS} (
        made_on       DATE NOT NULL,
        target_date   DATE NOT NULL,
        predicted     DOUBLE NOT NULL,
        model_version VARCHAR NOT NULL,
        PRIMARY KEY (made_on, target_date)
    )
    """,
    # What the model saw when it made a prediction. last_count is the persistence prediction.
    f"""
    CREATE TABLE IF NOT EXISTS {PREDICTION_INPUTS} (
        made_on         DATE NOT NULL,
        target_date     DATE NOT NULL,
        last_count      INTEGER NOT NULL,
        last_count_date DATE NOT NULL,
        features        VARCHAR NOT NULL,  -- JSON: feature name to value
        PRIMARY KEY (made_on, target_date)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {MODEL_VERSIONS} (
        model_version   VARCHAR PRIMARY KEY,
        trained_on      DATE NOT NULL,
        trained_at      TIMESTAMP NOT NULL,  -- UTC
        family          VARCHAR NOT NULL,    -- negative_binomial | poisson
        params          VARCHAR NOT NULL,    -- JSON, see ModelVersion.params
        training_rows   INTEGER NOT NULL,
        estimate_rows   INTEGER NOT NULL,
        first_target    DATE,
        last_target     DATE,
        season_open     BOOLEAN,             -- season state on the day it was trained
        reason          VARCHAR NOT NULL
    )
    """,
)


def ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    for ddl in DDL:
        con.execute(ddl)


@dataclass(frozen=True)
class ModelVersion:
    """One fitted model. `params` holds everything needed to predict:
    {"family", "alpha", "features": [names in order], "coefficients": {"const": ..., name: ...}}."""

    model_version: str
    trained_on: date
    family: str
    params: dict[str, Any]
    training_rows: int
    estimate_rows: int
    reason: str
    first_target: date | None = None
    last_target: date | None = None
    season_open: bool | None = None
    trained_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


def next_version_id(con: duckdb.DuckDBPyConnection, trained_on: date) -> str:
    """Version ids read as the training date plus a sequence number for that day."""
    ensure_tables(con)
    prefix = f"m{trained_on:%Y%m%d}"
    (n,) = con.execute(
        f"SELECT count(*) FROM {MODEL_VERSIONS} WHERE model_version LIKE ?", [prefix + "-%"]
    ).fetchone()
    return f"{prefix}-{n + 1}"


def save_model_version(con: duckdb.DuckDBPyConnection, mv: ModelVersion) -> None:
    ensure_tables(con)
    con.execute(
        f"INSERT INTO {MODEL_VERSIONS} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [mv.model_version, mv.trained_on, mv.trained_at, mv.family, json.dumps(mv.params),
         mv.training_rows, mv.estimate_rows, mv.first_target, mv.last_target, mv.season_open,
         mv.reason],
    )


def _row_to_version(row: tuple) -> ModelVersion:
    (version, trained_on, trained_at, family, params, rows, estimates, first, last, season_open,
     reason) = row
    return ModelVersion(model_version=version, trained_on=trained_on, trained_at=trained_at,
                        family=family, params=json.loads(params), training_rows=rows,
                        estimate_rows=estimates, first_target=first, last_target=last,
                        season_open=season_open, reason=reason)


def current_model(con: duckdb.DuckDBPyConnection) -> ModelVersion | None:
    """The most recently trained model version, or None before the first training."""
    ensure_tables(con)
    row = con.execute(
        f"SELECT * FROM {MODEL_VERSIONS} ORDER BY trained_at DESC, model_version DESC LIMIT 1"
    ).fetchone()
    return _row_to_version(row) if row else None


def model_versions(con: duckdb.DuckDBPyConnection) -> list[ModelVersion]:
    """Every model version, oldest first."""
    ensure_tables(con)
    rows = con.execute(
        f"SELECT * FROM {MODEL_VERSIONS} ORDER BY trained_at, model_version").fetchall()
    return [_row_to_version(r) for r in rows]


def save_prediction(con: duckdb.DuckDBPyConnection, *, made_on: date, target_date: date,
                    predicted: float, model_version: str, last_count: int,
                    last_count_date: date, features: dict[str, float]) -> None:
    """Store one prediction and its inputs. A rerun on the same day replaces that day's row."""
    ensure_tables(con)
    con.begin()
    try:
        con.execute(f"INSERT OR REPLACE INTO {PREDICTIONS} VALUES (?, ?, ?, ?)",
                    [made_on, target_date, predicted, model_version])
        con.execute(f"INSERT OR REPLACE INTO {PREDICTION_INPUTS} VALUES (?, ?, ?, ?, ?)",
                    [made_on, target_date, last_count, last_count_date,
                     json.dumps(features, sort_keys=True)])
        con.commit()
    except Exception:
        con.rollback()
        raise
