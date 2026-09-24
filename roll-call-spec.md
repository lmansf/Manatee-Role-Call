# Roll Call: project spec

A data pipeline whose real subject is the **data-quality layer**. The manatee prediction is the
excuse; the point of the project is catching a source that changes underneath you.

Vocabulary is defined in [`CONTEXT.md`](CONTEXT.md) and used strictly here: *count*, *counter*,
*roll call*, *forecast* (weather only) and *prediction* (the model's output) mean exactly what
the glossary says. Decisions that were hard to reverse or surprising are recorded in
[`docs/adr/`](docs/adr/). Source endpoints and scraping notes are in
[`docs/sources.md`](docs/sources.md).

---

## Kickoff prompt (for a fresh coding session)

> I'm building a portfolio data project called **Roll Call**: a Python pipeline, run daily by a
> systemd timer on my Ubuntu machine and storing everything in one DuckDB file, that predicts
> tomorrow's manatee count at Blue Spring State Park from weather and river temperature. The
> real focus is the data-quality layer around it: freshness, volume and consistency checks,
> quarantine for doubtful observations, incidents for failing sources, and a public Evidence
> dashboard on Vercel, rebuilt from summary CSVs that each run pushes to the repo.
>
> Read `roll-call-spec.md`, `CONTEXT.md` and `docs/adr/` first. I write the code myself where
> it's instructive: give me structure, let me fill in the logic, and flag where I'd learn more
> by hitting the problem first.

---

## 1. Purpose

Most portfolio pipelines show that data *moved*. This one shows that the data was *watched*:
what the pipeline did when a source drifted, went stale, or started lying.

Deliverables: a GitHub repo, an Evidence dashboard site on Vercel, and a short written walkthrough.

## 2. Platform

See [ADR 0002](docs/adr/0002-local-scheduled-script-with-duckdb.md).

- **Runs on:** the owner's Ubuntu machine, as a plain Python script, from a runner clone of the
  repo at `~/roll-call-runner`. The runner clone stays on `main` and is never used for editing.
- **Schedule:** a systemd user timer at 19:00 local, `Persistent=true` so a run missed while the
  machine was off or asleep fires when it's back. Setup: [`docs/scheduling.md`](docs/scheduling.md).
- **Catch-up:** every run fetches everything since the last successful run for each source, not
  just yesterday. A missed run therefore costs at most that day's weather forecast.
- **Storage:** one DuckDB file. Every table a run touches is keyed so re-running is safe.
- **Publish:** the run's last step, `jobs/publish_dashboard.py`, exports the dashboard CSVs,
  commits them and pushes to `main` (§6).
- **Push access:** an SSH deploy key with write access to this one repo, not a personal token.
  Setup: [`docs/scheduling.md`](docs/scheduling.md).
- **Dashboard hosting:** Vercel rebuilds the Evidence site on every push to `main`
  ([ADR 0003](docs/adr/0003-evidence-on-vercel-instead-of-tableau.md)).
- **Secrets:** a `.env` file in the repo folder, ignored by git. `.env.example` lists the keys.
  The deploy key lives in `~/.ssh`, outside the repo.
- **Backup:** clearing decisions and the baseline refresh log are exported as CSV and committed
  (they are the records only a person could make). The whole database file is copied weekly to
  a second disk by a second timer.
- **Not used:** Databricks (outbound allowlist, quota shutdowns), GitHub Actions (owner's call),
  Dagster (a separate learning series), Tableau Public with Google Sheets (ADR 0003).

## 3. Data sources

### 3.1 Save the Manatee Club reports (the counts)

- The researchers' daily roll call during the season, published as prose on season pages; the
  current season lives on the hub page until archived. Scraped, no API. This is deliberate: a
  scraped page is the source most likely to change silently.
- Each report can give two counts, the researchers' and the park's, plus the report temperature.
- Historical seasons 2018–19 onward are extracted once by `tools/extract_seasons.py` into one
  CSV per season, reviewed, then aggregated (aggregation rule still open, §8).
- A hand-transcribed 2025–26 set in `data/reference/` scores the extraction. It is never a source.

### 3.2 Open-Meteo (weather)

- **Archive** (ERA5 reanalysis, ~5-day lag): the only source of observed air weather. Feeds
  training features and weather baselines. Six daily fields plus hourly temperature, units
  pinned (see `docs/sources.md` §1).
- **Forecast:** the 7-day daily forecast for the same fields, stored every run with its issue
  date. Feeds live predictions. Forecast revisions between issues are genuine drift.
- The live run never uses observed air weather; the archive lag makes yesterday's unavailable.

### 3.3 USGS gauge 02236000, St. Johns River near DeLand (river temperature)

- Continuous water temperature about 7 km downstream of the park, including weekends, with
  years of history. It is the model's river temperature input.
- The report temperature becomes a cross-check between two independent sources.
- Values are published as provisional and later approved; revisions are recorded, not
  overwritten, like forecast revisions.

### 3.4 Synthetic mutation generator (test fixture)

- A standalone script run on demand (`tools/mutate.py`). It works on a *copy* of the database
  file and refuses the live one.
- Mutations: schema drift, null spikes, unit changes, duplicates, renamed keys. Its job is to
  prove every check fires. Accepted trade-off: it is a closed loop, which is why real sources
  sit beside it.

### 3.5 Out of the first version

- Florida's statewide aerial surveys (FWC). One to three flights a winter: context only, and a
  fourth source to watch for no model benefit. Endpoints kept in `docs/sources.md`.

## 4. The model

- **Target:** the researchers' count. Park counts are never substituted. Estimates are included
  and flagged, so scores can be computed with and without them. Counts written as sums are
  ordinary counts.
- **Prediction:** tomorrow's count, for every calendar day of an open season. Scored only on days
  that turn out counted.
- **Model:** a count regression (Poisson or negative binomial) with hand-picked features. Feature
  selection stays manual so the performance history stays readable.
- **Features:** today's (or the last) count, days since the last count, gauge temperature and its
  recent trend, degree-hours below 20°C (river and forecast air), and tomorrow's forecast. Reuse
  the lag and calendar features from the owner's attendance model where they fit.
- **Physics note:** Blue Spring is a natural spring at a constant ~72°F. Manatees gather when the
  river drops below roughly 20°C, a threshold effect worth modelling explicitly.
- **Training data vs live inputs:** training stands in archive air weather for forecasts; the size
  of that mismatch is measurable from forecast history since 2024.

### Retraining

- **Triggered by performance, never by quality flags.** Retrain when the model does worse than
  persistence over the last 10 scored predictions, and once a year at season close.
- Quarantined observations are excluded from training until cleared. Flags gate the training
  set; performance triggers the retrain.

## 5. The quality layer

### Checks

| Domain | Checks |
|---|---|
| **Freshness** | Last successful run per source. Season-aware: the season opens at the first report; no opening after the latest plausible start is an incident; the season closes after five silent weekdays once March begins. Unreported weekends are normal. |
| **Volume** | Rows per run against expectation; duplicate rejection; join retention between counts, weather and gauge by date. |
| **Distribution** | Null rate per column; units against the pinned units. Weather fields: beyond three standard deviations of the 30-year normal for that day of the year. |
| **Counts** | Not judged by standard deviations ([ADR 0001](docs/adr/0001-counts-not-judged-by-standard-deviations.md)). Plausible range, and jumps that contradict the river temperature. Counter disagreement is monitored, never a reason to quarantine. |
| **Cross-source** | Report temperature against gauge temperature. |

Structural checks catch schema drift. Value-level checks catch the nastier cases: a column that
was always populated arriving null, or a renamed field silently dropping joined rows.

**Join keys:** surrogate keys on stable identifiers (site, date, counter), never display names.

**Thresholds** start as placeholders (latest plausible start 15 November, five silent weekdays,
counts 0–1,500) and are calibrated from the historical seasons once extracted.

### Baselines

- Fixed, refreshed once a year at season close. Never mid-season: a refresh would absorb the
  cold-snap behaviour the checks watch for.
- Weather baseline: the normal for each day of the year over the trailing 30 years.
- Every refresh logs old and new side by side. Without this, a scheduled refresh quietly becomes
  a rolling window.
- Past years are replayed with the live rule and labelled as replayed, so the drift history
  starts where the data does.

### Quarantine, incidents and clearing

- A doubtful observation is **quarantined**: kept, never dropped, held out of training.
- A failing source-level check opens an **incident**, which closes itself when the check passes.
- The owner **clears** quarantined observations in a local Jupyter notebook: confirmed real or
  rejected, with a reason, written to the database. A big number is not automatically bad data.

### Alerting

- Resend (permanent free tier: 3,000 a month, 100 a day). SendGrid retired its free plan in 2025.
- One email per run, only when something new opened, listing new incidents and new quarantined
  observations.

## 6. Dashboard

An [Evidence](https://evidence.dev) site deployed on Vercel. Evidence builds a static site from
SQL and Markdown and runs its queries at build time. Why not Tableau Public:
[ADR 0003](docs/adr/0003-evidence-on-vercel-instead-of-tableau.md).

**The path:**

1. The daily run ends with `jobs/publish_dashboard.py`. It computes the summaries in Python and
   writes them as CSV into `dashboard/sources/roll_call/`.
2. If any file changed, it commits only that folder and pushes to `main` with the deploy key.
   It refuses to run on another branch or with other uncommitted changes.
3. Vercel sees the push and rebuilds the site from `dashboard/`.

**The files:**

| File | Contents |
|---|---|
| `meta.csv` | When the data was generated. |
| `source_status.csv` | Per source: last successful run, rows against expectation, null rate against normal. |
| `baseline_drift.csv` | Per baseline refresh: old and new values, and whether the refresh was replayed. |
| `quarantine_queue.csv` | Per quarantined observation: source, check, dates quarantined and cleared, outcome. |

**Write summaries, not raw logs:** metrics are precomputed in Python, and the site only charts
them.

**Public by design:** the repo and the site are public. The CSVs carry dates, source and check
names, numbers and statuses only. Report text names people and never goes there, and neither do
clearing reasons. The DuckDB file is never committed.

**Staleness:** the site shows when its data was generated and warns when that is more than two
days old. The owner's machine may be off, and the dashboard has to say so.

### Views

1. **Drift over time (cumulative).** Cumulative signed change in each baseline across refreshes,
   including replayed years.
2. **Per-source status.** Last successful run, rows against expectation, null rate against normal.
3. **Quarantine queue.** Open items and time to clear, the metric that proves the loop closes.

## 7. Naming

**Roll Call**: what the Blue Spring morning count is called, and what the pipeline does daily.

Rejected: **Manatee Watch**, already the name of Volusia County Environmental Management's
volunteer program, in the same county as Blue Spring.

## 8. Open, pending data

1. How to aggregate the per-season CSVs into one history.
2. Threshold values (see §5).
3. Which features carry over from the attendance model.
