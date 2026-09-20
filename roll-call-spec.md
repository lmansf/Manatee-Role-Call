# Roll Call — project spec

A data pipeline whose real subject is the **data-quality layer**. The manatee forecast
is the excuse; the point of the project is catching a source that changes underneath you.

---

## Kickoff prompt (paste this to the coding agent)

> I'm building a portfolio data project called **Roll Call**. It's a Python pipeline on
> Databricks (free edition, Delta tables) that predicts daily manatee counts at Blue Spring
> State Park from weather data — but the real focus is the data-quality and observability
> layer wrapped around it: freshness, volume and distribution checks, a quarantine table for
> anomalies, and a Tableau Public dashboard fed via Google Sheets.
>
> The full spec is in `roll-call-spec.md`. Read it, then help me build it in stages, starting
> with the ingestion layer. I want to write the code myself where it's instructive — give me
> the structure and let me fill in the logic, and flag the parts where you think I'd learn
> more by hitting the problem first. Two design decisions are still open (marked in the spec)
> — raise them when we get there rather than picking for me.

---

## 1. Purpose

Most portfolio pipelines show that data *moved*. This one shows that the data was *watched*:
what the pipeline did when a source drifted, went stale, or started lying.

Deliverables: a GitHub repo, a Tableau Public dashboard, and a short written walkthrough.

## 2. Platform

- **Compute/storage:** Databricks free edition, Delta tables.
  Free-tier compute is time-limited — keep runs small.
- **Language:** Python.
- **Dashboard:** Tableau Public (free).
- **Orchestration:** scheduled job. (Dagster is queued as a separate learning series and is
  explicitly *not* part of this build.)

## 3. Data sources

### 3.1 Open-Meteo (primary weather source)
- Free, no API key, global coverage (~11 km, finer where regional models exist).
- Blends ECMWF, GFS, ICON and others.
- Historical reanalysis goes back decades — **use it to backfill the baseline** rather than
  waiting months to accumulate one.
- **Open question:** whether to pull forecasts as well as observations. Forecasts get
  *revised* before the target date, which is genuine, un-manufactured drift — good material
  for the distribution checks.

### 3.2 Blue Spring manatee counts (the modelling target)
- Park staff run a daily count each morning during manatee season (~November to mid-March).
  Counts range from a few dozen to 700+.
- Published as **sighting reports on Save the Manatee Club's site — HTML blog posts, no API.**
  This is deliberate: a scraped page is exactly the kind of source that changes format
  silently, which is what the quality layer exists to catch.
- Reports frequently include **river temperature** alongside the count, so part of the join
  arrives in the same record.
- The season has a hard on/off, which makes it good material for freshness and volume checks.

### 3.3 FWC synoptic surveys (optional statewide layer)
- Available as CSV with a GeoServices/WMS/WFS API via `geodata.myfwc.com`.
- Only a handful of aerial flights per winter — **too sparse to model against.** Use as
  context or a statewide baseline only.

### 3.4 Synthetic mutation generator (test fixture)
- Generates data with **built-in variable mutation**: schema drift, null spikes, unit changes,
  duplicates, renamed keys.
- Points at *copies*, never the real tables. Its job is to prove every check actually fires.
- Accepted trade-off: it's a closed loop (you wrote the bug and the detector), which is why
  it sits alongside a real source rather than replacing one.
- **Open question:** does it run as a test suite on every deploy, or as a script pointed at
  things on demand?

## 4. The model

Predict daily manatee count at Blue Spring from weather.

- Manatees aggregate at warm-water refuges when ambient water drops below roughly 20°C —
  a genuine **threshold effect** rather than a smooth relationship. Worth modelling explicitly
  (e.g. degree-hours below threshold, consecutive cold days) rather than raw daily temperature.
- Note: Blue Spring is a **natural spring at a constant ~72°F**, not a power plant outflow.
  Same physics, different story — don't describe it as industrial warm water.
- Reuse the lag/calendar feature approach from the existing attendance model where it fits.

### Retraining
- **Trigger on model performance, not on data-quality flags.** Error on recent predictions
  exceeding a threshold means the world changed and the model should follow.
- Quality flags must *not* trigger retraining — that would point the model at exactly the
  data just quarantined, and retrain on the noisiest days by design. Flags **gate the
  training set**; performance **triggers the retrain**.
- **Feature selection stays manual.** Automated selection on every run means the feature set
  changes for reasons that can't be reconstructed later, which makes the performance history
  unreadable.

## 5. The quality layer

Three domains:

| Domain | Checks |
|---|---|
| **Freshness** | Last successful run per source; expected-vs-actual arrival; season-aware (Blue Spring goes quiet Mar–Nov by design, not by failure). |
| **Volume** | Row counts per run vs baseline; duplicate-match rejection; join row-count retention. |
| **Distribution** | Null rate per column; value ranges; mean/spread vs baseline; unit-change detection. |

Structural checks (column set vs expected) catch schema drift. They do **not** catch the
nastier cases — a renamed club/site silently dropping joined rows, or a column that was always
populated starting to arrive null — which is what the value-level checks above are for.

**Join keys:** use surrogate keys on stable identifiers, never on display names.

### Baselines
- **Fixed baseline, refreshed on a schedule** — not a rolling window. A rolling window absorbs
  slow drift until it silently becomes the new normal.
- At every refresh, **log old and new baseline side by side.** Without this, a scheduled
  refresh quietly reinvents the rolling window — the drift just gets absorbed in steps.
- Backfill the initial baseline from Open-Meteo reanalysis.

### Flagging and quarantine
- Anomalies → **quarantine/review table**. Never silently dropped, never auto-deleted.
- Quarantined rows are **held out of model training until a human clears or confirms them**.
- Rule of thumb for the threshold: values beyond ~3 SD from that source's baseline.
- A big number is not automatically bad data — verify against the world before calling it
  wrong (an unusual count may be real). Record the decision either way.

### Alerting
- Email on every flag.
- **SendGrid** if its free tier still covers the volume — the free allowance shrank a while
  back, so check current limits. **Amazon SES** is the fallback.

## 6. Dashboard

**Tableau Public cannot connect to databases or warehouses** — only flat files, Google Sheets,
JSON, spatial files and web data connectors. That's a security limit (everything saved to
Tableau Public is public), and it also rules out the `.taco` connector route, which needs
paid Desktop.

**The path:**
1. The Databricks job computes check summaries in Python.
2. It writes them to **Google Sheets** via `gspread` with a service account (share the sheet
   with the service account's email like a normal collaborator).
3. Tableau Public connects to the Sheet and refreshes on a schedule.

**Write summaries, not raw logs.** One row per source per run, metrics already computed.
Keeps the sheet small and stops Tableau doing the aggregation.

**Tableau Public is public — keep anything identifying out of those rows.**

### Views
1. **Drift over time (cumulative).** Prefer *cumulative signed change in the baseline* over a
   simple count of drift events — that's what exposes a source that has crept 20% over six
   months while no single refresh looked alarming.
2. **Per-source status.** Last successful run, row count vs norm, null rate vs norm.
3. **Quarantine queue.** Open items and time-to-clear — the metric that proves the loop closes.

## 7. Naming

**Roll Call** — what the Blue Spring morning count is actually called, and what the pipeline
does daily: check who's there, flag what's missing.

Rejected: **Manatee Watch** — already the name of Volusia County Environmental Management's
volunteer program (running since 2005), in the same county as Blue Spring. Too easy to
mistake for an official project.

## 8. Open questions

1. Where the mutation generator lives — deploy-time test suite, or on-demand script.
2. Which Open-Meteo fields to pull, and whether to include revisable forecasts.
