# Fixtures

Saved copies of real source responses. Parsers are tested against these, never against
the live site.

- `open_meteo_archive_sample.json`: one archive response, a short window, including at least
  one trailing day the archive hadn't filled yet (nulls).
- `blue_spring_report_*.html`: two or three sighting reports from different months, plus one
  "no count today" post if you can find one.

Add them by saving the raw response; don't hand-edit. When the live page format changes,
save the new shape as a new fixture and keep the old one. The old fixture is the regression
test for "the parser still reads history".
