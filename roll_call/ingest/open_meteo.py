"""Open-Meteo weather ingestion.

Two endpoints matter:
  - Archive (ERA5 reanalysis, decades of history, ~5 day lag):
      https://archive-api.open-meteo.com/v1/archive
  - Forecast (also serves recent past days via `past_days`):
      https://api.open-meteo.com/v1/forecast

No API key. Response shape for daily variables:
    {"latitude": ..., "daily_units": {"time": "iso8601", "temperature_2m_max": "°C", ...},
     "daily": {"time": ["2024-01-01", ...], "temperature_2m_max": [18.2, ...], ...}}

Note `daily_units`. It is worth persisting: if you ever pass `temperature_unit=fahrenheit`
by accident, or the API changes a default, the unit-change check needs this to notice.

OPEN QUESTION (spec §8.2): which fields, and whether to pull forecasts as well as
observations. Not decided here. `DAILY_FIELDS` below is a placeholder to make the
archive call work; revisit before the baseline backfill.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import requests

from roll_call import config
from roll_call.ingest.base import IngestResult, IngestRun, sha256_text

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Placeholder until open question 2 is settled. Candidates worth looking at:
#   temperature_2m_max / _min / _mean, apparent_temperature_*, precipitation_sum,
#   wind_speed_10m_max, shortwave_radiation_sum, daylight_duration.
# The threshold physics in spec §4 argues for hourly temperature_2m too (degree-hours
# below 20°C can't be computed from a daily min/max). That's a separate decision.
DAILY_FIELDS: tuple[str, ...] = (
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
)

SOURCE_NAME = "open_meteo_archive"


def build_archive_params(start: date, end: date) -> dict[str, Any]:
    """Query params for one archive call covering [start, end] inclusive."""
    return {
        "latitude": config.BLUE_SPRING_LAT,
        "longitude": config.BLUE_SPRING_LON,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(DAILY_FIELDS),
        "timezone": config.TIMEZONE,
    }


def fetch_archive(start: date, end: date, session: requests.Session | None = None) -> IngestResult:
    """Fetch daily reanalysis for the window and parse it into one record per day.

    Keeps the raw response text on the result. Records the run either way.
    """
    run = IngestRun(source=SOURCE_NAME, source_url=ARCHIVE_URL)
    sess = session or requests.Session()
    try:
        resp = sess.get(ARCHIVE_URL, params=build_archive_params(start, end), timeout=30)
        resp.raise_for_status()
        raw = resp.text
        run.payload_sha256 = sha256_text(raw)
        records = parse_daily(resp.json())
        run.succeed(len(records))
        return IngestResult(run=run, raw=raw, records=records)
    except Exception as exc:  # noqa: BLE001 - the run record is the error channel
        run.fail(exc)
        raise


def parse_daily(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn Open-Meteo's column-oriented `daily` block into row-oriented records.

    Each record should carry: obs_date (date), one key per field in DAILY_FIELDS, and the
    unit for each field from `daily_units` (or a single `units` dict, your call).

    TODO(you): implement. Things to decide as you go:
      - Open-Meteo returns null for days it has no data (the archive lags ~5 days).
        Do you keep those rows with nulls, or drop them? The null-rate check in stage 2
        behaves very differently depending on this answer.
      - The arrays are positionally aligned. Assert they're the same length before zipping;
        a silent mismatch here is exactly the kind of thing this project is about.
    """
    raise NotImplementedError
