-- Reads source_status.csv. See meta.sql for why the first row is a placeholder.
select
    true as is_placeholder,
    null::varchar as run_date,
    null::varchar as source,
    null::varchar as status,
    null::varchar as last_success_utc,
    null::double as row_count,
    null::double as rows_expected,
    null::double as null_rate,
    null::double as null_rate_normal
union all
select
    false as is_placeholder,
    run_date,
    source,
    status,
    last_success_utc,
    row_count,
    rows_expected,
    null_rate,
    null_rate_normal
from read_csv(
    'sources/roll_call/source_status.csv',
    header = true,
    auto_detect = false,
    columns = {
        'run_date': 'VARCHAR',
        'source': 'VARCHAR',
        'status': 'VARCHAR',
        'last_success_utc': 'VARCHAR',
        'row_count': 'DOUBLE',
        'rows_expected': 'DOUBLE',
        'null_rate': 'DOUBLE',
        'null_rate_normal': 'DOUBLE'
    }
)
