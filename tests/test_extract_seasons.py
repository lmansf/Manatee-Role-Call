"""Tests for tools/extract_seasons.py.

The HTML below is SYNTHETIC. No real page could be fetched when this was written. Each entry
sentence is quoted from real Save the Manatee Club report text found via search (listed in
docs/sources.md §2); the markup around them is a guess at a WordPress page. Replace with a real
saved page once one exists.
"""
import importlib.util
import sys
from datetime import date
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "extract_seasons", Path(__file__).resolve().parents[1] / "tools" / "extract_seasons.py")
es = importlib.util.module_from_spec(_spec)
sys.modules["extract_seasons"] = es
_spec.loader.exec_module(es)

SYNTHETIC = """<html><head><title>Manatee Sighting Reports: 2023 – 2024 | Save the Manatee Club</title></head>
<body><nav>Jan 1 menu 999 manatees</nav>
<div class="entry-content">
<p>Get the manatee sighting reports from our Blue Spring researchers for the 2023-2024 winter season.</p>
<p><strong>November 30</strong> – With air temps near 46°F (~8°C), researchers decided to do a roll call.
The river temp was 69.4°F (~20.8°C) with 32 additional manatees counted.</p>
<p><strong>Monday, December 4</strong> – No roll call today; the water was too murky.</p>
<p><strong>Dec. 5</strong> – The river temp was 70.3º F (21.3°C) with 51 manatees for roll call.</p>
<p><strong>January 16</strong> – The river temperature was 64.2°F (17.9°C), with researchers counting
186 manatees while the park counted 191.</p>
<p>Adoptees seen: Lily and Brutus.</p>
<p><strong>January 17</strong> – The river temperature rose to 60.1°F (15.6°C) with a count of
677 manatees by researchers and 687 by the park.</p>
<p><strong>Jan 18</strong> – The river dropped to 63.5F (17.5C) and the manatee count went up to 493.</p>
<p><strong>Jan 19</strong> – Annie + 23 others at roll call.</p>
</div></body></html>"""


def rows():
    return {r["date"]: r for r in es.extract_season("2023-2024", SYNTHETIC, "https://example.test")}


def test_entries_found_and_nav_and_intro_ignored():
    assert list(rows()) == ["2023-11-30", "2023-12-04", "2023-12-05",
                            "2024-01-16", "2024-01-17", "2024-01-18", "2024-01-19"]


def test_two_counts_attributed():
    r = rows()
    assert (r["2024-01-16"]["count_smc"], r["2024-01-16"]["count_park"]) == ("186", "191")
    assert (r["2024-01-17"]["count_smc"], r["2024-01-17"]["count_park"]) == ("677", "687")
    assert r["2024-01-16"]["needs_review"] == "FALSE"
    assert "Lily" in r["2024-01-16"]["entry_text"]


def test_additional_is_not_a_total():
    r = rows()["2023-11-30"]
    assert r["count_smc"] == "" and r["additional"] == "32"
    assert r["air_temp_f"] == "46" and r["river_temp_f"] == "69.4"
    assert r["needs_review"] == "TRUE"


def test_no_count_is_not_zero_and_not_flagged():
    r = rows()["2023-12-04"]
    assert r["no_count"] == "TRUE" and r["count_smc"] == "" and r["needs_review"] == "FALSE"


def test_single_count_and_ordinal_degree_sign():
    r = rows()["2023-12-05"]
    assert r["count_smc"] == "51" and r["river_temp_f"] == "70.3" and r["needs_review"] == "FALSE"


def test_count_verb_without_word_manatees():
    r = rows()["2024-01-18"]
    assert r["count_smc"] == "493" and r["river_temp_f"] == "63.5"


def test_derived_count_flagged_not_computed():
    r = rows()["2024-01-19"]
    assert r["derived"] == "TRUE" and r["needs_review"] == "TRUE"


def test_year_inferred_from_season():
    d, _, reasons = es.parse_date_lead("2023-2024", "February 2 - text")
    assert d == date(2024, 2, 2) and not reasons
    d, _, reasons = es.parse_date_lead("2023-2024", "November 30, 2022 - text")
    assert d == date(2022, 11, 30) and reasons


def test_mislabelled_page_flags_every_row():
    out = es.extract_season("2025-2026", SYNTHETIC, "https://example.test")
    assert all("titled 2023-2024" in r["review_reason"] for r in out)


def test_estimate_phrasing_is_a_count_marked_estimate():
    r = es.extract(es.Entry(date(2025, 12, 31), "Only a low estimate of 477 was possible. The park counted 670."),
                   "2025-2026", "x")
    assert (r["count_smc"], r["count_park"], r["estimate"]) == ("477", "670", "TRUE")


def test_researcher_and_park_counts_in_one_sentence():
    r = es.extract(es.Entry(date(2025, 2, 1),
                            "Park staff counted 34 manatees one morning while a researcher counted 38."),
                   "2024-2025", "x")
    assert (r["count_smc"], r["count_park"]) == ("38", "34")
