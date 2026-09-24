"""Gauge temperature from USGS gauge 02236000, St. Johns River near DeLand.

The new Water Data API (the legacy waterservices.usgs.gov is being retired; see
docs/sources.md §3). Daily-mean water temperature:

    GET https://api.waterdata.usgs.gov/ogcapi/v0/collections/daily/items
        ?monitoring_location_id=USGS-02236000&parameter_code=00010&statistic_id=00003
        &time=<start>/<end>&limit=50000&f=json

The response is a GeoJSON FeatureCollection. Each feature's `properties` carries `time`,
`value`, `unit_of_measure` and `approval_status` (Provisional, later Approved, sometimes
with a changed value). A free API key raises the rate limit; send it as `X-Api-Key`.

Paging follows USGS's own client (`dataretrieval`, `ogc/engine.py` and
`transport/pagination.py`). The client asks for `limit=50000`, the API's largest page. A
page names the next page in its `links` array as the link with `rel` "next". The client
stops at a page with no features, no `next` link, or an empty `href`. It resolves a
relative `href` against the page it came from and refuses a link to another host, because
the follow-up request carries the API key. The next link holds the whole query, so the
follow-up sends no params of its own.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from roll_call import config
from roll_call.ingest.base import IngestResult, IngestRun, sha256_text

log = logging.getLogger(__name__)

DAILY_URL = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/daily/items"
SOURCE_NAME = "usgs_gauge_daily"

# The API's largest page, and what dataretrieval asks for by default.
PAGE_LIMIT = 50_000
# A season is about 150 days, so one page is normal. Anything near this cap means the
# next links are not converging.
MAX_PAGES = 100


class PaginationError(RuntimeError):
    """The API's next links would not end, or pointed somewhere they should not."""


def build_daily_params(start: date, end: date) -> dict[str, Any]:
    """Query params for daily-mean water temperature over [start, end] inclusive."""
    return {
        "monitoring_location_id": config.GAUGE_SITE,
        "parameter_code": config.GAUGE_WATER_TEMP_PARAM,
        "statistic_id": config.GAUGE_DAILY_MEAN_STAT,
        "time": f"{start.isoformat()}/{end.isoformat()}",
        "limit": PAGE_LIMIT,
        "f": "json",
    }


def _headers() -> dict[str, str]:
    key = config.env("API_USGS_PAT", required=False)
    return {"X-Api-Key": key} if key else {}


def next_page_url(payload: dict[str, Any], page_url: str) -> str | None:
    """The URL of the page after this one, or None when this is the last page.

    Mirrors dataretrieval's `_next_req_url`. A page with no features ends the walk even if
    it carries a next link. A relative link is resolved against `page_url`. A link to a
    host other than the API's raises, because the follow-up request carries the API key.
    """
    if not (payload.get("features") or []):
        return None
    for link in payload.get("links") or []:
        if link.get("rel") != "next":
            continue
        href = link.get("href")
        if not href:
            return None
        target = urljoin(page_url, href)
        expected_host = urlparse(DAILY_URL).netloc
        if urlparse(target).netloc != expected_host:
            raise PaginationError(
                f"next link points at {urlparse(target).netloc!r}, not {expected_host!r}"
            )
        return target
    return None


def fetch_daily(start: date, end: date, session: requests.Session | None = None) -> IngestResult:
    """Fetch daily-mean water temperature for the window, every page of it.

    Follows `next` links until none remain and parses every page. `result.raw` is a JSON
    array of the raw page texts, in the order fetched. A next link that repeats a page
    already fetched, or a walk longer than MAX_PAGES, fails the run: a partial result
    that looks complete would hide the fault. Records the run either way.
    """
    run = IngestRun(source=SOURCE_NAME, source_url=DAILY_URL)
    sess = session or requests.Session()
    try:
        headers = _headers()
        resp = sess.get(DAILY_URL, params=build_daily_params(start, end), headers=headers, timeout=30)
        pages: list[str] = []
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            resp.raise_for_status()
            page_url = str(resp.url or DAILY_URL)
            seen.add(page_url)
            pages.append(resp.text)
            payload = resp.json()
            records.extend(parse_daily(payload))
            url = next_page_url(payload, page_url)
            if url is None:
                break
            if url in seen:
                raise PaginationError(f"next link repeats a page already fetched: {url}")
            if len(pages) >= MAX_PAGES:
                raise PaginationError(f"still paging after {MAX_PAGES} pages")
            resp = sess.get(url, headers=headers, timeout=30)
        raw = json.dumps(pages)
        run.payload_sha256 = sha256_text(raw)
        run.succeed(len(records))
        return IngestResult(run=run, raw=raw, records=records)
    except Exception as exc:  # noqa: BLE001 - the run record is the error channel
        run.fail(exc)
        raise


def _to_float(value: Any, obs_date: date) -> float | None:
    """The reading as a float, or None when it is absent or not a number.

    The API sends `value` as a string. A non-numeric string (an equipment code, say) is
    logged and becomes None: the day has a row, but no temperature.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        log.warning("gauge value for %s is not a number: %r", obs_date, value)
        return None
    if not math.isfinite(number):
        log.warning("gauge value for %s is not finite: %r", obs_date, value)
        return None
    return number


def parse_daily(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One record per feature on this page: obs_date, water_temp_c, unit, approval_status.

    `unit` is copied from `unit_of_measure` onto every row. The column name says °C
    because the parameter code asks for it, but a unit change is what the distribution
    checks should catch, so nothing here converts or assumes. `approval_status` is kept
    as sent: a value first seen as Provisional and later as Approved is a revision, and
    the two rows differ by status. A day the gauge did not report has no feature and so
    no row. It is unreported, not zero.
    """
    records: list[dict[str, Any]] = []
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        obs_date = date.fromisoformat(str(props["time"])[:10])
        records.append(
            {
                "obs_date": obs_date,
                "water_temp_c": _to_float(props.get("value"), obs_date),
                "unit": props.get("unit_of_measure"),
                "approval_status": props.get("approval_status"),
            }
        )
    return records
