# Data sources: calls and site notes

Researched 2026-09-22. The sandbox this was written in cannot reach any of these hosts,
so every call below is documented from official docs and search results, not executed.
Items marked **verify** need one manual call before they are trusted. Run the smoke tests
at the end first.

## 1. Open-Meteo

No API key. Non-commercial fair-use limits: 600 calls/min, 5,000/hour, 10,000/day.
A daily job plus a one-off backfill is nowhere near these.

Response shape is the same on every endpoint. Top-level keys:
`latitude, longitude, elevation, generationtime_ms, utc_offset_seconds, timezone,
timezone_abbreviation, daily_units, daily, hourly_units, hourly`. Each of `daily` and
`hourly` is column-oriented: a `time` array plus one array per variable, positionally
aligned. Missing values are `null`. Persist `daily_units` and `hourly_units` with the raw
payload (spec §3.1).

Defaults to override on every call: `timezone` defaults to GMT; `cell_selection` defaults
to `land` (fine for Blue Spring); `models` defaults to `best_match`.

### 1a. Archive (observations, the only source of observations)

```
GET https://archive-api.open-meteo.com/v1/archive
  ?latitude=28.9478&longitude=-81.3398
  &start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
  &daily=temperature_2m_min,temperature_2m_max,temperature_2m_mean,precipitation_sum,wind_speed_10m_max,shortwave_radiation_sum
  &hourly=temperature_2m
  &timezone=America/New_York
  &temperature_unit=celsius&precipitation_unit=mm&wind_speed_unit=kmh
```

- Data: ERA5 (0.25°, from 1940) and ERA5-Land (0.1°, from 1950), blended under
  `best_match`. Updated daily with a **5-day delay**. Days inside the lag come back as
  `null`, not as missing rows.
- All six daily fields and hourly `temperature_2m` are confirmed present in the archive
  endpoint's variable list (the website source, `historical-weather-api/options.ts`).
  `temperature_2m_mean` is there despite not appearing in the rendered docs table.
- One call can span years. For the backfill, one call per calendar year keeps each
  response small enough to store as a single raw row.
- `hourly.time` values are local ISO strings without an offset because `timezone` is
  set. `utc_offset_seconds` in the response is the offset for the *first* timestamp;
  DST changes inside the window are not reflected per row. Store local timestamps and
  the timezone name, or convert using a proper tz library, not the offset field.

### 1b. Forecast (7 days out, separate table)

```
GET https://api.open-meteo.com/v1/forecast
  ?latitude=28.9478&longitude=-81.3398
  &daily=<same six fields>
  &forecast_days=7
  &timezone=America/New_York
  &temperature_unit=celsius&precipitation_unit=mm&wind_speed_unit=kmh
```

- `forecast_days` default 7, max 16. `past_days` (0 to 92) is deliberately not used.
- `daily.time[0]` is today in the requested timezone. Record `issue_date` as today's
  date in America/New_York at call time, not from the response.

### 1c. Previous Runs API (backfilling forecast revisions)

The drift check on forecast revisions would otherwise need months of stored forecasts
before it has anything to compare. This endpoint serves what the forecast said N days
before each target date, back to January 2024 for most models.

```
GET https://previous-runs-api.open-meteo.com/v1/forecast
  ?latitude=28.9478&longitude=-81.3398
  &start_date=2024-01-01&end_date=YYYY-MM-DD
  &daily=temperature_2m_max_previous_day1,temperature_2m_max_previous_day3,temperature_2m_max_previous_day7
  &timezone=America/New_York&temperature_unit=celsius
```

- Variables take a `_previous_dayN` suffix, N from 0 to 7. **verify** the exact base
  host (the docs page is `open-meteo.com/en/docs/previous-runs-api`) and which daily
  fields accept the suffix; hourly fields definitely do.
- Not needed for stage 1. Noted here so the forecast table schema (issue_date,
  target_date, field, value) is designed to accept rows from this source later.

### 1d. Historical Forecast API (not used)

`https://historical-forecast-api.open-meteo.com/v1/forecast`, archived model output
stitched from the first hours of each run, from 2021/2022. Better day-to-day accuracy
than ERA5 but a shorter history. Spec §3.1 picked the archive for observations; this is
the alternative if ERA5's coarse grid turns out to matter for a spring in a river valley.

## 2. Save the Manatee Club: Blue Spring sighting reports

### What the site is

WordPress (evidence: `savethemanatee.org/?p=4458` resolves to a report page; assets
under `/wp-content/uploads/`). That opens two cleaner routes than scraping rendered HTML.
Both **verify**:

```
GET https://savethemanatee.org/wp-json/wp/v2/pages?slug=manatee-sighting-reports-2024-2025&_fields=id,slug,link,modified,content
GET https://savethemanatee.org/wp-json/wp/v2/posts?search=sightings%20update&per_page=20&_fields=id,slug,link,date,modified,title,content
GET https://savethemanatee.org/feed/
```

If the REST API is open, `content.rendered` is the post body as HTML without the theme
wrapper, and `modified` is a free freshness signal: it changes when a new day's entry is
added to a season page. Many sites disable `wp-json`; if it returns 401/403 or HTML,
fall back to the page itself.

### Where the reports live

Hub page: `https://savethemanatee.org/bssp-manatee-reports/` ("Manatee Sighting Blog").
An older path, `/manatees/manatee-webcams/manatee-reports/`, still appears in search;
treat it as a redirect, not a second source.

The reports are **not one post per day**. They have been organised differently each
season, and the slug has changed every year:

| Season | URL |
|---|---|
| 2018–19 | `/?p=4458` |
| 2021–22 | `/manatee-reports-2021-2022/` |
| 2022–23 | `/bssp-report-2022-2023/` |
| 2023–24 | `/manatee-sighting-reports-2023-2024/` |
| 2024–25 | `/manatee-sighting-reports-2024-2025/` |
| 2025–26 | monthly posts: `/manatee-sightings-update-december-2025/`, `/manatee-sightings-update-february-2026/`, plus `/sightings-update-manatee-season-has-started-at-blue-spring/` |

Two things follow. The scraper must discover season and month pages from the hub rather
than hard-code a slug. And the source has already changed shape once, from one page per
season to one post per month, between the 2024–25 and 2025–26 seasons. A
`Protected: Manatee Sighting Update: December 2025` page also exists alongside the public
one (WordPress password protection), so a post matching the title pattern can still have
no readable body.

Off-season monthly updates exist too (April, June, August). They carry no roll call.
The parser must not treat them as zero-count days.

### How entries are written

Prose, one paragraph or a few per day, dated. Phrasing collected from search snippets
of the 2023–24 and 2024–25 pages. Quote these in the parser tests as the fixture
sentences until real fixtures are saved:

- "The river temp was 70.3°F (21.3°C) with 51 manatees for roll call."
- "River temp was 69.8°F (21°C) with 65 manatees counted."
- "The river temperature was 69.6°F (20.89°C) in the river right at the park."
- "The river temperature was 64.2°F (17.9°C), with researchers counting 186 manatees
  while the park counted 191."
- "the river temperature rose to 60.1°F (15.6°C) with a count of 677 manatees by
  researchers and 687 by the park."
- "With air temps near 46°F (~8°C), researchers decided to do a roll call. The river
  temp was 69.4°F (~20.8°C) with 32 additional manatees counted."
- "Park staff counted 34 manatees one morning while a researcher counted 38."

Parser consequences:

1. **Two counts per day are common.** Save the Manatee Club's researchers and park staff
   count separately and both numbers get reported. Store both (`count_smc`, `count_park`)
   rather than picking one. The gap between them is itself a distribution-check feature.
2. **"Additional" is not a total.** "32 additional manatees" means newly seen animals,
   not the roll call. Treat any count qualified by "additional", "new" or "more" as a
   different field or discard it.
3. Temperatures arrive as °F with °C in parentheses, sometimes with `~`. Parse the °F
   figure and derive °C; use the parenthesised °C only as a cross-check.
4. "River temp", "river temperature" and "the river" are all used. Air temperature
   appears sometimes. Spring temperature rarely, because it is constant.
5. Dates: the format inside a page is not confirmed from snippets. Expect a heading or a
   bold lead-in per day. This is the first thing to look at in a saved fixture.

### A second count source worth checking

Park staff are reported to post the morning count to Blue Spring's own manatee page,
`https://www.floridastateparks.org/parks-and-trails/blue-spring-state-park/manatees-blue-spring-state-park`,
and to social media under `#manateecount`. **verify** whether that page carries a
machine-readable current count. If it does, it is a cleaner primary for `count_park`
and the Save the Manatee Club page becomes the source for `count_smc` and river
temperature. Social posts are not a source: no API, no history.

### Politeness

One request at a time, a short sleep between page fetches, a descriptive User-Agent, and
at most one fetch of each season or month page per day. During the season a single page
changes daily; out of season nothing changes for months, and the freshness check should
know that (spec §5).

## 3. FWC synoptic surveys (optional context)

ArcGIS REST, no key. Aerial statewide counts, 1991 to present, one to three flights per
winter.

```
GET https://gis.myfwc.com/mapping/rest/services/Open_Data/Manatee_Synoptic_Survey_Observation_Locations/MapServer/layers?f=pjson
GET https://gis.myfwc.com/mapping/rest/services/Open_Data/Manatee_Synoptic_Survey_Observation_Locations/MapServer/<layerId>/query
      ?where=1%3D1&outFields=*&returnGeometry=false&f=json&resultOffset=0&resultRecordCount=1000
```

- The `layers` call lists the layer IDs. Search results reference a layer 31, so expect
  many layers, likely one per survey. **verify** the layout before writing a loader.
- Add `returnCountOnly=true` to size a layer before paging. Page with `resultOffset`;
  with no `orderByFields` the service orders by object ID, which is stable.
- Bulk download alternative: the dataset page on `geodata.myfwc.com`
  (`/datasets/myfwc::manatee-synoptic-survey-observation-locations/about`) offers CSV
  and GeoJSON. For a handful of rows per year, one CSV download per season is simpler
  than paging the REST service.
- Statewide totals per survey are published as a table at
  `https://myfwc.com/research/manatee/research/population-monitoring/synoptic-surveys/`.
  That summary, not the point observations, is what the dashboard context view needs.

## 4. Smoke tests

Run these from a machine that can reach the hosts. Each should return HTTP 200 and the
noted shape. Save each response into `tests/fixtures/` as the first fixtures.

```sh
# 1. Archive, short window inside the lag so nulls appear
curl -s "https://archive-api.open-meteo.com/v1/archive?latitude=28.9478&longitude=-81.3398&start_date=$(date -d '-8 days' +%F)&end_date=$(date +%F)&daily=temperature_2m_min,temperature_2m_max,temperature_2m_mean,precipitation_sum,wind_speed_10m_max,shortwave_radiation_sum&hourly=temperature_2m&timezone=America/New_York&temperature_unit=celsius&precipitation_unit=mm&wind_speed_unit=kmh" \
  -o tests/fixtures/open_meteo_archive_sample.json
# expect: daily.time has 9 entries, the last few daily values are null

# 2. Forecast
curl -s "https://api.open-meteo.com/v1/forecast?latitude=28.9478&longitude=-81.3398&daily=temperature_2m_min,temperature_2m_max,temperature_2m_mean,precipitation_sum,wind_speed_10m_max,shortwave_radiation_sum&forecast_days=7&timezone=America/New_York&temperature_unit=celsius&precipitation_unit=mm&wind_speed_unit=kmh" \
  -o tests/fixtures/open_meteo_forecast_sample.json
# expect: daily.time has 7 entries starting today

# 3. WordPress REST (may be disabled)
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" \
  "https://savethemanatee.org/wp-json/wp/v2/pages?slug=manatee-sighting-reports-2024-2025"
# 200 + application/json means the clean route is open

# 4. Hub page and one season page, saved raw
curl -s -A "roll-call-pipeline (portfolio project)" https://savethemanatee.org/bssp-manatee-reports/ \
  -o tests/fixtures/stmc_hub.html
curl -s -A "roll-call-pipeline (portfolio project)" https://savethemanatee.org/manatee-sighting-reports-2024-2025/ \
  -o tests/fixtures/stmc_season_2024_2025.html

# 5. FWC layer listing
curl -s "https://gis.myfwc.com/mapping/rest/services/Open_Data/Manatee_Synoptic_Survey_Observation_Locations/MapServer/layers?f=pjson" \
  | python -c "import json,sys; print([(l['id'], l['name']) for l in json.load(sys.stdin)['layers']])"
```
