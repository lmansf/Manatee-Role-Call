"""Static configuration shared across the pipeline.

Secrets and machine-specific paths come from `.env` in the repo root (ignored by git; see
`.env.example`). Nothing identifying belongs in this file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

# Blue Spring State Park, Orange City, FL. Open-Meteo snaps to its nearest grid cell
# (~11 km), so a few decimal places is plenty.
BLUE_SPRING_LAT = 28.9478
BLUE_SPRING_LON = -81.3398
TIMEZONE = "America/New_York"

# USGS gauge on the St. Johns River near DeLand, ~7 km downstream (docs/sources.md §3).
GAUGE_SITE = "USGS-02236000"
GAUGE_WATER_TEMP_PARAM = "00010"  # water temperature, °C
GAUGE_DAILY_MEAN_STAT = "00003"

# The spring run holds ~72°F (22.2°C) year-round. Manatees gather when the river drops
# below roughly 20°C. Kept here so the model and the checks agree on the number.
SPRING_TEMP_C = 22.2
REFUGE_THRESHOLD_C = 20.0

# Season placeholders (spec §5), to be calibrated from the historical seasons. The season
# itself is not a date range: it opens at the first report and closes after silence.
LATEST_PLAUSIBLE_START = (11, 15)  # month, day
SEASON_CLOSE_NOT_BEFORE = (3, 1)
SEASON_CLOSE_SILENT_WEEKDAYS = 5
COUNT_PLAUSIBLE_RANGE = (0, 1500)

# Database and backup locations. Relative paths are taken from the repo root.
DB_PATH = (REPO_ROOT / os.environ.get("ROLL_CALL_DB", "data/roll_call.duckdb")).resolve()
RECORDS_DIR = REPO_ROOT / "data" / "records"

# The dashboard's data: small summary CSVs committed to the repo and pushed each run. Vercel
# rebuilds the Evidence site in dashboard/ on every push. Publishing refuses any other branch.
DASHBOARD_DATA_DIR = REPO_ROOT / "dashboard" / "sources" / "roll_call"
PUBLISH_BRANCH = os.environ.get("PUBLISH_BRANCH") or "main"


def env(name: str, required: bool = True) -> str | None:
    """Read a setting from the environment (.env already loaded). Fails loudly when a
    required one is missing, so a broken setup shows up as a failed run, not a silent one."""
    value = os.environ.get(name) or None
    if required and value is None:
        raise RuntimeError(f"{name} is not set; add it to {REPO_ROOT / '.env'} (see .env.example)")
    return value


@dataclass(frozen=True)
class Tables:
    """Table names in the DuckDB file. Two layers per source: raw keeps each payload exactly
    as received so a broken parser can be re-run over history; parsed is typed, one row per
    observation."""

    weather_raw: str = "weather_raw"
    weather_daily: str = "weather_daily"
    weather_hourly: str = "weather_hourly"
    weather_forecast: str = "weather_forecast_daily"
    counts_raw: str = "blue_spring_counts_raw"
    counts_daily: str = "blue_spring_counts_daily"
    counts_reference: str = "blue_spring_counts_reference"  # scores the parser; never trains
    gauge_raw: str = "gauge_raw"
    gauge_daily: str = "gauge_daily"
    ingest_runs: str = "ingest_runs"
    # Records only a person can make; exported to CSV and committed (spec §2).
    clearing_decisions: str = "clearing_decisions"
    baseline_refreshes: str = "baseline_refreshes"


TABLES = Tables()
RECORD_TABLES = (TABLES.clearing_decisions, TABLES.baseline_refreshes)
