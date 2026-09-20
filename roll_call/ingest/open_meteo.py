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

Field set and forecast policy are decided in spec §3.1. Summary: six daily fields plus
hourly temperature from the archive (observations); the same daily fields from the
forecast endpoint, 7 days out, into a separate table keyed by issue date. Observations
never come from the forecast endpoint's `past_days`.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import requests

from roll_call import config
from roll_call.ingest.base import IngestResult, IngestRun, sha256_text

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Spec §3.1. Changing this set means re-backfilling the baseline; do it on purpose.
DAILY_FIELDS: tuple[str, ...] = (
    "temperature_2m_min",
    "temperature_2m_max",
    "temperature_2m_mean",
    "precipitation_sum",
    "wind_speed_10m_max",
    "shortwave_radiation_sum",
)
HOURLY_FIELDS: tuple[str, ...] = ("temperature_2m",)

# Pinned so an upstream default change shows up as a unit mismatch, not a silent shift.
UNIT_PARAMS: dict[str, str] = {
    "temperature_unit": "celsius",
    "precipitation_unit": "mm",
    "wind_speed_unit": "kmh",
}

FORECAST_DAYS = 7

SOURCE_NAME = "open_meteo_archive"
FORECAST_SOURCE_NAME = "open_meteo_forecast"


def _base_params() -> dict[str, Any]:
    return {
        "latitude": config.BLUE_SPRING_LAT,
        "longitude": config.BLUE_SPRING_LON,
        "daily": ",".join(DAILY_FIELDS),
        "timezone": config.TIMEZONE,
        **UNIT_PARAMS,
    }


def build_archive_params(start: date, end: date) -> dict[str, Any]:
    """Query params for one archive call covering [start, end] inclusive.
    Requests both daily and hourly blocks in one call."""
    return {
        **_base_params(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(HOURLY_FIELDS),
    }


def build_forecast_params() -> dict[str, Any]:
    """Query params for the 7-day daily forecast. No `past_days`: observations come
    from the archive only (spec §3.1)."""
    return {**_base_params(), "forecast_days": FORECAST_DAYS}


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


def fetch_forecast(session: requests.Session | None = None) -> IngestResult:
    """Fetch today's 7-day daily forecast. Records are keyed by (issue_date, target_date).

    TODO(you): mirror fetch_archive. `issue_date` is today's date in config.TIMEZONE, not
    UTC; a run just after midnight UTC is still "yesterday" in Florida. parse_daily can be
    reused if you let it take the issue date as an extra key, or write a sibling.
    """
    raise NotImplementedError


def parse_hourly(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Row-orient the `hourly` block: one record per hour with obs_ts and temperature_2m.

    TODO(you): same shape as parse_daily. Timestamps arrive as local ISO strings without
    an offset because of the `timezone` param; decide whether to store them naive-local
    or convert to UTC. Whatever you pick, the daily-mean cross-check against the API's
    temperature_2m_mean has to group by local day, or it will be off at the edges.
    """
    raise NotImplementedError


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
