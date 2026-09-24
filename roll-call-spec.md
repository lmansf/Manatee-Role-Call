# Roll Call: project spec

A data pipeline whose real subject is the **data-quality layer**. The manatee prediction is the
excuse; the point of the project is catching a source that changes underneath you.

Terms mean exactly what [`CONTEXT.md`](CONTEXT.md) says. The reasons behind hard-to-reverse
choices are in [`docs/adr/`](docs/adr/). Endpoints and scraping notes are in
[`docs/sources.md`](docs/sources.md).

## 1. Purpose

Most portfolio pipelines show that data *moved*. This one shows that the data was *watched*:
what the pipeline did when a source drifted, went stale, or started lying.

Deliverables: a GitHub repo, an Evidence dashboard site on Vercel, and a short written walkthrough.

### Flow

One daily run, in order:

1. **Ingest.** Each source is fetched from its last successful run onward (§2, catch-up). The
   raw payload and the parsed rows land in DuckDB, and every run is logged, failures included.
2. **Check.** Freshness, volume, distribution and consistency checks run against the baselines
   (§5). A doubtful observation is quarantined. A failing source opens an incident.
3. **Alert.** One email lists anything new that opened (§5).
4. **Predict.** In an open season, the model predicts tomorrow's count from the observations
   that aren't quarantined (§4).
5. **Publish.** Summary CSVs are exported, committed and pushed. Vercel rebuilds the site (§6).

Once a year at season close, the baselines refresh and the model retrains (§4, §5).

## 2. Platform

See [ADR 0002](docs/adr/0002-local-scheduled-script-with-duckdb.md).

- **Runs on:** the owner's Ubuntu machine, as a plain Python script, from a runner clone of the
  repo at `~/roll-call-runner`. The runner clone stays on `main` and only the timers use it.
- **Schedule:** a systemd user timer at 19:00 local, `Persistent=true` so a run missed while the
  machine was off or asleep fires when it's back. Setup: [`docs/scheduling.md`](docs/scheduling.md).
- **Catch-up:** every run fetches everything since the last successful run for each source, not
  just yesterday. A missed run therefore costs at most that day's weather forecast.
- **Storage:** one DuckDB file. Every table a run touches is keyed so re-running is safe.
- **Publish:** the run's last step, `jobs/publish_dashboard.py`, exports the dashboard CSVs,
  commits them and pushes to `main` (§6).
- **Push access:** a fine-grained personal access token limited to this repo, with read and
  write access to contents. It expires and gets renewed. Setup:
  [`docs/scheduling.md`](docs/scheduling.md).
- **Dashboard hosting:** Vercel rebuilds the Evidence site on every push to `main`
  ([ADR 0003](docs/adr/0003-evidence-on-vercel-instead-of-tableau.md)).
- **Secrets:** a `.env` file in the repo folder, ignored by git. `.env.example` lists the keys.
  The push token lives in git's credential store (`~/.git-credentials`), outside the repo.
- **Backup:** clearing decisions and the baseline refresh log are exported as CSV and committed
  (they are the records only a person could make). The whole database file is copied weekly to
  a second disk by a second timer.
- **Not used:** Databricks and GitHub Actions ([ADR 0002](docs/adr/0002-local-scheduled-script-with-duckdb.md)),
  Tableau Public ([ADR 0003](docs/adr/0003-evidence-on-vercel-instead-of-tableau.md)), and
  Dagster, which the owner is learning in a separate series.

## 3. Data sources

### 3.1 Save the Manatee Club reports (the counts)

- The researchers' daily roll call during the season, published as prose on season pages; the
  current season lives on the hub page until archived. Scraped, no API. This is deliberate: a
  scraped page is the source most likely to change silently.
- Each report can give two counts, the researchers' and the park's, plus the report temperature.
- Historical seasons 2018–19 onward are extracted once by `tools/extract_seasons.py` into one
  CSV per season, reviewed, then aggregated (aggregation rule still open, §8).
- A hand-transcribed 2025–26 set in `data/reference/` scores the extraction and feeds nothing else.

### 3.2 Open-Meteo (weather)

- **Archive** (ERA5 reanalysis, ~5-day lag): the only source of observed air weather. Feeds
  training features and weather baselines. Six daily fields plus hourly temperature, units
  pinned (see `docs/sources.md` §1).
- **Forecast:** the 7-day daily forecast for the same fields, stored every run with its issue
  date. Feeds live predictions. Forecast revisions between issues are genuine drift.
- Live runs take air weather from the forecast, because the archive lags about five days.

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

- **Target:** the researchers' count alone. The park count stays its own series. Estimates are
  included and flagged, so scores can be computed with and without them. Counts written as
  sums are ordinary counts.
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

- **Performance triggers the retrain.** Retrain when the model does worse than persistence over
  the last 10 scored predictions, and once a year at season close.
- **Quality flags gate the training set.** Quarantined observations stay out of training until
  cleared. A flag decides what the model trains on, and performance decides when.

## 5. The quality layer

### Checks

| Domain | Checks |
|---|---|
| **Freshness** | Last successful run per source. Season-aware: the season opens at the first report; no opening after the latest plausible start is an incident; the season closes after five silent weekdays once March begins. Unreported weekends are normal. |
| **Volume** | Rows per run against expectation; duplicate rejection; join retention between counts, weather and gauge by date. |
| **Distribution** | Null rate per column; units against the pinned units. Weather fields: beyond three standard deviations of the 30-year normal for that day of the year. |
| **Counts** | Not judged by standard deviations ([ADR 0001](docs/adr/0001-counts-not-judged-by-standard-deviations.md)). Plausible range, and jumps that contradict the river temperature. Counter disagreement is monitored as its own measure. |
| **Cross-source** | Report temperature against gauge temperature. |

Structural checks catch schema drift. Value-level checks catch the nastier cases: a column that
was always populated arriving null, or a renamed field silently dropping joined rows.

**Join keys:** surrogate keys built from stable identifiers: site, date and counter.

**Thresholds** start as placeholders in `roll_call/config.py` and are calibrated from the
historical seasons once extracted (§8).

### Baselines

- Fixed, refreshed once a year at season close. Never mid-season: a refresh would absorb the
  cold-snap behaviour the checks watch for.
- Weather baseline: the normal for each day of the year over the trailing 30 years.
- Every refresh logs old and new side by side. Without this, a scheduled refresh quietly becomes
  a rolling window.
- Past years are replayed with the live rule and labelled as replayed, so the drift history
  starts where the data does.

### Quarantine, incidents and clearing

Definitions are in `CONTEXT.md`. How they work here:

- The owner clears quarantined observations in a local Jupyter notebook that writes each
  decision and its reason to the database.
- Verify a surprising value against the world before rejecting it. An unusual count may be real.

### Alerting

- Resend (permanent free tier: 3,000 a month, 100 a day). SendGrid retired its free plan in 2025.
- One email per run, only when something new opened, listing new incidents and new quarantined
  observations.

## 6. Dashboard

An [Evidence](https://evidence.dev) site deployed on Vercel. Evidence builds a static site from
SQL and Markdown and runs its queries at build time. Why not Tableau Public:
[ADR 0003](docs/adr/0003-evidence-on-vercel-instead-of-tableau.md).

The daily run's publish step writes the summary CSVs into `dashboard/sources/roll_call/`, and
commits and pushes them with `data/records/` (§2). Vercel rebuilds the site from `dashboard/` on
each push. The files and their columns are defined by `COLUMNS` in
`roll_call/export/dashboard.py` and match the header row of each CSV.

**Write summaries, not raw logs:** metrics are precomputed in Python, and the site only charts
them.

**Public by design:** the repo and the site are public. The CSVs carry dates, source and check
names, numbers and statuses only. Report text, which names people, and clearing reasons stay in
the database, and the database file stays on the owner's machine.

**Staleness:** the site shows when its data was generated and warns when that is more than two
days old. The owner's machine may be off, and the dashboard has to say so.

### Views

1. **Drift over time (cumulative).** Cumulative signed change in each baseline across refreshes,
   including replayed years.
2. **Per-source status.** Last successful run, rows against expectation, null rate against normal.
3. **Quarantine queue.** Open items and time to clear, the metric that proves the loop closes.

The site shows data-quality views only. The counts are Save the Manatee Club's published
fieldwork, so a counts-and-predictions page waits for stage 3, when there are predictions to
show. The owner emails Save the Manatee Club before adding it, and the page credits and links
to their reports.

## 7. Naming

**Roll Call**: what the Blue Spring morning count is called, and what the pipeline does daily.

Rejected: **Manatee Watch**, already the name of Volusia County Environmental Management's
volunteer program, in the same county as Blue Spring.

## 8. Open, pending data

1. How to aggregate the per-season CSVs into one history.
2. Threshold values (see §5).
3. Which features carry over from the attendance model.
