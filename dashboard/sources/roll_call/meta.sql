-- Reads meta.csv. Paths are relative to the dashboard folder, where npm runs.
-- Evidence writes no data file for a table with zero rows, and the build then fails.
-- The first row is a placeholder that keeps the table non-empty; pages filter it out
-- with "where not is_placeholder". Dates stay text here and are cast on each page.
select
    true as is_placeholder,
    null::varchar as generated_at_utc,
    null::varchar as git_sha
union all
select
    false as is_placeholder,
    generated_at_utc,
    git_sha
from read_csv(
    'sources/roll_call/meta.csv',
    header = true,
    auto_detect = false,
    columns = {
        'generated_at_utc': 'VARCHAR',
        'git_sha': 'VARCHAR'
    }
)
