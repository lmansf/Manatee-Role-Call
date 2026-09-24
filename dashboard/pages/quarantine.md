---
title: Quarantine queue
sidebar_position: 3
---

```sql queue
select
    try_cast(observation_date as date) as observation_date,
    source,
    check_name,
    value,
    status,
    case status
        when 'open' then 'Open'
        when 'confirmed' then 'Confirmed real'
        when 'rejected' then 'Rejected'
        else status
    end as status_label,
    try_cast(opened_on as date) as opened_on,
    try_cast(cleared_on as date) as cleared_on,
    days_open
from roll_call.quarantine_queue
where not is_placeholder
```

```sql open_items
select observation_date, source, check_name, value, opened_on, days_open
from ${queue}
where status = 'open'
order by days_open desc, observation_date
```

```sql cleared_items
select observation_date, source, check_name, value, status_label, opened_on, cleared_on, days_open
from ${queue}
where status in ('confirmed', 'rejected')
order by cleared_on desc, observation_date desc
```

```sql summary
select
    count(*) filter (where status = 'open') as open_count,
    count(*) filter (where status in ('confirmed', 'rejected')) as cleared_count,
    median(days_open) filter (where status in ('confirmed', 'rejected')) as median_days_to_clear,
    max(days_open) filter (where status = 'open') as oldest_open_days
from ${queue}
```

A check that doubts a single observation quarantines it. The pipeline keeps the observation but holds it out of training until a person clears it, either as confirmed real or as rejected. Time to clear shows whether that loop closes.

{#if queue.length === 0}

<Alert status="info">
No observation has been quarantined yet. Items appear here when a check doubts an observation.
</Alert>

{:else}

<BigValue data={summary} value=open_count title="Open now" fmt=num0/>
<BigValue data={summary} value=oldest_open_days title="Oldest open item (days)" fmt=num0/>
<BigValue data={summary} value=cleared_count title="Cleared" fmt=num0/>
<BigValue data={summary} value=median_days_to_clear title="Median days to clear" fmt=num1/>

## Open quarantined observations

{#if open_items.length === 0}

Nothing is waiting to be cleared.

{:else}

<DataTable data={open_items} rows=20 wrapTitles=true>
    <Column id=observation_date title="Observation date" fmt="yyyy-mm-dd"/>
    <Column id=source title="Source"/>
    <Column id=check_name title="Check" wrap=true/>
    <Column id=value title="Value"/>
    <Column id=opened_on title="Quarantined on" fmt="yyyy-mm-dd"/>
    <Column id=days_open title="Days open" fmt=num0/>
</DataTable>

{/if}

## Time to clear

{#if cleared_items.length === 0}

No quarantined observation has been cleared yet.

{:else}

Each point is one cleared observation: the day it was cleared and how many days it stayed open.

<ScatterPlot
    data={cleared_items}
    x=cleared_on
    y=days_open
    series=status_label
    seriesOrder={['Confirmed real', 'Rejected']}
    seriesColors={{'Confirmed real': '#236aa4', 'Rejected': '#c2410c'}}
    xAxisTitle="Cleared on"
    yAxisTitle="Days open"
    pointSize=10
    yMin=0
    emptySet=pass
/>

<DataTable data={cleared_items} rows=20 wrapTitles=true>
    <Column id=observation_date title="Observation date" fmt="yyyy-mm-dd"/>
    <Column id=source title="Source"/>
    <Column id=check_name title="Check" wrap=true/>
    <Column id=value title="Value"/>
    <Column id=status_label title="Decision"/>
    <Column id=cleared_on title="Cleared on" fmt="yyyy-mm-dd"/>
    <Column id=days_open title="Days open" fmt=num0/>
</DataTable>

{/if}

{/if}
