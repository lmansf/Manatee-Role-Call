"""Static configuration shared across the pipeline.

Keep this file free of secrets. Anything that identifies a person or an account
(service-account emails, sheet IDs, API keys) belongs in Databricks secrets, not here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Blue Spring State Park, Orange City, FL. Open-Meteo snaps to its nearest grid cell
# (~11 km), so a few decimal places is plenty.
BLUE_SPRING_LAT = 28.9478
BLUE_SPRING_LON = -81.3398
TIMEZONE = "America/New_York"

# Spring run holds ~72°F (22.2°C) year-round. Manatees start aggregating when the
# St. Johns River drops below roughly 20°C. Kept here so the model and the quality
# checks agree on the number.
SPRING_TEMP_C = 22.2
REFUGE_THRESHOLD_C = 20.0

# Manatee season at Blue Spring, roughly. Used by the freshness checks so that
# "no count today" in July is expected rather than a failure.
# TODO(you): decide whether these are fixed month/day boundaries or come from the
# source itself (the first and last sighting report each winter). Fixed is simpler;
# source-derived means the freshness check can't be fooled by a season that starts late.
SEASON_START_MONTH_DAY = (11, 1)
SEASON_END_MONTH_DAY = (3, 15)


def in_season(d: date) -> bool:
    """True when `d` falls inside manatee season (season straddles the new year)."""
    raise NotImplementedError  # TODO(you)


# Delta table names. Two layers per source: raw keeps the payload exactly as received
# so a broken parser can be re-run later; parsed is the typed, one-row-per-observation table.
@dataclass(frozen=True)
class Tables:
    catalog: str = "workspace"
    schema: str = "roll_call"

    def _q(self, name: str) -> str:
        return f"{self.catalog}.{self.schema}.{name}"

    @property
    def weather_raw(self) -> str:
        return self._q("weather_raw")

    @property
    def weather_daily(self) -> str:
        return self._q("weather_daily")

    @property
    def weather_hourly(self) -> str:
        return self._q("weather_hourly")

    @property
    def weather_forecast(self) -> str:
        return self._q("weather_forecast_daily")

    @property
    def counts_raw(self) -> str:
        return self._q("blue_spring_counts_raw")

    @property
    def counts_daily(self) -> str:
        return self._q("blue_spring_counts_daily")

    @property
    def ingest_runs(self) -> str:
        return self._q("ingest_runs")


TABLES = Tables()
