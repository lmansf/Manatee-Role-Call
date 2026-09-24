# Roll Call dashboard

A static site that shows what Roll Call's data-quality layer saw. It is built with
[Evidence](https://github.com/evidence-dev/evidence) and hosted on Vercel.

The site uses Evidence 40, the legacy static-site line of Evidence. The current Evidence
product, Evidence Studio, needs a live warehouse connection, and this site is built from CSV
files in the repo.

## Pages

- Source status (`pages/index.md`) shows when the data was last exported, warns when that
  export is more than two days old, and gives each source's latest status with recent history.
- Baseline drift (`pages/drift.md`) charts the cumulative signed change in each baseline
  across refreshes. A shaded band marks replayed refreshes, and each live refresh has a label.
- Quarantine queue (`pages/quarantine.md`) lists open quarantined observations and how long
  cleared ones stayed open.

Every page shows a plain message when its tables are empty.

## Where the data comes from

After each daily run the pipeline writes four summary CSV files into `sources/roll_call/` and
pushes them to GitHub. Vercel rebuilds the site on each push that changes `dashboard/`.

| File | One row per |
|---|---|
| `meta.csv` | export (generation time and pipeline commit) |
| `source_status.csv` | source per run |
| `baseline_drift.csv` | baseline measure per refresh |
| `quarantine_queue.csv` | quarantined observation |

The committed files hold only their header rows. Real rows arrive with the first pipeline run.

Each CSV has a matching `.sql` file that reads it with fixed column types. Evidence writes no
data file for a table with zero rows, and the build then fails. Each query therefore adds one
placeholder row, and every page removes it with `where not is_placeholder`. Dates stay as text
in these queries and the pages cast them.

The visitor's browser computes the freshness warning from `generated_at_utc`. The warning
keeps working when the pipeline machine is off and no new build happens.

## Run it locally

You need Node 22 and internet access. At build time, locally and on Vercel, DuckDB downloads
its Parquet extension from extensions.duckdb.org.

```bash
cd dashboard
npm ci
npm run sources   # reads the CSVs into .evidence/
npm run dev       # live preview at http://localhost:3000
npm run build     # static site in build/
```

Run `npm run sources` again after the CSV files change.

## Vercel project settings

`vercel.json` holds the build settings, so the only setting made in Vercel's project settings
is the root directory.

| Setting | Value |
|---|---|
| Root Directory | `dashboard` |
| Framework Preset | Other |
| Install Command | `npm ci` |
| Build Command | `npm run sources:strict && npm run build` |
| Output Directory | `build` |
| Ignored Build Step | `git diff --quiet HEAD^ HEAD -- .` |
| Node.js Version | 22.x (from `engines` in `package.json`) |
| Environment variables | none |

The ignored build step skips a build when the pushed commit changed nothing under
`dashboard/`. The strict sources step stops the build when a CSV cannot be read. Vercel then
keeps the last good deployment online.
