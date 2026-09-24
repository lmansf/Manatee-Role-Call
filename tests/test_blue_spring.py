"""Tests for the Blue Spring report parser and fetcher. Network-free.

The page fixtures (tests/fixtures/stmc_*_synthetic.html) are SYNTHETIC: real report sentences
from docs/sources.md section 2 inside guessed WordPress markup. Replace them with saved pages
once the site can be reached.
"""
from datetime import date
from pathlib import Path

import pytest
import requests

from roll_call.ingest import blue_spring
from roll_call.ingest.blue_spring import Entry, SightingReport

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HUB = (FIXTURES / "stmc_hub_synthetic.html").read_text(encoding="utf-8")
SEASON_PAGE = (FIXTURES / "stmc_season_2023_2024_synthetic.html").read_text(encoding="utf-8")
HUB_URL = blue_spring.LISTING_URL

RECORD_KEYS = {
    "report_date", "count_researchers", "count_park", "not_counted", "is_estimate",
    "river_temp_f", "spring_temp_f", "post_url", "count_text",
}


def hub_reports(season: str = "2025-2026") -> dict[date, SightingReport]:
    return {r.report_date: r for r in blue_spring.parse_page(HUB, HUB_URL, season)}


# ---------------------------------------------------------------- page parsing


def test_page_splits_into_one_report_per_dated_entry():
    reports = blue_spring.parse_page(HUB, HUB_URL, "2025-2026")
    assert [r.report_date for r in reports] == [
        date(2025, 12, 29), date(2025, 12, 30), date(2025, 12, 31),
        date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 5),
    ]
    assert all(r.post_url == HUB_URL for r in reports)


def test_nav_and_intro_are_not_entries():
    # The nav holds "Jan 5 ... 999 manatees"; it must not become an entry or a count.
    reports = blue_spring.parse_page(HUB, HUB_URL, "2025-2026")
    assert all(r.count_researchers != 999 for r in reports)
    assert sum(r.report_date == date(2026, 1, 5) for r in reports) == 1


def test_single_count_with_sentence_kept():
    r = hub_reports()[date(2025, 12, 29)]
    assert (r.count_researchers, r.count_park, r.not_counted, r.is_estimate) == (51, None, False, False)
    assert r.river_temp_f == 70.3
    assert r.count_text == "The river temp was 70.3° F (21.3°C) with 51 manatees for roll call."


def test_both_counts_attributed():
    r = hub_reports()[date(2026, 1, 2)]
    assert (r.count_researchers, r.count_park) == (186, 191)
    assert r.river_temp_f == 64.2
    assert "researchers counting 186 manatees while the park counted 191" in r.count_text
    # The continuation paragraph belongs to the entry but is not the count sentence.
    assert "Lily" not in r.count_text


def test_estimate_is_a_count_marked_estimate():
    r = hub_reports()[date(2025, 12, 31)]
    assert (r.count_researchers, r.count_park, r.is_estimate) == (477, 670, True)
    # The two counts sit in two sentences; both are kept, in order.
    assert r.count_text == "Only a low estimate of 477 was possible. The park counted 670."


def test_not_counted_is_not_zero():
    r = hub_reports()[date(2025, 12, 30)]
    assert r.not_counted is True
    assert r.count_researchers is None and r.count_park is None
    assert r.count_text == "No roll call today; the water was too murky."


def test_additional_manatees_are_not_a_count():
    r = hub_reports()[date(2026, 1, 3)]
    assert r.count_researchers is None and r.count_park is None
    assert r.not_counted is False
    assert r.river_temp_f == 69.4
    assert r.count_text is None


def test_says_no_count_but_has_a_number_keeps_the_count():
    r = blue_spring.report_from_entry(Entry(date(2025, 1, 10), "No roll call today, but the park counted 40."), "u")
    assert (r.count_park, r.not_counted) == (40, False)


def test_spring_temperature_but_not_blue_spring():
    text = "At Blue Spring the river was 61.2°F (16.2°C). The spring stays at 72°F. 300 manatees counted."
    r = blue_spring.report_from_entry(Entry(date(2025, 1, 10), text), "u")
    assert (r.river_temp_f, r.spring_temp_f, r.count_researchers) == (61.2, 72.0, 300)
    r = blue_spring.report_from_entry(Entry(date(2025, 1, 10), "Blue Spring river temp 61°F, 20 manatees."), "u")
    assert r.spring_temp_f is None


def test_season_page_uses_its_own_title_season():
    reports = {r.report_date: r for r in blue_spring.parse_page(SEASON_PAGE, "https://example.test/s", "2023-2024")}
    assert list(reports) == [date(2023, 11, 30), date(2024, 1, 16), date(2024, 1, 17), date(2024, 2, 1)]
    assert (reports[date(2024, 1, 17)].count_researchers, reports[date(2024, 1, 17)].count_park) == (677, 687)
    assert (reports[date(2024, 2, 1)].count_researchers, reports[date(2024, 2, 1)].count_park) == (38, 34)
    # A temperature-only entry is neither a count nor "not counted".
    nov = reports[date(2023, 11, 30)]
    assert (nov.count_researchers, nov.not_counted, nov.river_temp_f) == (None, False, 69.6)
    assert blue_spring.infer_season(SEASON_PAGE, date(2026, 9, 24)) == "2023-2024"


def test_to_record_has_exactly_the_table_columns():
    assert set(hub_reports()[date(2026, 1, 2)].to_record()) == RECORD_KEYS


# ---------------------------------------------------------------- year inference


def test_season_for_is_july_to_june():
    assert blue_spring.season_for(date(2025, 7, 1)) == "2025-2026"
    assert blue_spring.season_for(date(2026, 6, 30)) == "2025-2026"
    assert blue_spring.season_for(date(2026, 9, 24)) == "2026-2027"


def test_year_inference_across_new_year():
    season = blue_spring.infer_season(HUB, date(2026, 1, 6))
    assert season == "2025-2026"
    dates = [r.report_date for r in blue_spring.parse_page(HUB, HUB_URL, season)]
    assert date(2025, 12, 31) in dates and date(2026, 1, 2) in dates


def test_hub_still_showing_last_season_after_july():
    # Fetched in September, before the new winter's first report: the fetch-date season
    # (2026-2027) would put every entry in the future, so the previous season is used.
    assert blue_spring.infer_season(HUB, date(2026, 9, 24)) == "2025-2026"


def test_one_per_date_prefers_the_entry_with_a_count():
    def rep(d, n):
        return SightingReport(d, n, None, n is None, False, None, None, "u", None)

    kept = blue_spring.one_per_date([rep(date(2026, 1, 2), None), rep(date(2026, 1, 1), 5), rep(date(2026, 1, 2), 9)])
    assert [(r.report_date, r.count_researchers) for r in kept] == [(date(2026, 1, 1), 5), (date(2026, 1, 2), 9)]


# ---------------------------------------------------------------- fetch_reports


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


@pytest.fixture
def fetched_in_january(monkeypatch):
    monkeypatch.setattr(blue_spring, "_today", lambda: date(2026, 1, 6))


def test_fetch_reports_with_fake_session(fetched_in_january):
    session = FakeSession(FakeResponse(HUB))
    result = blue_spring.fetch_reports(session=session)

    (call,) = session.calls
    assert call["url"] == blue_spring.LISTING_URL
    assert call["headers"]["User-Agent"] == blue_spring.USER_AGENT
    assert call["timeout"] > 0

    assert result.raw == HUB
    assert result.run.source == "blue_spring_counts"
    assert result.run.status == "succeeded"
    assert result.run.row_count == 6
    assert result.run.payload_sha256 == blue_spring.sha256_text(HUB)
    assert [r["report_date"] for r in result.records][0] == date(2025, 12, 29)
    assert all(set(r) == RECORD_KEYS for r in result.records)


def test_fetch_reports_since_filter(fetched_in_january):
    result = blue_spring.fetch_reports(since=date(2026, 1, 2), session=FakeSession(FakeResponse(HUB)))
    assert [r["report_date"] for r in result.records] == [date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 5)]
    assert result.run.row_count == 3


def test_fetch_reports_http_error_raises(fetched_in_january):
    with pytest.raises(requests.HTTPError):
        blue_spring.fetch_reports(session=FakeSession(FakeResponse("gone", status_code=503)))
