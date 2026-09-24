---
title: Source status
sidebar_position: 1
---

<script>
	const HOUR_MS = 60 * 60 * 1000;
	const DAY_MS = 24 * HOUR_MS;
	const STALE_AFTER_DAYS = 2;

	// This runs again in the visitor's browser, so the age is measured against the
	// time the page is viewed, not the time the site was built.
	$: generatedIso = latest_meta.length ? latest_meta[0].generated_at_utc : null;
	$: generatedMs = generatedIso ? Date.parse(generatedIso) : NaN;
	$: hasExport = !Number.isNaN(generatedMs);
	$: ageMs = hasExport ? Date.now() - generatedMs : null;
	$: isStale = hasExport && ageMs > STALE_AFTER_DAYS * DAY_MS;
	$: generatedText = hasExport
		? new Date(generatedMs).toISOString().slice(0, 16).replace('T', ' ') + ' UTC'
		: null;
	$: ageText = !hasExport
		? null
		: ageMs < DAY_MS
			? `${Math.max(0, Math.floor(ageMs / HOUR_MS))} hours ago`
			: `${Math.floor(ageMs / DAY_MS)} days ago`;
	$: commitText = hasExport && latest_meta[0].git_sha ? latest_meta[0].git_sha.slice(0, 7) : null;
</script>

```sql latest_meta
select generated_at_utc, git_sha
from roll_call.meta
where not is_placeholder
order by generated_at_utc desc
limit 1
```

```sql source_runs
select
    try_cast(run_date as date) as run_date,
    source,
    case status when 'succeeded' then 'Succeeded' when 'failed' then 'Failed' else status end as status,
    strftime(try_cast(replace(replace(last_success_utc, 'T', ' '), 'Z', '') as timestamp), '%Y-%m-%d %H:%M') as last_success_utc,
    row_count,
    rows_expected,
    null_rate,
    null_rate_normal
from roll_call.source_status
where not is_placeholder
```

```sql latest_status
select source, run_date, status, last_success_utc, row_count, rows_expected, null_rate, null_rate_normal
from (
    select *, row_number() over (partition by source order by run_date desc) as recency
    from ${source_runs}
)
where recency = 1
order by source
```

```sql failed_latest
select source from ${latest_status} where status = 'Failed' order by source
```

```sql recent_runs
select run_date, source, status, row_count, rows_expected
from ${source_runs}
where run_date >= (select max(run_date) from ${source_runs}) - interval 13 day
order by run_date desc, source
```

```sql history_sources
select distinct source from ${source_runs} order by source
```

This site shows how Roll Call watches its three sources: Save the Manatee Club reports, Open-Meteo weather and the USGS river gauge. The pipeline runs once a day on a personal computer and publishes these summaries after each run.

## Freshness

{#if !hasExport}

<Alert status="info">
No pipeline run has been published yet. This page fills in after the first daily run exports its summaries.
</Alert>

{:else if isStale}

<Alert status="warning">
<b>Stale data.</b> The last export was on {generatedText}, {ageText}. The pipeline normally publishes once a day. The computer that runs it may be off, or the run may have failed before it could publish. Everything on this site shows the state as of that export.
</Alert>

{:else}

<Alert status="positive">
<b>Up to date.</b> The last export was on {generatedText}, {ageText}.
</Alert>

{/if}

{#if commitText}

Exported by pipeline commit {commitText}.

{/if}

## Latest status per source

{#if latest_status.length === 0}

No source status has been exported yet. Each run adds one row per source.

{:else}

{#if failed_latest.length > 0}

<Alert status="negative">
<b>Failing now:</b> {failed_latest.map((row) => row.source).join(', ')}. The latest run could not fetch or load this source. An incident stays open until a run succeeds again.
</Alert>

{/if}

<DataTable data={latest_status} rows=all wrapTitles=true>
    <Column id=source title="Source"/>
    <Column id=run_date title="Latest run" fmt="yyyy-mm-dd"/>
    <Column id=status title="Status"/>
    <Column id=last_success_utc title="Last success (UTC)"/>
    <Column id=row_count title="Rows" fmt=num0/>
    <Column id=rows_expected title="Rows expected" fmt=num0/>
    <Column id=null_rate title="Null rate" fmt=pct1/>
    <Column id=null_rate_normal title="Normal null rate" fmt=pct1/>
</DataTable>

A dash means the pipeline does not compute that value yet, or the run failed before counting rows.

## Recent runs

This table covers the last 14 run dates. A failed run leaves the last success unchanged.

<DataTable data={recent_runs} rows=15>
    <Column id=run_date title="Run date" fmt="yyyy-mm-dd"/>
    <Column id=source title="Source"/>
    <Column id=status title="Status"/>
    <Column id=row_count title="Rows" fmt=num0/>
    <Column id=rows_expected title="Rows expected" fmt=num0/>
</DataTable>

### Rows per run

Each source loads a different number of rows, so each has its own chart. A gap in a line is a failed run.

{#each history_sources as s}

<LineChart
    data={source_runs.where(`source = '${s.source.replaceAll("'", "''")}'`)}
    x=run_date
    y=row_count
    title={s.source}
    yAxisTitle="Rows"
    markers=true
    yMin=0
    echartsOptions={{yAxis: {minInterval: 1}}}
    emptySet=pass
    emptyMessage="No row counts for this source yet."
/>

{/each}

{/if}
