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

The extraction itself lives in roll_call/ingest/blue_spring.py, shared with the daily pipeline.
This tool only fetches season pages, formats one CSV row per entry, and flags repeated dates.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib import robotparser

import requests

ROOT = Path(__file__).resolve().parents[1]
# Run as a script, this folder is on sys.path but the repo root may not be. Put it first so
# the tool always reads pages with this checkout's parser.
sys.path.insert(0, str(ROOT))

from roll_call.ingest.blue_spring import (  # noqa: E402
    Candidate,
    Entry,
    extract_entry,
    page_entries,
    parse_date_lead,  # noqa: F401  (re-exported for callers and tests)
)

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
    "season", "date", "count_researchers", "count_park", "count_other", "estimate", "additional",
    "derived", "no_count", "river_temp_f", "air_temp_f", "needs_review", "review_reason",
    "source_url", "entry_text",
]

# ---------------------------------------------------------------- entry -> CSV row

def extract(entry: Entry, season: str, source_url: str) -> dict:
    """One CSV row for one dated entry. The values come from blue_spring.extract_entry."""
    x = extract_entry(entry)

    def v(c: Candidate | None) -> str:
        return str(c.value) if c else ""

    return {
        "season": season,
        "date": entry.date.isoformat(),
        "count_researchers": v(x.count_researchers),
        "count_park": v(x.count_park),
        "count_other": "|".join(v(c) for c in x.others),
        "estimate": str(x.estimate).upper(),
        "additional": "|".join(v(c) for c in x.additional),
        "derived": str(x.derived).upper(),
        "no_count": str(x.no_count).upper(),
        "river_temp_f": "" if x.river_temp_f is None else f"{x.river_temp_f:g}",
        "air_temp_f": "" if x.air_temp_f is None else f"{x.air_temp_f:g}",
        "needs_review": str(bool(x.reasons)).upper(),
        "review_reason": "; ".join(x.reasons),
        "source_url": source_url,
        "entry_text": entry.text[:2000],
    }


def extract_season(season: str, html: str, source_url: str) -> list[dict]:
    rows = sorted((extract(e, season, source_url) for e in page_entries(html, season)), key=lambda r: r["date"])
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
        column = "count_park" if ref["count_source"] == "Park staff" else "count_researchers"
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
        counted = sum(bool(r["count_researchers"] or r["count_park"]) for r in rows)
        review = sum(r["needs_review"] == "TRUE" for r in rows)
        print(f"{season}: {len(rows)} entries, {counted} with a count, {review} need review -> {path.relative_to(ROOT)}")
        if not rows:
            print(f"  no dated entries found; open {RAW_DIR / (season + '.html')} and check the date format",
                  file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
