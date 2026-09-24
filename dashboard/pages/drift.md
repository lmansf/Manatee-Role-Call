---
title: Baseline drift
sidebar_position: 2
---

```sql drift
select
    try_cast(refreshed_on as date) as refreshed_on,
    lower(trim(replayed)) = 'true' as is_replayed,
    case when lower(trim(replayed)) = 'true' then 'Replayed' else 'Live' end as refresh_type,
    source,
    measure,
    source || ': ' || measure as baseline,
    old_value,
    new_value,
    change,
    cumulative_change
from roll_call.baseline_drift
where not is_placeholder
order by source, measure, refreshed_on
```

```sql measures
select
    source,
    measure,
    count(*) filter (where is_replayed) as replayed_count,
    count(*) filter (where not is_replayed) as live_count,
    strftime(min(refreshed_on) filter (where is_replayed), '%Y-%m-%d') as replayed_start,
    strftime(max(refreshed_on) filter (where is_replayed), '%Y-%m-%d') as replayed_end
from ${drift}
group by source, measure
order by source, measure
```

```sql live_points
select source, measure, refreshed_on, cumulative_change, 'Live' as point_label
from ${drift}
where not is_replayed
```

A baseline is the fixed reference a check compares against. Each baseline is replaced once a year when the season closes, and every refresh logs the old and new values side by side. Each chart adds up the signed changes, so a line that keeps climbing or falling shows a baseline drifting in one direction.

Replayed refreshes are computed after the fact for past years with the same rule as live ones. They give the history its years before the pipeline existed. On each chart a shaded band covers the replayed refreshes and every live refresh carries a label. The table marks each refresh as replayed or live.

{#if drift.length === 0}

<Alert status="info">
No baseline refresh has been logged yet. The first entries appear when past years are replayed or when a season closes.
</Alert>

{:else}

{#each measures as m}

## {m.source}: {m.measure}

<LineChart
    data={drift.where(`source = '${m.source.replaceAll("'", "''")}' and measure = '${m.measure.replaceAll("'", "''")}'`)}
    x=refreshed_on
    y=cumulative_change
    yAxisTitle="Cumulative change"
    xAxisTitle="Refreshed on"
    markers=true
    markerSize=8
    yFmt=num2
    emptySet=pass
>
    <ReferenceLine y=0 hideValue=true/>
    {#if m.replayed_count > 0}
    <ReferenceArea xMin={m.replayed_start} xMax={m.replayed_end} label="Replayed" color=base-content-muted/>
    {/if}
    {#if m.live_count > 0}
    <ReferencePoint
        data={live_points.where(`source = '${m.source.replaceAll("'", "''")}' and measure = '${m.measure.replaceAll("'", "''")}'`)}
        x=refreshed_on
        y=cumulative_change
        label=point_label
        labelPosition=bottom
    />
    {/if}
</LineChart>

{m.replayed_count} replayed and {m.live_count} live refreshes.

{/each}

## Every refresh

<DataTable data={drift} rows=20 wrapTitles=true>
    <Column id=refreshed_on title="Refreshed on" fmt="yyyy-mm-dd"/>
    <Column id=refresh_type title="Refresh type"/>
    <Column id=baseline title="Baseline" wrap=true/>
    <Column id=old_value title="Old value" fmt=num2/>
    <Column id=new_value title="New value" fmt=num2/>
    <Column id=change title="Change" fmt=num2/>
    <Column id=cumulative_change title="Cumulative change" fmt=num2/>
</DataTable>

{/if}
