-- Reads baseline_drift.csv. See meta.sql for why the first row is a placeholder.
select
    true as is_placeholder,
    null::varchar as refreshed_on,
    null::varchar as replayed,
    null::varchar as source,
    null::varchar as measure,
    null::double as old_value,
    null::double as new_value,
    null::double as change,
    null::double as cumulative_change
union all
select
    false as is_placeholder,
    refreshed_on,
    replayed,
    source,
    measure,
    old_value,
    new_value,
    change,
    cumulative_change
from read_csv(
    'sources/roll_call/baseline_drift.csv',
    header = true,
    auto_detect = false,
    columns = {
        'refreshed_on': 'VARCHAR',
        'replayed': 'VARCHAR',
        'source': 'VARCHAR',
        'measure': 'VARCHAR',
        'old_value': 'DOUBLE',
        'new_value': 'DOUBLE',
        'change': 'DOUBLE',
        'cumulative_change': 'DOUBLE'
    }
)
