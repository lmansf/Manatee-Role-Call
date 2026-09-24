"""Blue Spring daily manatee counts, scraped from Save the Manatee Club's sighting reports.

There is no API. The reports are not one post per day: a season page holds many dated
entries, written by people, and the wording drifts. The hub page (`LISTING_URL`) holds the
current season's entries until the season is archived on its own page (docs/sources.md §2).

This module is the single source of truth for reading those pages. It splits a page into
dated entries and extracts each entry's counts, estimate flag, "not counted" flag,
temperatures and review reasons. `tools/extract_seasons.py` imports the same logic to write
the per-season CSVs, so the backfill and the daily run cannot drift apart.

The extraction is conservative. It records every candidate number with the counter it was
attributed to, and gives a review reason whenever the prose is ambiguous, rather than guess.
Several small, named patterns fail in more legible ways than one giant regex.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from roll_call import config
from roll_call.ingest.base import IngestResult, IngestRun, sha256_text

# Hub page. It holds the current season's entries until that season gets an archive page.
LISTING_URL = "https://savethemanatee.org/bssp-manatee-reports/"

# If the WordPress REST API turns out to be open (docs/sources.md §2, smoke test 3), it gives
# page bodies without the theme wrapper, and `modified` as a freshness signal.
WP_API_URL = "https://savethemanatee.org/wp-json/wp/v2"

SOURCE_NAME = "blue_spring_counts"

USER_AGENT = "roll-call-pipeline (portfolio project; contact via GitHub)"
REQUEST_TIMEOUT_S = 30


# ---------------------------------------------------------------- page -> text blocks

CONTENT_SELECTORS = [".entry-content", "article", "main", "#content"]
BLOCK_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]
STRIP_TAGS = ["script", "style", "noscript", "nav", "footer", "header", "form", "aside"]


def normalise(text: str) -> str:
    """Fold the typographic variants the reports use (degree signs, dashes, quotes, spaces)."""
    text = text.replace("\xa0", " ").replace("º", "°").replace("˚", "°")
    text = text.replace("’", "'").replace("–", "-").replace("—", " - ")
    return re.sub(r"\s+", " ", text).strip()


def page_season(html: str) -> str | None:
    """Season named in the page title or first heading, e.g. '2023 – 2024' -> '2023-2024'."""
    soup = BeautifulSoup(html, "html.parser")
    for el in (soup.title, soup.find("h1")):
        if el:
            m = re.search(r"(20\d\d)\s*[–-]\s*(20\d\d)", el.get_text())
            if m:
                return f"{m.group(1)}-{m.group(2)}"
    return None


def content_blocks(html: str) -> list[str]:
    """The page body as normalised text blocks, one per paragraph, heading or list item.

    Navigation, headers, footers and scripts are dropped first, so a menu item that looks like
    a dated entry cannot become one.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(STRIP_TAGS):
        tag.decompose()
    root = next((el for sel in CONTENT_SELECTORS if (el := soup.select_one(sel))), soup.body or soup)
    blocks = [el.get_text(" ", strip=True) for el in root.find_all(BLOCK_TAGS) if not el.find_parent(BLOCK_TAGS)]
    total = len(root.get_text(" ", strip=True))
    if total and sum(len(b) for b in blocks) < 0.5 * total:
        # Text not wrapped in block tags (bare <br>-separated divs): fall back to lines.
        blocks = root.get_text("\n").splitlines()
    return [n for b in blocks if (n := normalise(b))]


# ---------------------------------------------------------------- blocks -> dated entries

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_LEAD = re.compile(
    r"^(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?"
    r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?!\d)"
    r"(?:,?\s+(?P<year>20\d\d))?",
    re.I,
)
NUMERIC_DATE_LEAD = re.compile(r"^(?P<mon>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2}|\d{4}))?(?!\d)")


@dataclass
class Entry:
    """One dated entry on a page: its date, its full text, and any doubts about the date."""

    date: date
    text: str
    reasons: list[str] = field(default_factory=list)


def season_for(day: date) -> str:
    """The July-to-June season a day falls in: 2026-09-24 -> '2026-2027'."""
    start = day.year if day.month >= 7 else day.year - 1
    return f"{start}-{start + 1}"


def previous_season(season: str) -> str:
    start = int(season.split("-")[0]) - 1
    return f"{start}-{start + 1}"


def parse_date_lead(season: str, block: str) -> tuple[date, str, list[str]] | None:
    """Read the date a block opens with. Returns (date, rest of the block, reasons) or None.

    Entries rarely state a year. July to December take the season's first year and January
    to June its second. A stated year that disagrees with the season wins, with a reason.
    """
    m = DATE_LEAD.match(block) or NUMERIC_DATE_LEAD.match(block)
    if not m:
        return None
    mon = m.group("mon")
    month = int(mon) if mon.isdigit() else MONTHS[mon[:3].lower()]
    start, end = (int(y) for y in season.split("-"))
    inferred = start if month >= 7 else end
    year, reasons = inferred, []
    if m.group("year"):
        year = int(m.group("year"))
        year += 2000 if year < 100 else 0
        if year != inferred:
            reasons.append(f"stated year {year} disagrees with season")
    try:
        d = date(year, month, int(m.group("day")))
    except ValueError:
        return None
    rest = block[m.end():].lstrip(" :-.,")
    return d, rest, reasons


def split_entries(season: str, blocks: list[str]) -> list[Entry]:
    """Group blocks into dated entries. A block without a date lead continues the entry before
    it; blocks before the first dated one (the page intro) are dropped."""
    entries: list[Entry] = []
    for b in blocks:
        parsed = parse_date_lead(season, b)
        if parsed:
            entries.append(Entry(*parsed))
        elif entries:
            entries[-1].text = f"{entries[-1].text} {b}".strip()
    return entries


# ---------------------------------------------------------------- entry text -> values

NUM = r"(\d{1,3}(?:,\d{3})+|\d{1,4})(?!\d|[.,]\d)"
NOT_A_COUNT = r"(?!\s*(?:°|degrees|F\b|C\b|%|a\.m|p\.m|am\b|pm\b))"
MANATEE_COUNT = re.compile(
    NUM + r"\s+(?:(?P<qual>additional|new|more|other|different|extra)\s+)?manatees?\b", re.I)
COUNT_VERB = re.compile(
    r"\bcount(?:ed|ing|s)?\b(?:\s+(?:was|were|is|of|went|up|down|to|dropped|rose|jumped|fell|"
    r"increased|decreased|climbed|reached|totaled|totalled|now|today|this|morning)){0,4}\s+"
    + NUM + NOT_A_COUNT, re.I)
ESTIMATE_COUNT = re.compile(
    r"\bestimat(?:e|ed|es)\b(?:\s+(?:was|were|is|of|at|to|be)){0,2}\s+" + NUM + NOT_A_COUNT, re.I)
BY_WHO = re.compile(NUM + r"\s+by\s+(?:the\s+)?(?:park|rangers?|staff|researchers?|us|smc)\b", re.I)
AFTER_BY = re.compile(
    r"\s*(?:\w+\s+)?(?:manatees?\s+)?(?:were\s+|was\s+)?(?:counted\s+)?by\s+(?:the\s+)?([a-z]+)", re.I)
PARK = re.compile(r"\b(?:park|rangers?|staff)\b", re.I)
SMC = re.compile(r"\b(?:researchers?|research|we|our|us|smc|wayne|cora)\b", re.I)
CLAUSE_BREAK = re.compile(r"[,;]|(?<!\d)\.(?!\d)|\b(?:while|but|whereas|though|although)\b", re.I)
ESTIMATE_BEFORE = re.compile(
    r"(?:about|approximately|approx\.?|around|roughly|an estimated|estimated|over|more than|"
    r"at least|nearly|almost|~)\s*$", re.I)
ESTIMATE_ANY = re.compile(r"\bestimat|\bundercount|\blow count\b", re.I)
DERIVED = re.compile(r"\+\s*\d+\s+others?\b|\band\s+\d+\s+others\b", re.I)
NO_COUNT = re.compile(
    r"\bno\s+(?:roll\s*call|count)\b|\b(?:could\s*not|couldn't|did\s*not|didn't|unable\s+to|"
    r"wasn't able to|were not able to)\s+(?:do\s+(?:a|the)\s+)?(?:count|roll\s*call)\b|\bnot\s+counted\b",
    re.I)
TEMP_F = r"(\d{2}(?:\.\d+)?)\s*°?\s*F\b"
SPAN = r"(?:[^.;]|(?<=\d)\.(?=\d)){0,60}?"
RIVER_TEMP = re.compile(r"\briver\b" + SPAN + TEMP_F, re.I)
AIR_TEMP = re.compile(r"\bair\s+temp\w*" + SPAN + TEMP_F, re.I)
# "Blue Spring" names the park, not the spring's water, so it never starts a spring temperature.
SPRING_TEMP = re.compile(r"(?<!blue )\bspring\b" + SPAN + TEMP_F, re.I)
# A sentence ends at . ! or ? followed by a space or the end. Decimals never match.
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")

IMPLAUSIBLE_COUNT = 1500
RIVER_TEMP_RANGE_F = (50, 80)


@dataclass
class Candidate:
    """One number that reads like a count, where it sits in the text, and who counted it."""

    value: int
    start: int
    end: int
    who: str | None = None  # "smc" | "park" | "ambiguous" | None
    qual: str | None = None
    estimate: bool = False


@dataclass
class EntryValues:
    """Everything extracted from one entry's text, before any output format is chosen."""

    count_researchers: Candidate | None
    count_park: Candidate | None
    others: list[Candidate]
    additional: list[Candidate]
    no_count: bool
    derived: bool
    estimate: bool
    river_temp_f: float | None
    air_temp_f: float | None
    spring_temp_f: float | None
    reasons: list[str]


def _attribute(text: str, start: int, end: int) -> str | None:
    m = AFTER_BY.match(text, end)
    if m and m.start() - end < 40:
        word = m.group(1)
        if PARK.search(word):
            return "park"
        if SMC.search(word):
            return "smc"
    clause = CLAUSE_BREAK.split(text[max(0, start - 60):start])[-1]
    park, smc = bool(PARK.search(clause)), bool(SMC.search(clause))
    if park and smc:
        return "ambiguous"
    return "park" if park else "smc" if smc else None


def find_counts(text: str) -> list[Candidate]:
    """Every number in the text that reads like a count, in order, with its counter."""
    found: dict[int, Candidate] = {}
    stated_estimates: set[int] = set()
    for pattern in (MANATEE_COUNT, COUNT_VERB, ESTIMATE_COUNT, BY_WHO):
        for m in pattern.finditer(text):
            s, e = m.span(1)
            c = found.setdefault(s, Candidate(int(m.group(1).replace(",", "")), s, e))
            if pattern is MANATEE_COUNT and m.group("qual"):
                c.qual = m.group("qual").lower()
            if pattern is ESTIMATE_COUNT:
                stated_estimates.add(s)
    for c in found.values():
        c.who = _attribute(text, c.start, c.end)
        c.estimate = c.start in stated_estimates or bool(ESTIMATE_BEFORE.search(text[max(0, c.start - 20):c.start]))
    return sorted(found.values(), key=lambda c: c.start)


def _first_temp(pattern: re.Pattern, text: str) -> float | None:
    m = pattern.search(text)
    return float(m.group(1)) if m else None


def extract_entry(entry: Entry) -> EntryValues:
    """Extract counts, flags, temperatures and review reasons from one entry.

    The researchers' count is the first number attributed to the researchers, or failing that
    the first unattributed one, since the reports are the researchers' own. A number qualified
    by "additional" or "new" is late arrivals, never a count. Every leftover number and every
    ambiguity becomes a review reason.
    """
    text, reasons = entry.text, list(entry.reasons)
    cands = find_counts(text)
    totals = [c for c in cands if not c.qual]
    additional = [c for c in cands if c.qual]
    smc = [c for c in totals if c.who == "smc"]
    park = [c for c in totals if c.who == "park"]
    unattributed = [c for c in totals if c.who is None]
    ambiguous = [c for c in totals if c.who == "ambiguous"]

    count_researchers = smc[0] if smc else (unattributed.pop(0) if unattributed else None)
    count_park = park[0] if park else None
    others = smc[1:] + park[1:] + unattributed + ambiguous

    no_count = bool(NO_COUNT.search(text))
    derived = bool(DERIVED.search(text))
    estimate = any(c.estimate for c in (count_researchers, count_park) if c) or bool(ESTIMATE_ANY.search(text))
    river = _first_temp(RIVER_TEMP, text)

    if others:
        reasons.append("more than one candidate count")
    if ambiguous:
        reasons.append("count attribution ambiguous")
    if derived:
        reasons.append("count written as a sum (name + N others)")
    if not count_researchers and not count_park and not no_count:
        reasons.append("only an 'additional' count" if additional else "no count found")
    if no_count and (count_researchers or count_park):
        reasons.append("says no count but has a number")
    for c in (count_researchers, count_park):
        if c and c.value > IMPLAUSIBLE_COUNT:
            reasons.append(f"implausible count {c.value}")
    low, high = RIVER_TEMP_RANGE_F
    if river is not None and not low <= river <= high:
        reasons.append(f"river temp {river} outside {low}-{high}F")

    return EntryValues(
        count_researchers=count_researchers,
        count_park=count_park,
        others=others,
        additional=additional,
        no_count=no_count,
        derived=derived,
        estimate=estimate,
        river_temp_f=river,
        air_temp_f=_first_temp(AIR_TEMP, text),
        spring_temp_f=_first_temp(SPRING_TEMP, text),
        reasons=reasons,
    )


def page_entries(html: str, season: str) -> list[Entry]:
    """Split a page into dated entries. Years come from `season` unless an entry states one.

    When the page names a different season in its title, every entry gets a review reason.
    """
    entries = split_entries(season, content_blocks(html))
    named = page_season(html)
    if named and named != season:
        for e in entries:
            e.reasons.append(f"page is titled {named}, not {season}")
    return entries


# ---------------------------------------------------------------- entries -> SightingReport


@dataclass
class SightingReport:
    """One dated entry from a report page. Counts are None when that counter reported nothing."""

    report_date: date
    # Researchers and park staff count separately and both numbers are usually reported.
    # The researchers' count is the target; the park count is its own series, and the gap
    # between them is monitored as counter disagreement (CONTEXT.md).
    count_researchers: int | None
    count_park: int | None
    # "Not counted": the report says no roll call took place and gives no count. Distinct
    # from a zero count.
    not_counted: bool
    # The researchers called their number an estimate. Still a count, still the target.
    is_estimate: bool
    river_temp_f: float | None
    spring_temp_f: float | None
    # The page the entry came from. Entries share it; the date tells them apart.
    post_url: str
    # The sentence(s) the counts, or the "no roll call" statement, came from. When a count
    # looks wrong months later, this tells a parser bug from a real 700-manatee morning.
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


def _sentence_span(text: str, pos: int) -> tuple[int, int]:
    """Start and end of the sentence that contains character `pos`."""
    start = 0
    for m in SENTENCE_END.finditer(text):
        if m.end() <= pos:
            start = m.end()
        else:
            return start, m.end()
    return start, len(text)


def _count_text(text: str, values: EntryValues) -> str | None:
    """The sentences holding the counts, in text order. For a day not counted, the sentence
    that says so. None when the entry has neither."""
    positions = [c.start for c in (values.count_researchers, values.count_park) if c]
    if not positions and values.no_count:
        positions = [m.start() for m in [NO_COUNT.search(text)] if m]
    spans = sorted({_sentence_span(text, p) for p in positions})
    return " ".join(text[s:e].strip() for s, e in spans) or None


def report_from_entry(entry: Entry, post_url: str) -> SightingReport:
    """Turn one dated entry into a SightingReport.

    not_counted is set only when the entry says no roll call took place and no count was
    found. When it says both, the count is kept, and the tool's review reason ("says no count
    but has a number") is the place a person sees the conflict.
    """
    values = extract_entry(entry)
    researchers, park = values.count_researchers, values.count_park
    return SightingReport(
        report_date=entry.date,
        count_researchers=researchers.value if researchers else None,
        count_park=park.value if park else None,
        not_counted=values.no_count and researchers is None and park is None,
        is_estimate=values.estimate,
        river_temp_f=values.river_temp_f,
        spring_temp_f=values.spring_temp_f,
        post_url=post_url,
        count_text=_count_text(entry.text, values),
    )


def parse_page(html: str, page_url: str, season: str) -> list[SightingReport]:
    """One SightingReport per dated entry on a season page or the hub, in page order."""
    return [report_from_entry(e, page_url) for e in page_entries(html, season)]


def infer_season(html: str, fetched_on: date) -> str:
    """The season to read entry years against, for a page fetched on `fetched_on`.

    A season the page names in its title wins. Otherwise it is the July-to-June season the
    fetch date falls in, unless that would put an entry after the fetch date. That happens
    between July and the first report of a new winter, while the hub still shows last
    season, and then the previous season is used.
    """
    named = page_season(html)
    if named:
        return named
    season = season_for(fetched_on)
    if any(e.date > fetched_on for e in page_entries(html, season)):
        return previous_season(season)
    return season


def one_per_date(reports: list[SightingReport]) -> list[SightingReport]:
    """Keep one report per date, since report_date is the table's key. A page that repeats a
    date keeps the first entry with a count, else the first entry. Sorted by date."""
    chosen: dict[date, SightingReport] = {}
    for r in reports:
        current = chosen.get(r.report_date)
        has_count = r.count_researchers is not None or r.count_park is not None
        current_has_count = current is not None and (
            current.count_researchers is not None or current.count_park is not None)
        if current is None or (has_count and not current_has_count):
            chosen[r.report_date] = r
    return [chosen[d] for d in sorted(chosen)]


def _today() -> date:
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def fetch_reports(
    since: date | None = None, session: requests.Session | None = None
) -> IngestResult:
    """Fetch the hub page, parse its dated entries, and keep those on or after `since`.

    One polite request (descriptive User-Agent, timeout). The raw payload is the page HTML,
    so history can be re-parsed without another fetch. Entry years come from infer_season,
    using today's date in the park's time zone. Records the run whether or not it succeeds.
    """
    run = IngestRun(source=SOURCE_NAME, source_url=LISTING_URL)
    sess = session or requests.Session()
    try:
        resp = sess.get(LISTING_URL, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        raw = resp.text
        run.payload_sha256 = sha256_text(raw)
        season = infer_season(raw, _today())
        reports = one_per_date(parse_page(raw, LISTING_URL, season))
        records = [r.to_record() for r in reports if since is None or r.report_date >= since]
        run.succeed(len(records))
        return IngestResult(run=run, raw=raw, records=records)
    except Exception as exc:  # noqa: BLE001 - the run record is the error channel
        run.fail(exc)
        raise
