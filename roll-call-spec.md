# Roll Call: project spec

A data pipeline whose subject is the data-quality layer. The manatee prediction is the reason
to run it. The project is about catching a source that changes without notice.

Terms mean exactly what [`CONTEXT.md`](CONTEXT.md) says. The reasons behind hard-to-reverse
choices are in [`docs/adr/`](docs/adr/). Endpoints and scraping notes are in
[`docs/sources.md`](docs/sources.md).

## 1. Purpose

Most portfolio pipelines show that data moved. This one also shows that the data was watched,
and records what the pipeline did when a source drifted, went stale or sent wrong values.

The deliverables are a GitHub repo, an Evidence dashboard site on Vercel, and a short written
walkthrough.

### Flow

One daily run, `jobs/ingest_daily.py`, runs these stages in order:

1. **Ingest.** The run fetches each source from its last successful run onward, with an
   overlap that re-reads late or revised data (§2, catch-up). It stores the raw payload and the
   parsed rows in DuckDB, and logs every fetch, failures included.
2. **Check.** The checks compare the stored data with its expectations and baselines (§5). A
   check that doubts one observation quarantines it. A failed source-level check opens an
   incident.
3. **Alert.** One email lists everything that newly opened (§5). No email goes out when
   nothing did.
4. **Refresh baselines.** After the season closes, the weather baselines refresh once (§5).
5. **Retrain.** The model retrains when none exists yet, when it has lost to persistence, or
   once after the season closes (§4).
6. **Predict.** In an open season, the model predicts tomorrow's count from the counts that
   pass the quarantine gate (§4).
7. **Publish.** The run exports the summary CSVs, commits them and pushes. Vercel rebuilds the
   site (§6).

Each stage runs even when an earlier one failed. If any stage failed, the run exits non-zero,
so systemd marks it failed and retries it. `--today YYYY-MM-DD` runs it as of a past day.

## 2. Platform

See [ADR 0002](docs/adr/0002-local-scheduled-script-with-duckdb.md).

- **Machine.** The pipeline runs as a plain Python script on the owner's Ubuntu machine, from a
  runner clone of the repo at `~/roll-call-runner`. The runner clone stays on `main` and only
  the timers use it.
- **Schedule.** A systemd user timer starts the run at 19:00 local time. It is set to
  `Persistent=true`, so a run missed while the machine was off or asleep fires when the
  machine is back. The setup is in [`docs/scheduling.md`](docs/scheduling.md).
- **Catch-up.** Every run fetches each source from its last successful run onward, minus a
  per-source overlap, and not just yesterday. The overlaps are in `jobs/ingest_daily.py`. A
  missed run therefore costs at most that day's weather forecast.
- **Storage.** Everything is in one DuckDB file. Every table a run writes is keyed, so
  re-running is safe.
- **Publish.** The run's last step, `jobs/publish_dashboard.py`, exports the dashboard CSVs,
  commits them and pushes to `main` (§6).
- **Push access.** Pushing uses a fine-grained personal access token limited to this repo,
  with read and write access to contents. It expires and gets renewed. The setup is in
  [`docs/scheduling.md`](docs/scheduling.md).
- **Dashboard hosting.** Vercel rebuilds the Evidence site on every push to `main` that
  changes `dashboard/` ([ADR 0003](docs/adr/0003-evidence-on-vercel-instead-of-tableau.md)).
- **Secrets.** Secrets live in a `.env` file in the repo folder, ignored by git.
  `.env.example` lists the keys. The push token lives in git's credential store
  (`~/.git-credentials`), outside the repo.
- **Backup.** A second timer runs `jobs/backup_db.py` weekly. It exports the clearing decisions
  and the baseline refresh log as CSV to `data/records/`, and the next publish step commits
  them. They are the history no source can give back. The same job copies the whole database
  file to a second disk.
- **Not used.** The project does not use Databricks or GitHub Actions
  ([ADR 0002](docs/adr/0002-local-scheduled-script-with-duckdb.md)), Tableau Public
  ([ADR 0003](docs/adr/0003-evidence-on-vercel-instead-of-tableau.md)), or Dagster, which the
  owner is learning in a separate series.

## 3. Data sources

### 3.1 Save the Manatee Club reports (the counts)

- Save the Manatee Club publishes the researchers' daily roll call during the season as prose
  on season pages. The current season stays on the hub page until it is archived. There is no
  API, so the pipeline scrapes the pages. That is deliberate, because a scraped page is the
  source most likely to change without warning.
- The daily run reads the hub page (`roll_call/ingest/blue_spring.py`).
- Each report can give two counts, the researchers' and the park's, plus the report
  temperature.
- `tools/extract_seasons.py` extracts the historical seasons, 2018-19 onward, once, into one
  CSV per season. A person reviews them before they are aggregated. The aggregation rule is
  still open (§8).
- A hand-transcribed 2025-26 set in `data/reference/` scores the extraction and feeds nothing
  else.

### 3.2 Open-Meteo (weather)

- **Archive.** The archive is ERA5 reanalysis, with about a five-day lag. It is the only
  source of observed air weather, and it feeds the training features and the weather
  baselines. It has six daily fields plus hourly temperature, with units pinned (see
  `docs/sources.md` §1).
- **Forecast.** The forecast is the 7-day daily forecast for the same fields, stored every run
  with its issue date. It feeds live predictions. Forecast revisions between issues are real drift.
- Live runs take air weather from the forecast, because the archive lags about five days.

### 3.3 USGS gauge 02236000, St. Johns River near DeLand (river temperature)

- The gauge records water temperature continuously about 7 km downstream of the park,
  weekends included, and has years of history. Its daily mean is the model's river
  temperature input.
- The report temperature and the gauge temperature cross-check each other (§5).
- USGS publishes values as provisional and approves them later. The key includes the approval
  status, so the approved value is stored beside the provisional one instead of overwriting
  it, as forecast revisions are.

### 3.4 Synthetic mutation generator (test fixture)

- `tools/mutate.py` is a standalone script, run on demand and never scheduled. It mutates a
  copy of the database file. It refuses the live database and anything in the backup folder.
  `--copy-from` makes the copy first and only reads the file it copies.
- It has five mutations: schema drift (drop or add a column), null spikes, unit changes (°C to
  °F by default, with the unit labels left as they were), duplicate rows, and renamed key
  columns. A table with a key would reject duplicates, so that mutation first replaces the
  table with a keyless copy. A seed makes each run reproducible, and each mutation prints one
  line saying what it changed.
- Its job is to prove every check fires. After a mutation, run the checks on the copy. Each
  mutation should show up as an incident or a quarantined observation. No test automates that
  comparison yet.
- It is a closed loop, an accepted trade-off. The real sources run beside it for that reason.

### 3.5 Out of the first version

- Florida's statewide aerial surveys (FWC) fly one to three times a winter. They would give
  context only, and add a fourth source to watch with no benefit to the model. The endpoints
  are kept in `docs/sources.md`.

## 4. The model

- **Target.** The target is the researchers' count alone. The park count stays its own
  series. Training includes estimates and flags them, so scores can be computed with and
  without them. Counts written as sums are ordinary counts.
- **Prediction.** The model predicts tomorrow's count every calendar day of an open season.
  A prediction is scored only if its day turns out counted. There is no prediction before the
  first model is trained, or when an input is missing (`roll_call/model/predict.py`).
- **Model.** The model is a negative binomial regression with a log link over hand-picked
  features. It takes its dispersion from a Poisson fit, and keeps the Poisson fit when the
  negative binomial fit fails (`roll_call/model/train.py`). Feature selection stays manual so
  the performance history stays readable. Each model version stores its parameters as JSON, so
  any past prediction can be recomputed from the database.
- **Features.** `roll_call/model/features.py` defines them: the last count and the days since
  it, the gauge temperature and its recent change, degree-hours of the river below 20°C, and
  the forecast air temperature for the target day with degree-days below 20°C. Which lag and
  calendar features carry over from the owner's attendance model is still open (§8).
- **Physics note.** Blue Spring is a natural spring at a constant ~72°F. Manatees gather when
  the river drops below roughly 20°C. The degree-hour and degree-day features model that
  threshold effect.
- **Training data and live inputs.** Training uses archive air weather in place of forecasts.
  The size of that mismatch is measurable from forecast history since 2024.

### Retraining

- **Performance triggers the retrain.** The model retrains when it has done worse than
  persistence over its most recent scored predictions, and once after each season closes. The
  first model trains as soon as there are enough training rows. The window and the minimum
  are in `roll_call/model/train.py`, and each model version records why it was trained.
- **Quality flags gate the training set.** A quarantined count stays out of training and
  scoring while it is open or rejected, and goes back in once it is confirmed
  (`roll_call/quality/gate.py`). A flag decides what the model trains on, and performance
  decides when. The gate covers counts only. Quarantined weather values still reach the
  training features.

## 5. The quality layer

### Checks

`roll_call/quality/checks.py` runs them all on each daily run.

| Domain | Checks |
|---|---|
| Freshness | Days since each source's last successful run. Counts are judged in weekdays, and only while the season is open or overdue; unreported weekends are normal. The season opens at the first report. No opening by the latest plausible start is an incident. The season closes after a run of silent weekdays once March begins (`roll_call/quality/season.py`). |
| Volume | Rows from each source's latest run against an expected range. Counts have no expected range, because most runs find zero or one new report. Duplicate keys in each parsed table. |
| Schema | A check whose query fails on a missing or renamed column opens a schema incident for that source. The other checks still run. |
| Distribution | Null rate per column. Units against the pinned units, for weather and the gauge. An archive weather value beyond three standard deviations of the normal for that day of the year is quarantined. |
| Counts | Not judged by standard deviations ([ADR 0001](docs/adr/0001-counts-not-judged-by-standard-deviations.md)). A count outside the plausible range, or a jump that contradicts the river temperature, is quarantined. Counter disagreement is computed on each run as its own measure. It never quarantines and is not stored yet. |
| Cross-source | Report temperature against gauge temperature. When their difference moves too far from its trailing median, the check opens an incident on the gauge source. It quarantines nothing. |

Join retention between counts, weather and gauge by date is not checked yet.

A missing or renamed column opens a schema incident. The null-rate checks catch a column that
was always populated arriving null, which no query error would show.

**Keys.** Every parsed table has a primary key on its natural key, such as the report date for
counts, or the issue and target dates for the forecast (`roll_call/storage/db.py`). Both
counters share one row per report date.

**Thresholds.** Every threshold is a placeholder, in `roll_call/config.py` and the quality
modules, until it is calibrated from the extracted historical seasons (§8).

### Baselines

- Baselines are fixed and refreshed once a year, after the season closes. They never refresh
  mid-season, because a refresh then would absorb the cold snaps the checks watch for.
- Only weather has baselines. The weather baseline is the normal for each day of the year over
  the trailing 30 years, smoothed across neighbouring days (`roll_call/quality/baselines.py`).
- Every refresh logs each measure's old and new values side by side. Without this log, a
  scheduled refresh turns into a rolling window unnoticed.
- A replayed refresh applies the live rule to a past year, using only data from before that
  year's refresh date, and is labelled as replayed. The drift history then starts where the
  data does. `jobs/replay_baselines.py` runs them once, after the weather backfill.

### Quarantine, incidents and clearing

Definitions are in `CONTEXT.md`. How they work here:

- An incident opens once per failed check and source, and closes on the first run where that
  check passes.
- A quarantined observation's id is `<table>:<key>`. A count's id is
  `blue_spring_counts_daily:<date>`. A weather value's id also names the measure,
  `weather_daily:<date>:<measure>`, because the check doubts one measure, not the whole day.
- The owner clears quarantined observations in a local Jupyter notebook,
  `notebooks/clearing.ipynb`, that writes each decision and its reason to the database.
- Verify a surprising value against the world before rejecting it. An unusual count may be
  real.

### Alerting

- Alerts go through Resend, whose permanent free tier allows 3,000 emails a month and 100 a
  day. SendGrid retired its free plan in 2025.
- Each run sends at most one email, and only when something new opened. It lists new
  incidents and new quarantined observations.

## 6. Dashboard

The dashboard is an [Evidence](https://evidence.dev) site deployed on Vercel. It uses
Evidence 40, which builds a static site from SQL and Markdown and runs its queries at build
time. `dashboard/README.md` says why it uses that version, and
[ADR 0003](docs/adr/0003-evidence-on-vercel-instead-of-tableau.md) says why it is not Tableau
Public.

The daily run's publish step writes the summary CSVs into `dashboard/sources/roll_call/`, and
commits and pushes them with `data/records/` (§2). Vercel rebuilds the site from `dashboard/`
on each push that changes it. `COLUMNS` in `roll_call/export/dashboard.py` defines the files
and their columns, and they match the header row of each CSV.

**Summaries, not raw logs.** Python computes the metrics, and the site only charts them.

**Public by design.** The repo and the site are public. The CSVs carry dates, source and check
names, numbers and statuses only. Report text, which names people, and clearing reasons stay in
the database, and the database file stays on the owner's machine.

**Staleness.** The site shows when its data was generated and warns when that is more than two
days old. The owner's machine may be off, and the dashboard has to say so.

### Views

1. **Drift over time (cumulative).** Cumulative signed change in each baseline across refreshes,
   including replayed years.
2. **Per-source status.** Last successful run, rows against expectation, null rate against
   normal.
3. **Quarantine queue.** Open items and time to clear, the metric that proves the loop closes.

The site shows data-quality views only. The counts are Save the Manatee Club's published
fieldwork. Stage 3 now makes predictions, but a counts-and-predictions page is not built. The
owner emails Save the Manatee Club before adding it, and the page credits and links to their
reports.

## 7. Naming

Roll Call is what the Blue Spring morning count is called, and what the pipeline does daily.

Manatee Watch was rejected. It is already the name of Volusia County Environmental
Management's volunteer program, in the same county as Blue Spring.

## 8. Open, pending data

1. Saved real responses from each source, to replace the synthetic fixtures in
   `tests/fixtures/`.
2. The historical season extraction: run `tools/extract_seasons.py` and review the rows it
   flags.
3. How to aggregate the per-season CSVs into one history.
4. Threshold values, calibrated from the extracted seasons (see §5).
5. Which features carry over from the attendance model.
