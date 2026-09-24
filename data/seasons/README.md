# Per-season extracted counts

`tools/extract_seasons.py` generates one file per Save the Manatee Club season page:
`stmc_2018_2019.csv` through `stmc_2025_2026.csv`. Nobody edits them by hand; re-run the tool
to regenerate them. Aggregating across seasons is a separate, deliberate step.

```
pip install -e ".[dev]"
python tools/extract_seasons.py          # fetches each page once, caches it in data/raw/stmc/
python tools/extract_seasons.py --score  # 2025-26 output vs data/reference, as a trust check
```

If the site refuses scripted requests, save each page from a browser as
`data/raw/stmc/<season>.html` (for example `2023-2024.html`) and add `--offline`.

## Columns

One row per dated entry on the page. The tool reads each date from the entry's lead-in
("January 16", "Monday, Dec. 4", "1/16"). The year comes from the season, with July to
December in the first year, unless the text states one.

| column | meaning |
|---|---|
| `season`, `date` | The season page, such as `2023-2024`, and the entry's date in ISO format. |
| `count_researchers` | Researchers' roll call. An unattributed single count lands here too, since the reports are the researchers' own. |
| `count_park` | Park staff count, when the text attributes a number to the park. |
| `count_other` | Every further candidate number, `\|`-separated. Non-empty means review. |
| `estimate` | `TRUE` when the count is described as an estimate or undercount. |
| `additional` | "N additional/new manatees" figures. Not totals; never in `count_researchers`. |
| `derived` | `TRUE` when the count is written as a sum ("Annie + 23 others"). Not computed. |
| `no_count` | `TRUE` when the text says no roll call happened. A blank count here means absence, not zero. |
| `river_temp_f`, `air_temp_f` | First Fahrenheit figure after "river" / "air temp". |
| `needs_review`, `review_reason` | Why a person should look: multiple candidates, ambiguous attribution, no count found, a date or season mismatch, a date that appears more than once, an implausible value. |
| `source_url`, `entry_text` | Where the entry came from and its text, up to 2,000 characters, so review needs no second trip to the site. |

Rows flagged `needs_review` are extraction doubts, not data-quality verdicts. Resolve them
before the rows go anywhere near the baseline or the model.
