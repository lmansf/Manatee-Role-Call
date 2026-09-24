# Roll Call

A daily pipeline that watches the Blue Spring manatee counts, the weather and the river
temperature, and predicts tomorrow's count. Its subject is the data-quality layer. The
architecture is in `roll-call-spec.md`.

## Working agreement

`TODO(owner)` marks work that waits on something only the owner has: a decision, or real data
such as a saved source response. Leave it in place and say exactly what it needs. Placeholder
thresholds in `roll_call/config.py` and the quality modules wait on the historical seasons.
Calibrate them only from extracted data.

## Before you act

- **Naming anything** (code, columns, docs). Use the terms in `CONTEXT.md`. A word the
  glossary lists under _Avoid_ names a different concept.
- **Changing a settled design** (platform, dashboard, how counts are checked). Read
  `docs/adr/`. A change that contradicts a record needs a new record.
- **Fetching or parsing a source.** `docs/sources.md` has the endpoints, response shapes and
  phrasing traps. Parse against saved responses in `tests/fixtures/`. Tests run offline.
- **Changing a dashboard column.** The contract is `COLUMNS` in
  `roll_call/export/dashboard.py` and the header row of each CSV in
  `dashboard/sources/roll_call/`. Change both in one commit.
- **Committing data.** The repo and the site are public by design. Commit summaries only:
  dates, source and check names, numbers and statuses. The DuckDB file, `.env`, `data/raw/`
  and report text stay on the owner's machine.
- **Touching the timer, the runner clone or publishing.** Read `docs/scheduling.md`.
