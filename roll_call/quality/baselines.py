"""Weather baselines: the normal for each day of the year, refreshed once a year.

A baseline is the fixed reference a check compares against. For weather it is the mean and
standard deviation of each measure for each day of the year over the trailing 30 years. It is
replaced once a year, after the season closes, and never mid-season: a mid-season refresh would
absorb the cold snaps the checks watch for.

Every refresh logs each measure's season-average normal (November to March) before and after,
side by side in baseline_refreshes. Without that log a yearly refresh quietly becomes a rolling
window. A replayed refresh applies the same rule to a past year, using only data from before
that year's refresh date, and is labelled as replayed.

This module owns the DDL for baselines, baseline_refreshes and clearing_decisions.
"""
from __future__ import annotations

import logging
from datetime import date

import duckdb

from roll_call import config

log = logging.getLogger(__name__)

BASELINES_TABLE = "baselines"
REFRESHES_TABLE = config.TABLES.baseline_refreshes
DECISIONS_TABLE = config.TABLES.clearing_decisions

WEATHER_SOURCE = "open_meteo_archive"
WEATHER_MEASURES = (
    "temperature_2m_min",
    "temperature_2m_max",
    "temperature_2m_mean",
    "precipitation_sum",
    "wind_speed_10m_max",
    "shortwave_radiation_sum",
)

TRAILING_YEARS = 30
# The window is centred on each day: 7 days either side plus the day itself.
SMOOTHING_HALF_WIDTH = 7
DAYS_IN_YEAR = 365
# 29 February shares 28 February's day of the year (59), so every year has 365 days.
LEAP_DAY_FOLDED_INTO = 59
# The season-average normal averages the days from 1 November (day 305) to 31 March (day 90).
SEASON_FIRST_DAY = 305
SEASON_LAST_DAY = 90
# A past year has no recorded season close, so a replayed refresh uses this nominal date.
# Seasons close after a run of silent weekdays once March has begun, so by 1 April most have.
REPLAY_REFRESH_MONTH_DAY = (4, 1)

DDL = (
    f"""
    CREATE TABLE IF NOT EXISTS {BASELINES_TABLE} (
        source       VARCHAR NOT NULL,
        measure      VARCHAR NOT NULL,
        day_of_year  INTEGER NOT NULL,
        mean         DOUBLE,
        sd           DOUBLE,
        refreshed_on DATE NOT NULL,
        PRIMARY KEY (source, measure, day_of_year)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {REFRESHES_TABLE} (
        refreshed_on DATE NOT NULL,
        replayed     BOOLEAN NOT NULL,
        source       VARCHAR NOT NULL,
        measure      VARCHAR NOT NULL,
        old_value    DOUBLE,
        new_value    DOUBLE,
        PRIMARY KEY (refreshed_on, source, measure)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {DECISIONS_TABLE} (
        obs_id     VARCHAR PRIMARY KEY,
        decision   VARCHAR NOT NULL,
        reason     VARCHAR NOT NULL,
        decided_at TIMESTAMP NOT NULL  -- UTC
    )
    """,
)


def ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create the baselines, refresh log and clearing decisions tables if they are missing."""
    for ddl in DDL:
        con.execute(ddl)


def day_of_year(day: date) -> int:
    """The day of the year in a 365-day year. 29 February folds into day 59, 28 February."""
    if day.month == 2 and day.day == 29:
        return LEAP_DAY_FOLDED_INTO
    return date(2001, day.month, day.day).timetuple().tm_yday


# The same fold in SQL: map each date onto a non-leap year before taking its day of the year.
_DAY_OF_YEAR_SQL = (
    "dayofyear(make_date(2001, month(obs_date), "
    "CASE WHEN month(obs_date) = 2 AND day(obs_date) = 29 THEN 28 ELSE day(obs_date) END))"
)


def normal(con: duckdb.DuckDBPyConnection, source: str, measure: str,
           day: date) -> tuple[float, float] | None:
    """The (mean, sd) normal for that measure on that day of the year, or None without one."""
    if not table_exists(con, BASELINES_TABLE):
        return None
    row = con.execute(
        f"SELECT mean, sd FROM {BASELINES_TABLE} "
        "WHERE source = ? AND measure = ? AND day_of_year = ?",
        [source, measure, day_of_year(day)],
    ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    return float(row[0]), float(row[1])


def compute(con: duckdb.DuckDBPyConnection, measure: str,
            refreshed_on: date) -> list[tuple[int, float, float]]:
    """The smoothed (day_of_year, mean, sd) rows for one weather measure.

    Only observations from the 30 years before refreshed_on count; the refresh date itself is
    excluded. Each day of the year first gets the mean and variance of its own observations.
    Each day then takes the average of those means, and the square root of the average of those
    variances, over the 15 days centred on it. The window wraps from 31 December to 1 January.
    A day needs at least one neighbour with two or more observations, or it gets no row.
    """
    if measure not in WEATHER_MEASURES:
        raise ValueError(f"unknown weather measure {measure!r}")
    if not table_exists(con, config.TABLES.weather_daily):
        return []
    rows = con.execute(
        f"""
        WITH obs AS (
            SELECT {_DAY_OF_YEAR_SQL} AS doy, {measure} AS v
            FROM {config.TABLES.weather_daily}
            WHERE obs_date >= CAST(? AS DATE) - INTERVAL {TRAILING_YEARS} YEAR
              AND obs_date < CAST(? AS DATE)
              AND {measure} IS NOT NULL
        ),
        daily AS (
            SELECT doy, avg(v) AS m, var_samp(v) AS var FROM obs GROUP BY doy
        ),
        days AS (
            SELECT CAST(range AS INTEGER) AS doy FROM range(1, {DAYS_IN_YEAR + 1})
        )
        SELECT d.doy, avg(daily.m) AS mean, sqrt(avg(daily.var)) AS sd
        FROM days d
        JOIN daily
          ON least(abs(d.doy - daily.doy), {DAYS_IN_YEAR} - abs(d.doy - daily.doy))
             <= {SMOOTHING_HALF_WIDTH}
        GROUP BY d.doy
        HAVING avg(daily.var) IS NOT NULL
        ORDER BY d.doy
        """,
        [refreshed_on, refreshed_on],
    ).fetchall()
    return [(int(doy), float(mean), float(sd)) for doy, mean, sd in rows]


def season_average(rows: list[tuple[int, float, float]]) -> float | None:
    """The mean normal over the season's days, 1 November to 31 March."""
    means = [m for doy, m, _ in rows if doy >= SEASON_FIRST_DAY or doy <= SEASON_LAST_DAY]
    return sum(means) / len(means) if means else None


def refresh(con: duckdb.DuckDBPyConnection, refreshed_on: date,
            replayed: bool = False) -> list[dict]:
    """Recompute every weather baseline as of refreshed_on and log old against new.

    The old value is the season-average normal of the latest refresh logged before this one,
    or None for the first. The baselines table takes the new rows unless it already holds a
    later refresh, so a replay of a past year never overwrites the baseline in force. A measure
    with too little data keeps its old baseline and is not logged.

    Returns one dict per logged measure: source, measure, old_value and new_value.
    """
    ensure_tables(con)
    logged = []
    con.begin()
    try:
        for measure in WEATHER_MEASURES:
            rows = compute(con, measure, refreshed_on)
            if not rows:
                log.info("no data for %s before %s; baseline not refreshed", measure, refreshed_on)
                continue
            old = con.execute(
                f"SELECT new_value FROM {REFRESHES_TABLE} "
                "WHERE source = ? AND measure = ? AND refreshed_on < ? "
                "ORDER BY refreshed_on DESC LIMIT 1",
                [WEATHER_SOURCE, measure, refreshed_on],
            ).fetchone()
            entry = {
                "source": WEATHER_SOURCE,
                "measure": measure,
                "old_value": old[0] if old else None,
                "new_value": season_average(rows),
            }
            con.execute(
                f"INSERT OR REPLACE INTO {REFRESHES_TABLE} VALUES (?, ?, ?, ?, ?, ?)",
                [refreshed_on, replayed, WEATHER_SOURCE, measure,
                 entry["old_value"], entry["new_value"]],
            )
            in_force = con.execute(
                f"SELECT max(refreshed_on) FROM {BASELINES_TABLE} WHERE source = ? AND measure = ?",
                [WEATHER_SOURCE, measure],
            ).fetchone()[0]
            if in_force is None or in_force <= refreshed_on:
                con.execute(
                    f"DELETE FROM {BASELINES_TABLE} WHERE source = ? AND measure = ?",
                    [WEATHER_SOURCE, measure],
                )
                con.executemany(
                    f"INSERT INTO {BASELINES_TABLE} VALUES (?, ?, ?, ?, ?, ?)",
                    [[WEATHER_SOURCE, measure, doy, m, sd, refreshed_on] for doy, m, sd in rows],
                )
            logged.append(entry)
        con.commit()
    except Exception:
        con.rollback()
        raise
    log.info("%s baseline refresh on %s: %d measures",
             "replayed" if replayed else "live", refreshed_on, len(logged))
    return logged


def refresh_if_due(con: duckdb.DuckDBPyConnection, today: date) -> bool:
    """Refresh once after the season closes. Returns True when it refreshed.

    Due means the season is closed, it has a close date on or before today, and no live refresh
    is logged on or after that close. The log is the memory, so a second call for the same
    close does nothing. A refresh that found no data logs nothing and is tried again next day.

    Reads the season from `roll_call.quality.season.state`, expecting `is_open` and `closed_on`
    (the date of the latest close, or None before any).
    """
    from roll_call.quality import season  # imported here: the checks agent owns it

    state = season.state(con, today)
    if getattr(state, "is_open", False):
        return False
    closed_on = getattr(state, "closed_on", None)
    if closed_on is None or closed_on > today:
        return False
    ensure_tables(con)
    done = con.execute(
        f"SELECT count(*) FROM {REFRESHES_TABLE} WHERE NOT replayed AND refreshed_on >= ?",
        [closed_on],
    ).fetchone()[0]
    if done:
        return False
    return bool(refresh(con, today))


def replay_date(year: int) -> date:
    """The nominal refresh date of a replayed year."""
    return date(year, *REPLAY_REFRESH_MONTH_DAY)


def replay(con: duckdb.DuckDBPyConnection, first_year: int, last_year: int) -> list[date]:
    """Replay one refresh per year from first_year to last_year, oldest first.

    Each uses only data from before its own refresh date and is labelled replayed. A year that
    already has a live refresh on that date is left alone. Returns the dates that logged.
    """
    ensure_tables(con)
    done = []
    for year in range(first_year, last_year + 1):
        when = replay_date(year)
        live = con.execute(
            f"SELECT count(*) FROM {REFRESHES_TABLE} WHERE refreshed_on = ? AND NOT replayed",
            [when],
        ).fetchone()[0]
        if live:
            continue
        if refresh(con, when, replayed=True):
            done.append(when)
    return done


def table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    return bool(row and row[0])
