"""Blue Spring daily manatee counts, scraped from Save the Manatee Club's sighting reports.

There is no API. Reports are HTML posts, written by people, and the wording drifts:
"Total of 412 manatees" one day, "412 manatees counted" the next, "no count today due to
fog" on a third. That is the point. This is the source the quality layer exists to watch.

Deliberately unimplemented parts are marked TODO(you). Read the fixture HTML before writing
any parsing code. Save two or three real reports to `tests/fixtures/` first (different
months if you can), and write the parser against those. The pattern you *think* the page
uses and the pattern it *actually* uses will differ, and finding out how is the lesson.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import requests
from bs4 import BeautifulSoup

from roll_call.ingest.base import IngestResult, IngestRun, sha256_text

# Hub page linking each season's report page. Season/month page slugs change every year
# (see docs/sources.md §2), so those are discovered from here, never hard-coded.
LISTING_URL = "https://savethemanatee.org/bssp-manatee-reports/"

# If the WordPress REST API turns out to be open (docs/sources.md §2, smoke test 3),
# prefer it: post bodies without the theme wrapper, and `modified` as a freshness signal.
WP_API_URL = "https://savethemanatee.org/wp-json/wp/v2"

SOURCE_NAME = "blue_spring_counts"

USER_AGENT = "roll-call-pipeline (portfolio project; contact via GitHub)"


@dataclass
class SightingReport:
    """One parsed report entry. Counts are None when that counter reported nothing."""

    report_date: date
    # Researchers and park staff count separately and both numbers are usually reported.
    # The researchers' count is the target; the park count is its own series, and the gap
    # between them is monitored as counter disagreement (CONTEXT.md).
    count_researchers: int | None
    count_park: int | None
    # "Not counted": the report says no roll call took place. Distinct from a zero count.
    not_counted: bool
    # The researchers called their number an estimate. Still a count, still the target.
    is_estimate: bool
    river_temp_f: float | None
    spring_temp_f: float | None
    post_url: str
    # Keep the sentence you extracted the number from. When a count looks wrong six months
    # from now, this is how you'll tell a parser bug from a real 700-manatee morning.
    count_text: str | None

    def to_record(self) -> dict[str, Any]:
        return {
            "report_date": self.report_date,
            "count_researchers": self.count_researchers,
            "count_park": self.count_park,
            "not_counted": self.not_counted,
            "is_estimate": self.is_estimate,
            "river_temp_f": self.river_temp_f,
            "spring_temp_f": self.spring_temp_f,
            "post_url": self.post_url,
            "count_text": self.count_text,
        }


def fetch_listing(session: requests.Session | None = None) -> str:
    """Return the HTML of the listing page. Nothing else; parsing is separate so it can
    be tested against saved fixtures without network access."""
    sess = session or requests.Session()
    resp = sess.get(LISTING_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_listing(html: str) -> list[str]:
    """Extract the URLs of individual sighting-report posts from the listing page.

    TODO(you): implement against a saved fixture. Watch for: pagination, posts that are
    not sighting reports mixed into the same feed, relative vs absolute URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    raise NotImplementedError


def parse_report(html: str, post_url: str) -> SightingReport:
    """Extract date, count and temperatures from one report's HTML.

    TODO(you): implement. Suggested order of attack:
      1. Date. Is it in the URL, the title, a <time> tag, or only in the prose?
      2. Counts. Find the sentence, then the numbers. Keep the sentence (`count_text`).
         Two counts per day are common ("186 by researchers, 191 by the park"). A count
         qualified by "additional" or "new" is not a roll call total; don't store it as one.
      3. Temperatures. Written as °F with °C in parentheses, sometimes with "~". Parse the
         °F figure; use the °C only as a cross-check.
      4. The "no roll call today" case. Set not_counted=True and leave both counts None,
         never 0. Zero manatees is data; not counted is absence of data, and the checks
         need to tell them apart (CONTEXT.md: Counted / Not counted / Unreported).
         Off-season monthly updates (April, June, August) carry no roll call at all.

    Resist a single giant regex. Several small, named ones fail in more legible ways.
    """
    soup = BeautifulSoup(html, "html.parser")
    raise NotImplementedError


def fetch_reports(
    since: date | None = None, session: requests.Session | None = None
) -> IngestResult:
    """Fetch the listing, then each report newer than `since`, and parse them all.

    The raw payload stored for this source should be the concatenation of every report's
    HTML (or one raw row per post; your call, but decide it now). The listing page is
    not worth keeping.

    TODO(you): wire together fetch_listing -> parse_listing -> fetch each post ->
    parse_report. Be polite: one request at a time, a short sleep between posts. On a
    parse failure for a single post, decide whether the whole run fails or the post is
    skipped and counted. (Hint: the volume check wants to know about skipped posts.)
    """
    run = IngestRun(source=SOURCE_NAME, source_url=LISTING_URL)
    raise NotImplementedError
