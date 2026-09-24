"""Tests for the Blue Spring report parser. Network-free; uses saved fixtures."""
import pytest

from roll_call.ingest import blue_spring


@pytest.mark.skip(reason="TODO(owner): save a real report to tests/fixtures first")
def test_parse_report_extracts_count_and_date():
    ...


@pytest.mark.skip(reason="TODO(owner): 'no count today' must yield count=None, not 0")
def test_parse_report_no_count_is_none():
    ...
