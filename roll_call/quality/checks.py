"""The daily checks (spec §5): freshness, volume, distribution, counts, cross-source and join
retention.

A check either judges a source or judges one observation.

- A failed source-level check opens an incident. It closes on the first run where the same
  check passes. A check with nothing to look at (no rows yet) leaves its incident as it is.
- A doubted observation is quarantined until a person clears it. Counts are never judged
  by standard deviations (ADR 0001): only by a hard range and by jumps that contradict the
  river temperature. Weather values are judged against the normal for their day of the year.
- Counter disagreement is a measure, stored in the measures table and never quarantined.
- Join retention is the share of counted report dates that have a gauge value, and the share
  that have archive weather, for the same date. Both shares are stored as measures, and a low
  share opens an incident on the counts source.

Every threshold below is a placeholder. Calibrate them from the historical seasons (spec §8)
before trusting an alert.
"""
from __future__ import annotations

import importlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

import duckdb

from roll_call import config
from roll_call.quality import season as season_mod
from roll_call.quality import store

T = config.TABLES

ARCHIVE = "open_meteo_archive"
FORECAST = "open_meteo_forecast"
COUNTS = "blue_spring_counts"
GAUGE = "usgs_gauge_daily"
SOURCES = (ARCHIVE, FORECAST, COUNTS, GAUGE)

# Parsed tables per source, each with its natural key.
SOURCE_TABLES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    ARCHIVE: ((T.weather_daily, ("obs_date",)), (T.weather_hourly, ("obs_ts",))),
    FORECAST: ((T.weather_forecast, ("issue_date", "target_date")),),
    COUNTS: ((T.counts_daily, ("report_date",)),),
    GAUGE: ((T.gauge_daily, ("obs_date", "approval_status")),),
}

WEATHER_FIELDS = (
    "temperature_2m_min",
    "temperature_2m_max",
    "temperature_2m_mean",
    "precipitation_sum",
    "wind_speed_10m_max",
    "shortwave_radiation_sum",
)

# The units Open-Meteo reports for the pinned request parameters (docs/sources.md §1).
PINNED_DAILY_UNITS = {
    "temperature_2m_min": "°C",
    "temperature_2m_max": "°C",
    "temperature_2m_mean": "°C",
    "precipitation_sum": "mm",
    "wind_speed_10m_max": "km/h",
    "shortwave_radiation_sum": "MJ/m²",
}
PINNED_HOURLY_UNIT = "°C"
# USGS writes Celsius as "degC". The other spellings are accepted so a parser choice does not
# read as drift. Verify against a saved gauge response.
PINNED_GAUGE_UNITS = frozenset({"degC", "deg C", "°C"})

# --- Placeholder thresholds: calibrate from the historical seasons. ---
# How far back the value-level checks look on each run. Overlap is harmless: quarantine is
# idempotent. Long enough to cover the archive's lag and a missed week of runs.
LOOKBACK_DAYS = 21
# Freshness: days since the last successful run before a source counts as stale.
FRESHNESS_MAX_AGE_DAYS = {ARCHIVE: 2, FORECAST: 2, GAUGE: 2}
# Freshness for counts: weekdays since the last successful run, checked only in season.
COUNTS_FRESHNESS_MAX_WEEKDAYS = 2
# Volume: (min, max) rows expected from one run; None leaves that side open. Counts have no
# expectation: most runs in season find zero or one new report.
VOLUME_EXPECTED_ROWS: dict[str, tuple[int | None, int | None]] = {
    ARCHIVE: (1, None),
    FORECAST: (7, 7),
    GAUGE: (1, None),
}
# Null rate: the archive leaves its last ~5 days null by design, so the window ends before them.
ARCHIVE_LAG_DAYS = config.ARCHIVE_LAG_DAYS
NULL_RATE_WINDOW_DAYS = 14
NULL_RATE_MAX = 0.10
# Weather values further than this many standard deviations from the day's normal.
WEATHER_SD_LIMIT = 3.0
# A count jump: a rise or fall of more than this share of the previous count.
JUMP_RATIO = 0.5
# Ignore jumps smaller than this many manatees; early-season counts swing by a few animals.
JUMP_MIN_ABSOLUTE = 10
# Only compare counts this many days apart or closer (covers a weekend).
JUMP_MAX_GAP_DAYS = 4
# Cross-source: report temperature minus gauge temperature, against its trailing median.
CROSS_SOURCE_MAX_SHIFT_C = 2.0
CROSS_SOURCE_TRAILING = 30
CROSS_SOURCE_MIN_TRAILING = 5
# Join retention: the least share of counted report dates that must have a gauge value, and
# archive weather, for the same date.
JOIN_RETENTION_MIN_SHARE = 0.9
# Fewer report dates than this give a share too noisy to judge: one miss in two reads as 50%.
# The share is still stored; the incident waits until there are enough dates.
JOIN_RETENTION_MIN_DAYS = 5


@dataclass
class CheckReport:
    """What one run of the checks found. `con` lets the alert stamp alerted_at."""

    today: date
    new_incidents: list[dict[str, Any]] = field(default_factory=list)
    closed_incidents: list[dict[str, Any]] = field(default_factory=list)
    new_quarantine: list[dict[str, Any]] = field(default_factory=list)
    measures: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    season: season_mod.SeasonState | None = None
    con: Any = field(default=None, repr=False, compare=False)


# --- Shared helpers ---


def _f_to_c(f: float | None) -> float | None:
    return None if f is None else (f - 32.0) * 5.0 / 9.0


def _weekdays_between(after: date, through: date) -> int:
    """Weekdays d with after < d <= through."""
    n, d = 0, after + timedelta(days=1)
    while d <= through:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def _latest_success(con: duckdb.DuckDBPyConnection, source: str) -> tuple | None:
    """(run_id, started_at, row_count) of the source's latest successful run, or None."""
    if not store.table_exists(con, T.ingest_runs):
        return None
    return con.execute(
        f"""SELECT run_id, started_at, row_count FROM {T.ingest_runs}
            WHERE source = ? AND status = 'succeeded' ORDER BY started_at DESC LIMIT 1""",
        [source],
    ).fetchone()


def _normal(con, source: str, measure: str, day: date) -> tuple[float, float] | None:
    """The baseline normal (mean, sd). Resolved at call time so tests can swap it."""
    baselines = importlib.import_module("roll_call.quality.baselines")
    return baselines.normal(con, source, measure, day)


def _gauge_temps(con, start: date, end: date) -> dict[date, float]:
    """Gauge temperature per day. An approved value wins over a provisional one."""
    if not store.table_exists(con, T.gauge_daily):
        return {}
    rows = con.execute(
        f"""SELECT obs_date, water_temp_c FROM {T.gauge_daily}
            WHERE obs_date BETWEEN ? AND ? AND water_temp_c IS NOT NULL
            ORDER BY obs_date, CASE WHEN lower(approval_status) = 'approved' THEN 1 ELSE 0 END""",
        [start, end],
    ).fetchall()
    return {d: v for d, v in rows}  # later rows (approved) overwrite earlier ones


class _Run:
    """Collects outcomes into the report and records them in the tables."""

    def __init__(self, con, today: date):
        self.con = con
        self.report = CheckReport(today=today, con=con)
        self.errored: set[str] = set()

    def settle(self, source: str, check_name: str, failed: bool, detail: str = "") -> None:
        if failed:
            incident, created = store.open_incident(self.con, source, check_name, detail)
            if created:
                self.report.new_incidents.append(incident)
        else:
            self.report.closed_incidents.extend(store.close_incidents(self.con, source, check_name))

    def quarantine(self, source: str, table: str, key: str, day: date, check_name: str,
                   value: Any) -> None:
        row, created = store.quarantine_observation(self.con, source, table, key, day,
                                                    check_name, value)
        if created:
            self.report.new_quarantine.append(row)

    def guard(self, source: str, name: str, fn: Callable[[], None]) -> None:
        """Run one check. A query that fails on a missing or renamed column is schema drift:
        it marks the source for a `schema` incident instead of stopping the other checks."""
        try:
            fn()
        except duckdb.Error as exc:
            self.errored.add(source)
            self.settle(source, "schema", True,
                        f"{name} could not run: {type(exc).__name__}: {str(exc)[:200]}")


# --- Freshness ---


def check_freshness(run: _Run, state: season_mod.SeasonState) -> None:
    today = run.report.today
    for source in SOURCES:
        latest = _latest_success(run.con, source)
        last_day = latest[1].date() if latest else None
        if source == COUNTS:
            if state.status not in (season_mod.OPEN, season_mod.OVERDUE):
                run.settle(source, "freshness", False)
                continue
            if last_day is None:
                run.settle(source, "freshness", True, "no successful run")
                continue
            silent = _weekdays_between(last_day, today)
            run.settle(source, "freshness", silent > COUNTS_FRESHNESS_MAX_WEEKDAYS,
                       f"last successful run {last_day}, {silent} weekdays ago")
            continue
        if last_day is None:
            run.settle(source, "freshness", True, "no successful run")
            continue
        age = (today - last_day).days
        run.settle(source, "freshness", age > FRESHNESS_MAX_AGE_DAYS[source],
                   f"last successful run {last_day}, {age} days ago")

    overdue = state.status == season_mod.OVERDUE
    run.settle(COUNTS, "season_overdue", overdue,
               f"no report since {season_mod.season_anchor(today)}; latest plausible start "
               f"{state.latest_plausible_start} has passed")


# --- Volume ---


def check_volume(run: _Run) -> None:
    for source, (low, high) in VOLUME_EXPECTED_ROWS.items():
        latest = _latest_success(run.con, source)
        if latest is None or latest[2] is None:
            continue
        rows = latest[2]
        failed = (low is not None and rows < low) or (high is not None and rows > high)
        expected = " and ".join(p for p in (low is not None and f"at least {low}",
                                            high is not None and f"at most {high}") if p)
        run.settle(source, "volume", failed,
                   f"latest run {latest[0]} returned {rows} rows, expected {expected}")


def check_duplicates(run: _Run) -> None:
    for source, tables in SOURCE_TABLES.items():
        run.guard(source, "duplicates", lambda: _duplicates(run, source, tables))


def _duplicates(run: _Run, source: str, tables) -> None:
    found, looked = [], False
    for table, key in tables:
        if not store.table_exists(run.con, table):
            continue
        looked = True
        cols = ", ".join(key)
        n = run.con.execute(
            f"SELECT count(*) FROM (SELECT {cols} FROM {table} GROUP BY {cols} HAVING count(*) > 1)"
        ).fetchone()[0]
        if n:
            found.append(f"{table}: {n} duplicated keys")
    if looked:
        run.settle(source, "duplicates", bool(found), "; ".join(found))


# --- Distribution ---


def _null_rates(con, table: str, columns: tuple[str, ...], where: str,
                params: list) -> tuple[int, dict[str, float]]:
    exprs = ", ".join(f"avg(CASE WHEN {c} IS NULL THEN 1.0 ELSE 0.0 END)" for c in columns)
    row = con.execute(f"SELECT count(*), {exprs} FROM {table} WHERE {where}", params).fetchone()
    return row[0], dict(zip(columns, row[1:]))


def _settle_null_rates(run: _Run, source: str, total: int, rates: dict[str, float],
                       where_desc: str) -> None:
    if not total:
        return
    for column, rate in rates.items():
        run.settle(source, f"null_rate:{column}", rate > NULL_RATE_MAX,
                   f"{rate:.0%} null in {total} rows ({where_desc}), limit {NULL_RATE_MAX:.0%}")


def check_null_rates(run: _Run) -> None:
    con, today = run.con, run.report.today
    end = today - timedelta(days=ARCHIVE_LAG_DAYS)
    start = end - timedelta(days=NULL_RATE_WINDOW_DAYS)
    desc = f"{start} to {end - timedelta(days=1)}"

    def archive_daily() -> None:
        if store.table_exists(con, T.weather_daily):
            total, rates = _null_rates(con, T.weather_daily, WEATHER_FIELDS,
                                       "obs_date >= ? AND obs_date < ?", [start, end])
            _settle_null_rates(run, ARCHIVE, total, rates, desc)

    def archive_hourly() -> None:
        if store.table_exists(con, T.weather_hourly):
            total, rates = _null_rates(con, T.weather_hourly, ("temperature_2m",),
                                       "CAST(obs_ts AS DATE) >= ? AND CAST(obs_ts AS DATE) < ?",
                                       [start, end])
            _settle_null_rates(run, ARCHIVE, total, rates, desc)

    def forecast() -> None:
        if store.table_exists(con, T.weather_forecast):
            issue = con.execute(f"SELECT max(issue_date) FROM {T.weather_forecast}").fetchone()[0]
            if issue is not None:
                total, rates = _null_rates(con, T.weather_forecast, WEATHER_FIELDS,
                                           "issue_date = ?", [issue])
                _settle_null_rates(run, FORECAST, total, rates, f"issued {issue}")

    run.guard(ARCHIVE, "null rate (daily)", archive_daily)
    run.guard(ARCHIVE, "null rate (hourly)", archive_hourly)
    run.guard(FORECAST, "null rate", forecast)


def _unit_problems(units_json: str | None) -> list[str]:
    try:
        units = json.loads(units_json) if units_json else None
    except (TypeError, ValueError):
        units = None
    if not isinstance(units, dict):
        return ["units missing or unreadable"]
    return [f"{f} is {units.get(f)!r}, pinned {u!r}"
            for f, u in PINNED_DAILY_UNITS.items() if units.get(f) != u]


def check_units(run: _Run) -> None:
    """Units of the rows the latest successful run wrote, against the pinned units."""
    con = run.con

    def distinct_units(source: str, table: str, column: str) -> list | None:
        latest = _latest_success(con, source)
        if latest is None or not store.table_exists(con, table):
            return None
        rows = con.execute(f"SELECT DISTINCT {column} FROM {table} WHERE run_id = ?",
                           [latest[0]]).fetchall()
        return [r[0] for r in rows] or None

    def archive() -> None:
        daily = distinct_units(ARCHIVE, T.weather_daily, "units")
        hourly = distinct_units(ARCHIVE, T.weather_hourly, "unit")
        if daily is None and hourly is None:
            return
        problems = sorted({p for u in daily or [] for p in _unit_problems(u)})
        problems += [f"hourly temperature_2m is {u!r}, pinned {PINNED_HOURLY_UNIT!r}"
                     for u in hourly or [] if u != PINNED_HOURLY_UNIT]
        run.settle(ARCHIVE, "units", bool(problems), "; ".join(problems))

    def forecast() -> None:
        daily = distinct_units(FORECAST, T.weather_forecast, "units")
        if daily is not None:
            problems = sorted({p for u in daily for p in _unit_problems(u)})
            run.settle(FORECAST, "units", bool(problems), "; ".join(problems))

    def gauge() -> None:
        units = distinct_units(GAUGE, T.gauge_daily, "unit")
        if units is not None:
            bad = [u for u in units if u not in PINNED_GAUGE_UNITS]
            run.settle(GAUGE, "units", bool(bad),
                       f"water temperature unit {bad!r}, pinned {sorted(PINNED_GAUGE_UNITS)!r}")

    run.guard(ARCHIVE, "units", archive)
    run.guard(FORECAST, "units", forecast)
    run.guard(GAUGE, "units", gauge)


def check_weather_normals(run: _Run, since: date) -> None:
    """Quarantine archive values beyond WEATHER_SD_LIMIT standard deviations of the day's
    normal. A day without a baseline is skipped."""
    con, today = run.con, run.report.today

    def weather_normals() -> None:
        if not store.table_exists(con, T.weather_daily):
            return
        rows = con.execute(
            f"SELECT obs_date, {', '.join(WEATHER_FIELDS)} FROM {T.weather_daily} "
            "WHERE obs_date BETWEEN ? AND ? ORDER BY obs_date",
            [since, today],
        ).fetchall()
        for obs_date, *values in rows:
            for measure, value in zip(WEATHER_FIELDS, values):
                if value is None:
                    continue
                normal = _normal(con, ARCHIVE, measure, obs_date)
                if normal is None:
                    continue
                mean, sd = normal
                if sd is None or sd <= 0:
                    continue
                if abs(value - mean) > WEATHER_SD_LIMIT * sd:
                    run.quarantine(ARCHIVE, T.weather_daily, f"{obs_date.isoformat()}:{measure}",
                                   obs_date, "weather_normal", value)

    run.guard(ARCHIVE, "weather normals", weather_normals)


# --- Counts ---


def check_counts(run: _Run, since: date) -> None:
    """Plausible range and jumps that contradict the river temperature (ADR 0001).
    Counter disagreement is computed as a measure and never quarantines."""
    con, today = run.con, run.report.today
    table = T.counts_daily

    def counts() -> None:
        if not store.table_exists(con, table):
            return
        context_start = since - timedelta(days=JUMP_MAX_GAP_DAYS)
        rows = con.execute(
            f"""SELECT report_date, count_researchers, count_park, river_temp_f FROM {table}
                WHERE report_date BETWEEN ? AND ? ORDER BY report_date""",
            [context_start, today],
        ).fetchall()
        gauge = _gauge_temps(con, context_start, today)
        low, high = config.COUNT_PLAUSIBLE_RANGE

        disagreement = []
        for day, researchers, park, _ in rows:
            if day < since:
                continue
            for counter, value in (("count_researchers", researchers), ("count_park", park)):
                if value is not None and not low <= value <= high:
                    run.quarantine(COUNTS, table, day.isoformat(), day, "count_range",
                                   f"{counter}={value}")
            if researchers is not None and park is not None:
                disagreement.append({"report_date": day, "count_researchers": researchers,
                                     "count_park": park,
                                     "counter_disagreement": researchers - park})
        run.report.measures["counter_disagreement"] = disagreement
        for row in disagreement:
            day = row["report_date"]
            store.record_measure(con, "counter_disagreement", day.isoformat(), day,
                                 row["counter_disagreement"])

        counted = [(d, n, _f_to_c(f)) for d, n, _, f in rows if n is not None]
        for (d0, n0, r0), (d1, n1, r1) in zip(counted, counted[1:]):
            if d1 < since or (d1 - d0).days > JUMP_MAX_GAP_DAYS:
                continue
            deltas = [b - a for a, b in ((gauge.get(d0), gauge.get(d1)), (r0, r1))
                      if a is not None and b is not None]
            rose = any(x > 0 for x in deltas)
            fell = any(x < 0 for x in deltas)
            change = n1 - n0
            if abs(change) < JUMP_MIN_ABSOLUTE:
                continue
            if (change > JUMP_RATIO * n0 and rose) or (-change > JUMP_RATIO * n0 and fell):
                run.quarantine(COUNTS, table, d1.isoformat(), d1, "count_jump",
                               f"count_researchers {n0} on {d0} to {n1}")

    run.guard(COUNTS, "counts", counts)


# --- Cross-source ---


def check_cross_source(run: _Run) -> None:
    """Report temperature against gauge temperature on the latest day with both. Fails when
    the difference moves more than CROSS_SOURCE_MAX_SHIFT_C from its trailing median."""
    con, today = run.con, run.report.today

    def report_vs_gauge() -> None:
        if not store.table_exists(con, T.counts_daily):
            return
        reports = con.execute(
            f"""SELECT report_date, river_temp_f FROM {T.counts_daily}
                WHERE river_temp_f IS NOT NULL AND report_date <= ? ORDER BY report_date""",
            [today],
        ).fetchall()
        if not reports:
            return
        gauge = _gauge_temps(con, reports[0][0], today)
        diffs = [(d, _f_to_c(f) - gauge[d]) for d, f in reports if d in gauge]
        if len(diffs) < CROSS_SOURCE_MIN_TRAILING + 1:
            return
        day, latest = diffs[-1]
        trailing = [x for _, x in diffs[-CROSS_SOURCE_TRAILING - 1:-1]]
        median = statistics.median(trailing)
        shift = latest - median
        run.settle(GAUGE, "report_vs_gauge_temperature", abs(shift) > CROSS_SOURCE_MAX_SHIFT_C,
                   f"{day}: report minus gauge {latest:+.1f} °C, trailing median {median:+.1f} °C, "
                   f"limit {CROSS_SOURCE_MAX_SHIFT_C} °C")

    run.guard(COUNTS, "report vs gauge temperature", report_vs_gauge)


# --- Join retention ---


def check_join_retention(run: _Run, since: date) -> None:
    """The share of counted report dates from `since` that have a gauge value, and the share
    that have archive weather, for the same date. Each share is stored as a measure keyed by
    the run date. A share below JOIN_RETENTION_MIN_SHARE opens an incident on the counts
    source, and the incident closes when the share recovers.

    The weather share leaves out report dates inside the archive's lag, whose weather is null
    by design. The gauge share leaves out today, whose daily mean exists only once the day is
    over. A share over fewer than JOIN_RETENTION_MIN_DAYS report dates is stored but leaves
    its incident as it is, and so does a share with no report dates at all."""
    con, today = run.con, run.report.today

    def join_retention() -> None:
        if not store.table_exists(con, T.counts_daily):
            return
        counted = [r[0] for r in con.execute(
            f"""SELECT DISTINCT report_date FROM {T.counts_daily}
                WHERE report_date BETWEEN ? AND ? AND count_researchers IS NOT NULL
                  AND NOT coalesce(not_counted, false)
                ORDER BY report_date""",
            [since, today],
        ).fetchall()]
        if not counted:
            return

        gauge_days: set[date] = set()
        if store.table_exists(con, T.gauge_daily):
            gauge_days = {r[0] for r in con.execute(
                f"""SELECT DISTINCT obs_date FROM {T.gauge_daily}
                    WHERE obs_date BETWEEN ? AND ? AND water_temp_c IS NOT NULL""",
                [since, today],
            ).fetchall()}
        weather_days: set[date] = set()
        if store.table_exists(con, T.weather_daily):
            weather_days = {r[0] for r in con.execute(
                f"""SELECT DISTINCT obs_date FROM {T.weather_daily}
                    WHERE obs_date BETWEEN ? AND ?
                      AND coalesce({', '.join(WEATHER_FIELDS)}) IS NOT NULL""",
                [since, today],
            ).fetchall()}

        # Report dates before `end` are judged; the same cut-off as the archive null rate.
        for name, joined, end in (("gauge", gauge_days, today),
                                  ("weather", weather_days,
                                   today - timedelta(days=ARCHIVE_LAG_DAYS))):
            days = [d for d in counted if d < end]
            if not days:
                continue
            kept = sum(d in joined for d in days)
            share = kept / len(days)
            check_name = f"join_retention:{name}"
            store.record_measure(con, check_name, today.isoformat(), today, share)
            if len(days) < JOIN_RETENTION_MIN_DAYS:
                continue
            run.settle(COUNTS, check_name, share < JOIN_RETENTION_MIN_SHARE,
                       f"{kept} of {len(days)} counted report dates from {since} to "
                       f"{end - timedelta(days=1)} have {name} data ({share:.0%}), "
                       f"limit {JOIN_RETENTION_MIN_SHARE:.0%}")

    run.guard(COUNTS, "join retention", join_retention)


# --- Entry point ---


def run_checks(con: duckdb.DuckDBPyConnection, today: date, since: date | None = None) -> CheckReport:
    """Run every check for `today` and record incidents and quarantine.

    Value-level checks look at observations from `since` (default LOOKBACK_DAYS ago) through
    today. Pass an earlier `since` to check history once.
    """
    store.ensure_tables(con)
    since = since or today - timedelta(days=LOOKBACK_DAYS)
    run = _Run(con, today)
    state = season_mod.state(con, today)
    run.report.season = state

    check_freshness(run, state)
    check_volume(run)
    check_duplicates(run)
    check_null_rates(run)
    check_units(run)
    check_weather_normals(run, since)
    check_counts(run, since)
    check_cross_source(run)
    check_join_retention(run, since)

    for source in SOURCES:
        if source not in run.errored:
            run.settle(source, "schema", False)
    return run.report
