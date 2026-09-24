"""Season transitions, read from the counts table alone."""
from datetime import date, timedelta

import duckdb
import pytest

from roll_call import config
from roll_call.quality import season

COUNTS_DDL = """
CREATE TABLE blue_spring_counts_daily (
    report_date DATE, count_researchers INTEGER, count_park INTEGER, not_counted BOOLEAN,
    is_estimate BOOLEAN, river_temp_f DOUBLE, spring_temp_f DOUBLE, post_url VARCHAR,
    count_text VARCHAR, run_id VARCHAR, ingested_at TIMESTAMP)
"""


@pytest.fixture
def con(monkeypatch):
    monkeypatch.setattr(config, "LATEST_PLAUSIBLE_START", (11, 15))
    monkeypatch.setattr(config, "SEASON_CLOSE_NOT_BEFORE", (3, 1))
    monkeypatch.setattr(config, "SEASON_CLOSE_SILENT_WEEKDAYS", 5)
    c = duckdb.connect(":memory:")
    c.execute(COUNTS_DDL)
    yield c
    c.close()


def report(con, day, count=50, not_counted=False):
    con.execute(
        "INSERT INTO blue_spring_counts_daily (report_date, count_researchers, not_counted) "
        "VALUES (?, ?, ?)", [day, None if not_counted else count, not_counted])


def weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def test_no_table_means_awaiting():
    c = duckdb.connect(":memory:")
    assert season.state(c, date(2026, 10, 1)).status == season.AWAITING


def test_awaiting_until_the_latest_plausible_start_then_overdue(con):
    assert season.state(con, date(2026, 11, 15)).status == season.AWAITING
    assert season.state(con, date(2026, 11, 16)).status == season.OVERDUE
    # Still overdue in the new year: the winter never opened.
    assert season.state(con, date(2027, 2, 1)).status == season.OVERDUE
    # A new winter starts in August.
    assert season.state(con, date(2027, 8, 2)).status == season.AWAITING


def test_reports_before_august_belong_to_the_last_winter(con):
    report(con, date(2026, 7, 20))
    assert season.state(con, date(2026, 11, 20)).status == season.OVERDUE


def test_first_report_opens_the_season(con):
    report(con, date(2026, 11, 3))
    s = season.state(con, date(2026, 11, 4))
    assert s.status == season.OPEN and s.opened_on == date(2026, 11, 3)
    assert s.winter == 2026
    assert season.is_open(con, date(2026, 11, 4))
    assert season.state(con, date(2026, 11, 2)).status == season.AWAITING


def test_not_counted_report_opens_the_season(con):
    report(con, date(2026, 11, 20), not_counted=True)
    assert season.state(con, date(2026, 11, 21)).status == season.OPEN


def test_row_with_no_count_and_no_not_counted_flag_is_not_a_report(con):
    con.execute("INSERT INTO blue_spring_counts_daily (report_date, not_counted) VALUES (?, false)",
                [date(2026, 11, 3)])
    assert season.state(con, date(2026, 11, 4)).status == season.AWAITING


def test_silence_before_march_does_not_close(con):
    report(con, date(2026, 11, 3))
    report(con, date(2027, 1, 4))  # two months of silence in winter
    assert season.state(con, date(2027, 2, 26)).status == season.OPEN


def test_closes_after_five_silent_weekdays_in_march(con):
    for d in weekdays(date(2026, 11, 2), date(2027, 3, 5)):
        report(con, d)
    # Last report Friday 2027-03-05. Silent weekdays: 8, 9, 10, 11, 12 March.
    assert season.state(con, date(2027, 3, 12)).status == season.OPEN  # today is not yet silent
    s = season.state(con, date(2027, 3, 13))
    assert s.status == season.CLOSED
    assert s.closed_on == date(2027, 3, 12)
    assert s.last_report == date(2027, 3, 5)


def test_weekends_never_count_as_silence(con):
    for d in weekdays(date(2026, 11, 2), date(2027, 3, 31)):
        report(con, d)
    # Every weekend in March is unreported; the season stays open.
    assert season.state(con, date(2027, 4, 1)).status == season.OPEN


def test_silence_straddling_march_counts_only_march_weekdays(con):
    report(con, date(2026, 11, 2))
    report(con, date(2027, 2, 22))  # Monday; silent from here on
    # Weekdays from 1 March: Mon 1 to Fri 5 March 2027. Closes on the fifth.
    assert season.state(con, date(2027, 3, 5)).status == season.OPEN
    s = season.state(con, date(2027, 3, 6))
    assert s.status == season.CLOSED and s.closed_on == date(2027, 3, 5)


def test_a_late_report_does_not_reopen_a_closed_season(con):
    report(con, date(2026, 11, 2))
    report(con, date(2027, 3, 1))
    report(con, date(2027, 4, 20))
    s = season.state(con, date(2027, 4, 21))
    assert s.status == season.CLOSED and s.closed_on == date(2027, 3, 8)
