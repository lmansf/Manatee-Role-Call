# A scheduled script on a personal machine, with DuckDB, instead of Databricks

The spec named Databricks Free Edition with Delta tables. Free Edition restricts outbound
internet to an unpublished list of trusted domains, so none of this project's hosts (the report
pages, Open-Meteo, the USGS gauge, Resend, Google Sheets) could be assumed reachable, and going
over the daily compute quota shuts the workspace down for the day. A pipeline whose job is to
notice sources failing would mostly have reported its own platform. It runs instead as a Python
script scheduled on the owner's Ubuntu machine, storing everything in one DuckDB file.

## Considered options

- Databricks Free Edition: rejected for the network allowlist and quota shutdowns above.
- GitHub Actions on a schedule: rejected by the owner.
- An always-on small machine or free cloud VM: deferred; the personal machine is simpler, and
  every run catches up from the last successful run, so a missed run loses at most that day's
  weather forecast.

## Consequences

The machine being off is now a real failure mode. It surfaces through the same freshness checks
as any source going quiet.
