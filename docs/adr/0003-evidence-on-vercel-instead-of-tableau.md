# An Evidence site on Vercel instead of Tableau Public fed by Google Sheets

The spec named a Tableau Public dashboard reading a Google Sheet. Tableau Public refreshes on
its own only from Google Sheets, once a day. Tableau deprecated that connector in 2023 in
favour of a Google Drive connector. Whether the daily sync still works through Drive on Tableau
Public could not be confirmed. Tableau's desktop app has no Linux build, so the owner would
author in the browser editor, which is confirmed only for CSV and Excel uploads. The dashboard
could end up as a snapshot republished by hand. Instead, the daily run exports summary CSVs into
the repo and pushes them. Vercel rebuilds an Evidence site from them on every push. Evidence
runs its SQL at build time on DuckDB, the same engine the pipeline uses. Vercel's free Hobby
plan allows 100 deployments a day for personal, non-commercial use. Its GitHub integration
builds on Vercel's side, so this adds no GitHub Actions.

## Considered options

- Tableau Public with Google Sheets: rejected for the uncertain refresh path and the missing
  Linux editor above. It also needed a Google service account and a sheet to share with it.
- A hand-built React dashboard: rejected because the charts, the build and the hosting would all
  be code to maintain for three views.

## Consequences

- The repo and the site are public, so the exported CSVs carry only dates, source and check
  names, numbers and statuses. Report text and names never go there, and the DuckDB file is
  never committed.
- The timer runs from a separate runner clone kept on `main` and never used for editing, so the
  publish step can commit and push without touching work in progress.
- Pushing uses an SSH deploy key with write access to this one repo, not a personal token.
- `main` gets about one data commit a day.
- The site shows when its data was generated and warns when that is more than two days old,
  because the owner's machine may be off.
