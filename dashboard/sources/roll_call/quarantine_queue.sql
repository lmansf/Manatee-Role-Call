-- Reads quarantine_queue.csv. See meta.sql for why the first row is a placeholder.
select
    true as is_placeholder,
    null::varchar as observation_date,
    null::varchar as source,
    null::varchar as check_name,
    null::varchar as value,
    null::varchar as status,
    null::varchar as opened_on,
    null::varchar as cleared_on,
    null::double as days_open
union all
select
    false as is_placeholder,
    observation_date,
    source,
    check_name,
    value,
    status,
    opened_on,
    cleared_on,
    days_open
from read_csv(
    'sources/roll_call/quarantine_queue.csv',
    header = true,
    auto_detect = false,
    columns = {
        'observation_date': 'VARCHAR',
        'source': 'VARCHAR',
        'check_name': 'VARCHAR',
        'value': 'VARCHAR',
        'status': 'VARCHAR',
        'opened_on': 'VARCHAR',
        'cleared_on': 'VARCHAR',
        'days_open': 'DOUBLE'
    }
)
