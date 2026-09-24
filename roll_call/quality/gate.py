"""The training gate: which counts the model may learn from.

A count held in quarantine stays out of training while it is open or after it is rejected. A
confirmed count goes back in. Counts that were never quarantined always train.

Quarantined counts carry obs_id `blue_spring_counts_daily:<report_date as YYYY-MM-DD>`.
The predicate reads the quarantine table (roll_call/quality/store.py) and clearing_decisions
(roll_call/quality/baselines.py), so both must exist when the query runs.
"""
from __future__ import annotations

from roll_call import config
from roll_call.quality import baselines, clearing

COUNTS_TABLE = config.TABLES.counts_daily


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
