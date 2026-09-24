# Roll Call dashboard

A static site that shows what Roll Call's data-quality layer saw. It is built with
[Evidence](https://github.com/evidence-dev/evidence) and hosted on Vercel.

## Pages

- **Source status** (`pages/index.md`): when the data was last exported, a warning when that
  export is more than two days old, and the latest status of each source with recent history.
- **Baseline drift** (`pages/drift.md`): the cumulative signed change in each baseline across
  refreshes. A shaded band marks replayed refreshes, and each live refresh has a label.
- **Quarantine queue** (`pages/quarantine.md`): open quarantined observations and how long
  cleared ones stayed open.

Every page shows a plain message when its tables are empty.

## Where the data comes from

After each daily run the pipeline writes four summary CSV files into `sources/roll_call/` and
pushes them to GitHub. Vercel rebuilds the site on every push.

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
in these queries and are cast on the pages.

The freshness warning is computed in the visitor's browser from `generated_at_utc`. It keeps
working when the pipeline machine is off and no new build happens.

## Run it locally

Needs Node 22 and internet access (DuckDB downloads its Parquet extension during the build).

```bash
cd dashboard
npm ci
npm run sources   # reads the CSVs into .evidence/
npm run dev       # live preview at http://localhost:3000
npm run build     # static site in build/
```

Run `npm run sources` again after the CSV files change.

## Vercel project settings

`vercel.json` holds the build settings, so only the root directory is set in the dashboard.

| Setting | Value |
|---|---|
| Root Directory | `dashboard` |
| Framework Preset | Other |
| Install Command | `npm ci` |
| Build Command | `npm run sources:strict && npm run build` |
| Output Directory | `build` |
| Node.js Version | 22.x (from `engines` in `package.json`) |
| Environment variables | none |

The strict sources step stops the build when a CSV cannot be read. Vercel then keeps the last
good deployment online.

Evidence 40 is the static-site version of Evidence. Its current product, Evidence Studio, needs
a live database connection, so it does not fit a site built from CSV files.
