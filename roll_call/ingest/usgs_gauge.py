"""River temperature from USGS gauge 02236000, St. Johns River near DeLand.

The new Water Data API (the legacy waterservices.usgs.gov is being retired; see
docs/sources.md §3). Daily-mean water temperature:

    GET https://api.waterdata.usgs.gov/ogcapi/v0/collections/daily/items
        ?monitoring_location_id=USGS-02236000&parameter_code=00010&statistic_id=00003
        &time=<start>/<end>&f=json

The response is a GeoJSON FeatureCollection. Each feature's `properties` carries `time`,
`value`, `unit_of_measure` and `approval_status` (Provisional, later Approved, sometimes
with a changed value). A free API key raises the rate limit; send it as `X-Api-Key`.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import requests

from roll_call import config
from roll_call.ingest.base import IngestResult, IngestRun, sha256_text

DAILY_URL = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/daily/items"
SOURCE_NAME = "usgs_gauge_daily"


def build_daily_params(start: date, end: date) -> dict[str, Any]:
    """Query params for daily-mean water temperature over [start, end] inclusive."""
    return {
        "monitoring_location_id": config.GAUGE_SITE,
        "parameter_code": config.GAUGE_WATER_TEMP_PARAM,
        "statistic_id": config.GAUGE_DAILY_MEAN_STAT,
        "time": f"{start.isoformat()}/{end.isoformat()}",
        "f": "json",
    }


def _headers() -> dict[str, str]:
    key = config.env("API_USGS_PAT", required=False)
    return {"X-Api-Key": key} if key else {}


def fetch_daily(start: date, end: date, session: requests.Session | None = None) -> IngestResult:
    """Fetch daily-mean water temperature for the window. Records the run either way.

    TODO(owner): pagination. The API pages large results; a season-long window may not fit in
    one response. Look for a `next` link in the response's `links` and follow it until there
    is none, keeping every page's raw text. Try a one-year window against the live API first
    and see what comes back.
    """
    run = IngestRun(source=SOURCE_NAME, source_url=DAILY_URL)
    sess = session or requests.Session()
    try:
        resp = sess.get(DAILY_URL, params=build_daily_params(start, end), headers=_headers(), timeout=30)
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
    """One record per day: obs_date, water_temp_c, unit, approval_status.

    TODO(owner): implement against a saved fixture (smoke test 5 in docs/sources.md).
      - `value` may arrive as a string. Convert it, and decide what a non-numeric value means.
      - Check `unit_of_measure` rather than assuming °C. A unit change is exactly what the
        distribution checks should catch, so keep the unit on every row.
      - Keep `approval_status`. When a provisional value is later approved with a different
        number, that is a revision to record, not a duplicate to discard.
      - Expect gaps: gauges go offline. A missing day is unreported, not a zero.
    """
    raise NotImplementedError
