# Reference data

Hand-transcribed sets used to score the pipeline, never fed into it. Nothing here is a
source: no freshness check watches these files, and no row reaches the training set.

## `blue_spring_manatee_counts_2025_2026.csv`

The 2025-26 season's daily roll call, read by hand from Save the Manatee Club's monthly
sighting-update posts (the first season published in that format; see `docs/sources.md`
§2). 114 dated rows from 2025-10-31 to 2026-03-26.

| column | meaning |
|---|---|
| `date` | Morning of the count, ISO format. Gaps are days with no report, mostly weekends. |
| `count` | Number reported. Integer. Zero is a real zero (late March). |
| `count_source` | Who produced the number: `SMC roll call` (researchers' count, 107 rows), `SMC estimate` (researchers gave an estimate, not a count, 3 rows), `Park staff` (no researcher count that day; park's number used, 4 rows). |
| `late_arrivals` | Animals noted arriving after roll call, when the post mentions them. Not included in `count`. |
| `derived` | `TRUE` when `count` was computed from the prose (for example "Annie + 23 others") rather than stated as a single figure. 8 rows. |
| `notes` | Free text: the park's count when both were given, undercount caveats, how a derived figure was reached. |

Uses:

- **Parser scoring.** Once the parser reads the 2025-26 season page, the extracted
  `count_researchers` and `count_park` should reproduce this table.
  `python tools/extract_seasons.py --score` compares `data/seasons/stmc_2025_2026.csv` with
  it. A disagreement is a parser bug or a transcription error; either way, record which.
- **Test days worth targeting.** 2025-11-01 (birth after roll call, derived count),
  2025-12-13 (SMC 452 vs park 670, both stated), 2026-01-16 (park-only count), 2026-02-02
  (season high, 834), 2026-03-09 (first true zero), 2026-03-05 (late arrivals noted).
- **The end-to-end test.** `tests/test_end_to_end.py` takes its season's counts from this
  file, so the season opens, peaks and closes on real dates.

Where the number came from matters as much as the number. `count_source` and `derived`
exist so that a mismatch with the parser can be attributed before anyone calls it an error.
