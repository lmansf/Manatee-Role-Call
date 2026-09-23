"""One CSV per Save the Manatee Club season page: the bootstrap for the historical backfill.

Run where savethemanatee.org is reachable:

    python tools/extract_seasons.py                    # fetch (cached) and extract every season
    python tools/extract_seasons.py --season 2023-2024 # one season
    python tools/extract_seasons.py --offline          # re-extract from cached HTML only
    python tools/extract_seasons.py --score            # compare 2025-2026 output with the hand-made reference

Pages are fetched once, politely, and cached in data/raw/stmc/<season>.html. If the site blocks
scripted requests, save each page from a browser ("Save Page As", HTML only) to that path and
run with --offline.

The extraction is conservative on purpose. It records every candidate count with who it was
attributed to, and sets needs_review with a reason whenever the prose is ambiguous, rather than
guessing. Aggregation (which count to trust, what to do with estimates) is left to you.

This is not the pipeline's parser. roll_call/ingest/blue_spring.parse_report is separate and
still yours to write; the review reasons this tool emits are the cases that parser must handle.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib import robotparser

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "stmc"
OUT_DIR = ROOT / "data" / "seasons"
REFERENCE = ROOT / "data" / "reference" / "blue_spring_manatee_counts_2025_2026.csv"

BASE = "https://savethemanatee.org"
USER_AGENT = "roll-call-pipeline (portfolio project; one fetch per season page)"

# Candidate URLs per season, tried in order. Slugs differ every year; see docs/sources.md §2.
SEASONS: dict[str, list[str]] = {
    "2018-2019": [f"{BASE}/?p=4458"],
    "2019-2020": [f"{BASE}/manatee-sighting-reports-2019-2020/", f"{BASE}/manatee-reports-2019-2020-season"],
    "2020-2021": [f"{BASE}/manatee-sighting-reports-2020-2021/"],
    "2021-2022": [f"{BASE}/manatee-sighting-reports-2021-2022/", f"{BASE}/manatee-reports-2021-2022/"],
    "2022-2023": [f"{BASE}/bssp-report-2022-2023/"],
    "2023-2024": [f"{BASE}/manatee-sighting-reports-2023-2024/"],
    "2024-2025": [f"{BASE}/manatee-sighting-reports-2024-2025/"],
    # No archive page found for 2025-26. The current season appears to live on the hub until it
    # is archived, so the hub is the fallback. Once 2026-27 starts, the hub will hold that instead.
    "2025-2026": [f"{BASE}/manatee-sighting-reports-2025-2026/", f"{BASE}/bssp-manatee-reports/"],
}

COLUMNS = [
    "season", "date", "count_smc", "count_park", "count_other", "estimate", "additional",
    "derived", "no_count", "river_temp_f", "air_temp_f", "needs_review", "review_reason",
    "source_url", "entry_text",
]

# ---------------------------------------------------------------- page -> text blocks

CONTENT_SELECTORS = [".entry-content", "article", "main", "#content"]
BLOCK_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]
STRIP_TAGS = ["script", "style", "noscript", "nav", "footer", "header", "form", "aside"]


def normalise(text: str) -> str:
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
    date: date
    text: str
    reasons: list[str] = field(default_factory=list)


def parse_date_lead(season: str, block: str) -> tuple[date, str, list[str]] | None:
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


@dataclass
class Candidate:
    value: int
    start: int
    end: int
    who: str | None = None  # "smc" | "park" | "ambiguous" | None
    qual: str | None = None
    estimate: bool = False


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


def extract(entry: Entry, season: str, source_url: str) -> dict:
    text, reasons = entry.text, list(entry.reasons)
    cands = find_counts(text)
    totals = [c for c in cands if not c.qual]
    additional = [c for c in cands if c.qual]
    smc = [c for c in totals if c.who == "smc"]
    park = [c for c in totals if c.who == "park"]
    unattributed = [c for c in totals if c.who is None]
    ambiguous = [c for c in totals if c.who == "ambiguous"]

    count_smc = smc[0] if smc else (unattributed.pop(0) if unattributed else None)
    count_park = park[0] if park else None
    others = smc[1:] + park[1:] + unattributed + ambiguous

    no_count = bool(NO_COUNT.search(text))
    derived = bool(DERIVED.search(text))
    estimate = any(c.estimate for c in (count_smc, count_park) if c) or bool(ESTIMATE_ANY.search(text))
    river = _first_temp(RIVER_TEMP, text)
    air = _first_temp(AIR_TEMP, text)

    if others:
        reasons.append("more than one candidate count")
    if ambiguous:
        reasons.append("count attribution ambiguous")
    if derived:
        reasons.append("count written as a sum (name + N others)")
    if not count_smc and not count_park and not no_count:
        reasons.append("only an 'additional' count" if additional else "no count found")
    if no_count and (count_smc or count_park):
        reasons.append("says no count but has a number")
    for c in (count_smc, count_park):
        if c and c.value > 1500:
            reasons.append(f"implausible count {c.value}")
    if river is not None and not 50 <= river <= 80:
        reasons.append(f"river temp {river} outside 50-80F")

    def v(c: Candidate | None) -> str:
        return str(c.value) if c else ""

    return {
        "season": season,
        "date": entry.date.isoformat(),
        "count_smc": v(count_smc),
        "count_park": v(count_park),
        "count_other": "|".join(v(c) for c in others),
        "estimate": str(estimate).upper(),
        "additional": "|".join(v(c) for c in additional),
        "derived": str(derived).upper(),
        "no_count": str(no_count).upper(),
        "river_temp_f": "" if river is None else f"{river:g}",
        "air_temp_f": "" if air is None else f"{air:g}",
        "needs_review": str(bool(reasons)).upper(),
        "review_reason": "; ".join(reasons),
        "source_url": source_url,
        "entry_text": text[:2000],
    }


def extract_season(season: str, html: str, source_url: str) -> list[dict]:
    named = page_season(html)
    entries = split_entries(season, content_blocks(html))
    if named and named != season:
        for e in entries:
            e.reasons.append(f"page is titled {named}, not {season}")
    rows = sorted((extract(e, season, source_url) for e in entries), key=lambda r: r["date"])
    dupes = Counter(r["date"] for r in rows)
    for r in rows:
        if dupes[r["date"]] > 1:
            r["review_reason"] = "; ".join(filter(None, [r["review_reason"], f"date appears {dupes[r['date']]} times"]))
            r["needs_review"] = "TRUE"
    return rows


# ---------------------------------------------------------------- fetching and output

def _robots() -> robotparser.RobotFileParser | None:
    rp = robotparser.RobotFileParser(f"{BASE}/robots.txt")
    try:
        rp.read()
        return rp
    except Exception:  # noqa: BLE001 - robots.txt unreachable: proceed, one request per page anyway
        return None


def load_page(season: str, offline: bool, refresh: bool, pause: float,
              session: requests.Session, robots) -> tuple[str, str]:
    html_path, url_path = RAW_DIR / f"{season}.html", RAW_DIR / f"{season}.url"
    if html_path.exists() and not refresh:
        url = url_path.read_text().strip() if url_path.exists() else SEASONS[season][0]
        return html_path.read_text(encoding="utf-8", errors="replace"), url
    if offline:
        raise FileNotFoundError(f"{html_path} not cached; run without --offline or save it from a browser")
    errors = []
    for url in SEASONS[season]:
        if robots is not None and not robots.can_fetch(USER_AGENT, url):
            errors.append(f"{url}: disallowed by robots.txt")
            continue
        resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        time.sleep(pause)
        if resp.status_code == 200 and "html" in resp.headers.get("content-type", ""):
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            html_path.write_text(resp.text, encoding="utf-8")
            url_path.write_text(resp.url)
            return resp.text, resp.url
        errors.append(f"{url}: HTTP {resp.status_code}")
    raise RuntimeError("; ".join(errors))


def write_csv(season: str, rows: list[dict]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"stmc_{season.replace('-', '_')}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return path


def score() -> int:
    """Compare the extracted 2025-26 CSV against the hand-transcribed reference."""
    extracted_path = OUT_DIR / "stmc_2025_2026.csv"
    if not extracted_path.exists():
        print(f"{extracted_path} missing; extract 2025-2026 first", file=sys.stderr)
        return 1
    with extracted_path.open(newline="", encoding="utf-8") as f:
        by_date: dict[str, list[dict]] = {}
        for r in csv.DictReader(f):
            by_date.setdefault(r["date"], []).append(r)
    with REFERENCE.open(newline="", encoding="utf-8") as f:
        reference = list(csv.DictReader(f))

    agree, disagree, missing = 0, [], []
    for ref in reference:
        rows = by_date.get(ref["date"])
        if not rows:
            missing.append(ref["date"])
            continue
        column = "count_park" if ref["count_source"] == "Park staff" else "count_smc"
        got = {r[column] for r in rows}
        if ref["count"] in got:
            agree += 1
        else:
            disagree.append((ref["date"], ref["count"], column, "|".join(sorted(got)) or "(blank)"))
        park_note = re.search(r"Park counted (\d+)", ref["notes"])
        if park_note and park_note.group(1) not in {r["count_park"] for r in rows}:
            disagree.append((ref["date"], park_note.group(1), "count_park", "|".join(r["count_park"] for r in rows) or "(blank)"))
    extra = sorted(set(by_date) - {r["date"] for r in reference})

    print(f"reference dates: {len(reference)}  agree: {agree}  disagree: {len(disagree)}  missing: {len(missing)}  extra: {len(extra)}")
    for d, want, col, got in disagree:
        print(f"  {d}  {col}: reference {want}, extracted {got}")
    if missing:
        print("  missing:", ", ".join(missing))
    if extra:
        print("  extracted but not in reference:", ", ".join(extra))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--season", action="append", choices=sorted(SEASONS), help="repeatable; default all")
    ap.add_argument("--offline", action="store_true", help="use cached HTML only")
    ap.add_argument("--refresh", action="store_true", help="re-fetch even if cached")
    ap.add_argument("--pause", type=float, default=3.0, help="seconds between requests")
    ap.add_argument("--score", action="store_true", help="score 2025-2026 output against the reference")
    args = ap.parse_args(argv)
    if args.score:
        return score()

    session = requests.Session()
    robots = None if args.offline else _robots()
    status = 0
    for season in args.season or sorted(SEASONS):
        try:
            html, url = load_page(season, args.offline, args.refresh, args.pause, session, robots)
        except Exception as exc:  # noqa: BLE001 - report and continue with other seasons
            print(f"{season}: FAILED {exc}", file=sys.stderr)
            status = 1
            continue
        rows = extract_season(season, html, url)
        path = write_csv(season, rows)
        counted = sum(bool(r["count_smc"] or r["count_park"]) for r in rows)
        review = sum(r["needs_review"] == "TRUE" for r in rows)
        print(f"{season}: {len(rows)} entries, {counted} with a count, {review} need review -> {path.relative_to(ROOT)}")
        if not rows:
            print(f"  no dated entries found; open {RAW_DIR / (season + '.html')} and check the date format",
                  file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
