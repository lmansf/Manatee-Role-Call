"""Feature rows for predicting the researchers' count on the next calendar day.

A prediction made on day D targets D + 1. Its features use only what was known on D:

- log_last_count: log(1 + the last count on or before D).
- days_since_last_count: days from that count to the target date. A Monday target predicted on
  Sunday, after an unreported weekend, gets 3.
- gauge_temp_c: the latest gauge temperature on or before D.
- gauge_change_3d_c: that reading minus the reading three days earlier.
- gauge_degree_hours_below: degree-hours below the refuge threshold over the gauge's last three
  days. The gauge series is daily means, so each day counts 24 hours at its mean.
- air_min_c: the minimum air temperature on the target date.
- air_degree_days_below: degree-days below the refuge threshold on D and on the target date,
  from the daily mean air temperature.

Live predictions read air weather from the forecast. Training reads the archive in its place,
because the archive is the only record of observed air weather (spec section 3.2). Counts are the
researchers' only. Not counted and unreported days are skipped, and quarantined observations that
are open or rejected are left out everywhere, so a doubted count never drives a prediction.
"""
from __future__ import annotations

import importlib
import math
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from types import ModuleType
from typing import Literal

import duckdb

from roll_call import config

FEATURES: tuple[str, ...] = (
    "log_last_count",
    "days_since_last_count",
    "gauge_temp_c",
    "gauge_change_3d_c",
    "gauge_degree_hours_below",
    "air_min_c",
    "air_degree_days_below",
)

# A count older than this says little about tomorrow, and it would join one season to the next.
MAX_DAYS_SINCE_COUNT = 14
# The gauge publishes every day. A reading older than this means the gauge feed has stalled.
GAUGE_MAX_LAG_DAYS = 3
GAUGE_WINDOW_DAYS = 3
GAUGE_CHANGE_DAYS = 3
# A forecast issued longer ago than this is too stale to use for a live prediction.
FORECAST_MAX_AGE_DAYS = 2

AirSource = Literal["archive", "forecast"]
AirByDay = dict[date, tuple[float | None, float | None]]  # min, mean


def quality_module(name: str) -> ModuleType:
    """Import `roll_call.quality.<name>` at call time. The quality package is written separately,
    and tests replace its modules through sys.modules."""
    return importlib.import_module(f"roll_call.quality.{name}")


def usable_counts_sql() -> str:
    """The researchers' counts the model may use: counted days that pass the quarantine gate.

    The gate's predicate is applied to the counts table alone, so it can name that table's
    columns without a prefix."""
    mask = quality_module("gate").training_mask_sql()
    return f"""
        SELECT report_date, count_researchers, coalesce(is_estimate, false) AS is_estimate
        FROM {config.TABLES.counts_daily}
        WHERE count_researchers IS NOT NULL
          AND NOT coalesce(not_counted, false)
          AND ({mask})
    """


@dataclass
class Inputs:
    """Everything the features read, loaded once. Dates map to values."""

    counts: dict[date, tuple[int, bool]] = field(default_factory=dict)  # count, is_estimate
    gauge: dict[date, float] = field(default_factory=dict)  # daily mean, degrees C
    air: AirByDay = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._count_dates = sorted(self.counts)
        self._gauge_dates = sorted(self.gauge)

    def last_count_on_or_before(self, day: date) -> tuple[date, int] | None:
        i = bisect_right(self._count_dates, day)
        if i == 0:
            return None
        d = self._count_dates[i - 1]
        return d, self.counts[d][0]

    def latest_gauge_on_or_before(self, day: date) -> date | None:
        i = bisect_right(self._gauge_dates, day)
        return self._gauge_dates[i - 1] if i else None


@dataclass(frozen=True)
class FeatureRow:
    made_on: date
    target_date: date
    last_count: int  # the persistence prediction
    last_count_date: date
    values: dict[str, float]


def _table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    (n,) = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()
    return n > 0


def load_counts(con: duckdb.DuckDBPyConnection) -> dict[date, tuple[int, bool]]:
    if not _table_exists(con, config.TABLES.counts_daily):
        return {}
    rows = con.execute(usable_counts_sql()).fetchall()
    return {d: (int(c), bool(e)) for d, c, e in rows}


def load_gauge(con: duckdb.DuckDBPyConnection) -> dict[date, float]:
    """One gauge temperature per day. An approved value wins over a provisional one."""
    if not _table_exists(con, config.TABLES.gauge_daily):
        return {}
    rows = con.execute(f"""
        SELECT obs_date,
               arg_max(water_temp_c,
                       CASE WHEN lower(coalesce(approval_status, '')) LIKE 'a%' THEN 1 ELSE 0 END)
        FROM {config.TABLES.gauge_daily}
        WHERE water_temp_c IS NOT NULL
        GROUP BY obs_date
    """).fetchall()
    return {d: float(t) for d, t in rows}


def load_archive_air(con: duckdb.DuckDBPyConnection) -> AirByDay:
    if not _table_exists(con, config.TABLES.weather_daily):
        return {}
    rows = con.execute(f"""
        SELECT obs_date, temperature_2m_min, temperature_2m_mean
        FROM {config.TABLES.weather_daily}
    """).fetchall()
    return {d: (lo, mean) for d, lo, mean in rows}


def load_forecast_air(con: duckdb.DuckDBPyConnection, made_on: date) -> AirByDay:
    """Air weather for the prediction day and the target date, from the latest forecast issued on
    or before the prediction day and no more than FORECAST_MAX_AGE_DAYS earlier."""
    if not _table_exists(con, config.TABLES.weather_forecast):
        return {}
    rows = con.execute(f"""
        SELECT target_date,
               arg_max(temperature_2m_min, issue_date),
               arg_max(temperature_2m_mean, issue_date)
        FROM {config.TABLES.weather_forecast}
        WHERE issue_date BETWEEN ? AND ?
          AND target_date BETWEEN ? AND ?
        GROUP BY target_date
    """, [made_on - timedelta(days=FORECAST_MAX_AGE_DAYS), made_on,
          made_on, made_on + timedelta(days=1)]).fetchall()
    return {d: (lo, mean) for d, lo, mean in rows}


def load_inputs(con: duckdb.DuckDBPyConnection, air: AirSource,
                made_on: date | None = None) -> Inputs:
    """Load the counts, the gauge and one air source. The forecast needs `made_on`."""
    if air == "forecast":
        if made_on is None:
            raise ValueError("forecast air weather needs the prediction day")
        air_values = load_forecast_air(con, made_on)
    else:
        air_values = load_archive_air(con)
    return Inputs(counts=load_counts(con), gauge=load_gauge(con), air=air_values)


def _below(threshold: float, value: float) -> float:
    return max(0.0, threshold - value)


def compute(inputs: Inputs, made_on: date) -> tuple[FeatureRow | None, str | None]:
    """The feature row for a prediction made on `made_on`, or None and why it cannot be built."""
    target = made_on + timedelta(days=1)
    threshold = config.REFUGE_THRESHOLD_C

    last = inputs.last_count_on_or_before(made_on)
    if last is None:
        return None, "no count on or before the prediction day"
    last_date, last_count = last
    days_since = (target - last_date).days
    if days_since > MAX_DAYS_SINCE_COUNT:
        return None, f"last count is {days_since} days before the target date"

    gauge_date = inputs.latest_gauge_on_or_before(made_on)
    if gauge_date is None or (made_on - gauge_date).days > GAUGE_MAX_LAG_DAYS:
        return None, "no recent gauge temperature"
    gauge_temp = inputs.gauge[gauge_date]
    earlier = inputs.gauge.get(gauge_date - timedelta(days=GAUGE_CHANGE_DAYS))
    if earlier is None:
        return None, f"no gauge temperature {GAUGE_CHANGE_DAYS} days before {gauge_date}"
    window = [inputs.gauge.get(gauge_date - timedelta(days=k)) for k in range(GAUGE_WINDOW_DAYS)]
    degree_hours = sum(24.0 * _below(threshold, t) for t in window if t is not None)

    air_target = inputs.air.get(target)
    air_today = inputs.air.get(made_on)
    if air_target is None or air_target[0] is None or air_target[1] is None:
        return None, f"no air temperature for {target}"
    if air_today is None or air_today[1] is None:
        return None, f"no air temperature for {made_on}"
    degree_days = _below(threshold, air_today[1]) + _below(threshold, air_target[1])

    values = {
        "log_last_count": math.log1p(last_count),
        "days_since_last_count": float(days_since),
        "gauge_temp_c": gauge_temp,
        "gauge_change_3d_c": gauge_temp - earlier,
        "gauge_degree_hours_below": degree_hours,
        "air_min_c": float(air_target[0]),
        "air_degree_days_below": degree_days,
    }
    return FeatureRow(made_on, target, last_count, last_date, values), None
