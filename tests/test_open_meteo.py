"""Tests for the Open-Meteo parser. Network-free; uses saved fixtures."""
from datetime import date

import pytest

from roll_call.ingest import open_meteo


def test_archive_params_cover_window():
    p = open_meteo.build_archive_params(date(2024, 1, 1), date(2024, 1, 31))
    assert p["start_date"] == "2024-01-01"
    assert p["end_date"] == "2024-01-31"
    assert "temperature_2m_max" in p["daily"]
    assert p["hourly"] == "temperature_2m"
    assert p["temperature_unit"] == "celsius"


def test_forecast_params_never_request_past_days():
    p = open_meteo.build_forecast_params()
    assert "past_days" not in p
    assert p["forecast_days"] == 7


@pytest.mark.skip(reason="TODO(you): implement parse_daily, then save a fixture and unskip")
def test_parse_daily_one_row_per_day():
    ...


@pytest.mark.skip(reason="TODO(you): misaligned arrays must raise, not silently truncate")
def test_parse_daily_rejects_misaligned_arrays():
    ...
