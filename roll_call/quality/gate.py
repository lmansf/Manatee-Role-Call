"""The training gate: which counts and weather values the model may learn from.

An observation held in quarantine stays out of training while it is open or after it is
rejected. A confirmed observation goes back in. Observations that were never quarantined always
train.

Quarantined counts carry obs_id `blue_spring_counts_daily:<report_date as YYYY-MM-DD>`.
Quarantined weather values carry `weather_daily:<obs_date as YYYY-MM-DD>:<measure>`, because the
check doubts one measure, not the whole day. Both read the quarantine table
(roll_call/quality/store.py) and clearing_decisions (roll_call/quality/baselines.py). The count
predicate needs both tables to exist when the query runs.
"""
from __future__ import annotations

from datetime import date

import duckdb

from roll_call import config
from roll_call.quality import baselines, clearing

COUNTS_TABLE = config.TABLES.counts_daily
WEATHER_TABLE = config.TABLES.weather_daily


def obs_id(report_date) -> str:
    """The quarantine obs_id of the count reported on report_date."""
    return f"{COUNTS_TABLE}:{report_date.isoformat()}"


def training_mask_sql(alias: str = COUNTS_TABLE) -> str:
    """A SQL predicate over blue_spring_counts_daily that is true for trainable counts.

    Use it in a WHERE clause. Pass alias when the query names the counts table another way,
    such as `FROM blue_spring_counts_daily c`.
    """
    return (
        "NOT EXISTS ("
        f"SELECT 1 FROM {clearing.QUARANTINE_TABLE} q "
        f"LEFT JOIN {baselines.DECISIONS_TABLE} d ON d.obs_id = q.obs_id "
        f"WHERE q.obs_id = '{COUNTS_TABLE}:' || strftime({alias}.report_date, '%Y-%m-%d') "
        "AND coalesce(d.decision, 'open') <> 'confirmed')"
    )


def weather_obs_id(obs_date: date, measure: str) -> str:
    """The quarantine obs_id of one weather_daily measure on obs_date."""
    return f"{WEATHER_TABLE}:{obs_date.isoformat()}:{measure}"


def held_out_weather(con: duckdb.DuckDBPyConnection) -> set[tuple[date, str]]:
    """The (obs_date, measure) pairs of weather_daily values the model may not use: quarantined
    and open, or rejected. A confirmed value is usable. Empty when nothing was ever quarantined."""
    if not baselines.table_exists(con, clearing.QUARANTINE_TABLE):
        return set()
    baselines.ensure_tables(con)
    rows = con.execute(
        "SELECT try_cast(split_part(q.obs_id, ':', 2) AS DATE), split_part(q.obs_id, ':', 3) "
        f"FROM {clearing.QUARANTINE_TABLE} q "
        f"LEFT JOIN {baselines.DECISIONS_TABLE} d ON d.obs_id = q.obs_id "
        f"WHERE starts_with(q.obs_id, '{WEATHER_TABLE}:') "
        "AND coalesce(d.decision, 'open') <> 'confirmed'"
    ).fetchall()
    return {(day, measure) for day, measure in rows if day is not None and measure}
