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

# TODO(you): confirm the current listing URL for the Blue Spring sighting reports on
# savethemanatee.org and put it here. Store it in one place so a URL change is a
# one-line fix and shows up in `ingest_runs.source_url`.
LISTING_URL = "https://www.savethemanatee.org/"

SOURCE_NAME = "blue_spring_counts"

USER_AGENT = "roll-call-pipeline (portfolio project; contact via GitHub)"


@dataclass
class SightingReport:
    """One parsed report. `count` is None when the report says no count was taken."""

    report_date: date
    count: int | None
    river_temp_f: float | None
    spring_temp_f: float | None
    post_url: str
    # Keep the sentence you extracted the number from. When a count looks wrong six months
    # from now, this is how you'll tell a parser bug from a real 700-manatee morning.
    count_text: str | None

    def to_record(self) -> dict[str, Any]:
        return {
            "report_date": self.report_date,
            "count": self.count,
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
      2. Count. Find the sentence, then the number. Keep the sentence (`count_text`).
      3. Temperatures. River and spring are usually both mentioned; don't assume order.
      4. The "no count today" case. Return count=None, not 0. Zero manatees is data;
         no count is absence of data, and the volume check needs to tell them apart.

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
