"""Tests for the USGS gauge request. Network-free."""
from datetime import date

import pytest

from roll_call.ingest import usgs_gauge


def test_daily_params_target_water_temperature_daily_mean():
    p = usgs_gauge.build_daily_params(date(2024, 11, 1), date(2025, 3, 31))
    assert p["monitoring_location_id"] == "USGS-02236000"
    assert (p["parameter_code"], p["statistic_id"]) == ("00010", "00003")
    assert p["time"] == "2024-11-01/2025-03-31"


@pytest.mark.skip(reason="TODO(you): save a response (docs/sources.md smoke test 5), implement parse_daily, unskip")
def test_parse_daily_keeps_unit_and_approval_status():
    ...
