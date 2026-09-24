# Fixtures

Source responses the parsers are tested against. Tests never touch the network.

Every fixture here is currently synthetic. No source was reachable from the environment that
wrote the parsers, so each file was built from the documented response shape and real report
sentences (docs/sources.md). Replace each one with a saved real response, using the smoke tests
in docs/sources.md section 5, and keep the tests passing.

| File | Stands in for |
|---|---|
| `open_meteo_archive_sample.json` | an archive response with daily and hourly blocks and two trailing null days |
| `open_meteo_forecast_sample.json` | a 7-day daily forecast |
| `stmc_hub_synthetic.html` | the reports hub page holding the current season |
| `stmc_season_2023_2024_synthetic.html` | an archived season page |
| `usgs_daily_synthetic_page1.json`, `usgs_daily_synthetic_page2.json` | two pages of daily gauge temperature linked by `next` |

Save a real response as-is, with no hand edits. When a live format changes, save the new shape as
a new fixture and keep the old one. The old fixture then tests that the parser still reads
history.
