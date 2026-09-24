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

import json
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

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


def fetch_forecast(
    issue_date: date | None = None, session: requests.Session | None = None
) -> IngestResult:
    """Fetch the 7-day daily forecast. Records are keyed by (issue_date, target_date).

    `issue_date` defaults to today in config.TIMEZONE, not UTC. A run just after midnight
    UTC is still the previous day in Florida. The response's first day is not used for it.
    Keeps the raw response text on the result. Records the run either way.
    """
    run = IngestRun(source=FORECAST_SOURCE_NAME, source_url=FORECAST_URL)
    sess = session or requests.Session()
    try:
        issued = issue_date or local_today()
        resp = sess.get(FORECAST_URL, params=build_forecast_params(), timeout=30)
        resp.raise_for_status()
        raw = resp.text
        run.payload_sha256 = sha256_text(raw)
        records = parse_forecast(resp.json(), issued)
        run.succeed(len(records))
        return IngestResult(run=run, raw=raw, records=records)
    except Exception as exc:  # noqa: BLE001 - the run record is the error channel
        run.fail(exc)
        raise


def local_today() -> date:
    """Today's date in config.TIMEZONE."""
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def _columns(payload: dict[str, Any], block: str, fields: tuple[str, ...]) -> dict[str, list]:
    """Pull `time` plus each field from a column-oriented block and check they line up.

    Raises ValueError when the block or a field is missing, or when the arrays differ in
    length. Zipping unequal arrays would drop values without a trace.
    """
    data = payload.get(block)
    if not isinstance(data, dict):
        raise ValueError(f"Open-Meteo response has no '{block}' block")
    names = ("time", *fields)
    missing = [name for name in names if name not in data]
    if missing:
        raise ValueError(f"Open-Meteo '{block}' block is missing {', '.join(missing)}")
    lengths = {name: len(data[name]) for name in names}
    if len(set(lengths.values())) > 1:
        detail = ", ".join(f"{name}={n}" for name, n in lengths.items())
        raise ValueError(f"Open-Meteo '{block}' arrays differ in length: {detail}")
    return {name: data[name] for name in names}


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def parse_daily(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn Open-Meteo's column-oriented `daily` block into weather_daily records.

    Each record has obs_date, one key per field in DAILY_FIELDS, and `units`: the
    response's `daily_units` as a JSON string. Days the archive has not filled yet come
    back as nulls. They stay as rows with None values, so the null-rate check can see them.
    Raises ValueError if the arrays are not all the same length.
    """
    cols = _columns(payload, "daily", DAILY_FIELDS)
    units = payload.get("daily_units")
    if not isinstance(units, dict):
        raise ValueError("Open-Meteo response has no 'daily_units'")
    units_json = json.dumps(units, sort_keys=True, ensure_ascii=False)
    records = []
    for i, day in enumerate(cols["time"]):
        record: dict[str, Any] = {"obs_date": date.fromisoformat(day)}
        for name in DAILY_FIELDS:
            record[name] = _float(cols[name][i])
        record["units"] = units_json
        records.append(record)
    return records


def parse_forecast(payload: dict[str, Any], issue_date: date) -> list[dict[str, Any]]:
    """Turn a forecast response into weather_forecast_daily records.

    Same parsing as parse_daily. Each day becomes a target_date under the given issue_date.
    """
    records = []
    for row in parse_daily(payload):
        target = row.pop("obs_date")
        records.append({"issue_date": issue_date, "target_date": target, **row})
    return records


def parse_hourly(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Row-orient the `hourly` block into weather_hourly records.

    Each record has obs_ts, temperature_2m and `unit` from `hourly_units`. Timestamps arrive
    as local ISO strings without an offset because the request sets `timezone`. They are
    kept as naive local datetimes in config.TIMEZONE, so grouping by obs_ts's date gives
    the same local day as the daily block. Null hours stay as rows with None values.
    Raises ValueError if the arrays are not all the same length.
    """
    cols = _columns(payload, "hourly", HOURLY_FIELDS)
    units = payload.get("hourly_units")
    if not isinstance(units, dict) or "temperature_2m" not in units:
        raise ValueError("Open-Meteo response has no hourly unit for temperature_2m")
    unit = units["temperature_2m"]
    return [
        {"obs_ts": datetime.fromisoformat(ts), "temperature_2m": _float(value), "unit": unit}
        for ts, value in zip(cols["time"], cols["temperature_2m"], strict=True)
    ]


