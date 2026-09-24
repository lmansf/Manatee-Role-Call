"""Tests for tools/mutate.py, against throwaway DuckDB files.

The "live" database here is a temporary file: config.DB_PATH is pointed at it, so the real
database is never opened.
"""
import hashlib
import importlib.util
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from roll_call import config

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("mutate", REPO / "tools" / "mutate.py")
mutate = importlib.util.module_from_spec(_spec)
sys.modules["mutate"] = mutate
_spec.loader.exec_module(mutate)

N_DAYS = 20


def _build(path: Path) -> None:
    """Tables shaped like the contract: weather_daily (single key), gauge_daily (compound key)
    and a keyless table."""
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE weather_daily (
        obs_date DATE PRIMARY KEY, temperature_2m_min DOUBLE, temperature_2m_max DOUBLE,
        temperature_2m_mean DOUBLE, precipitation_sum DOUBLE, wind_speed_10m_max DOUBLE,
        shortwave_radiation_sum DOUBLE, units VARCHAR, run_id VARCHAR, ingested_at TIMESTAMP)""")
    con.execute("""CREATE TABLE gauge_daily (
        obs_date DATE, water_temp_c DOUBLE, unit VARCHAR, approval_status VARCHAR,
        run_id VARCHAR, ingested_at TIMESTAMP, PRIMARY KEY (obs_date, approval_status))""")
    con.execute("CREATE TABLE loose (day DATE, value DOUBLE, n INTEGER)")
    start = date(2026, 1, 1)
    for i in range(N_DAYS):
        d = start + timedelta(days=i)
        con.execute("INSERT INTO weather_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'r1', '2026-02-01')",
                    [d, 5.0 + i, 20.0 + i, 12.5 + i, 0.1 * i, 10.0, 15.0, '{"temperature_2m_mean": "°C"}'])
        con.execute("INSERT INTO gauge_daily VALUES (?, ?, 'degC', 'Approved', 'r1', '2026-02-01')",
                    [d, 15.0 + 0.25 * i])
        con.execute("INSERT INTO loose VALUES (?, ?, ?)", [d, float(i), i])
    con.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path, sql: str):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _columns(path: Path, table: str) -> list[str]:
    return [r[0] for r in _rows(path, f"SELECT column_name FROM information_schema.columns "
                                      f"WHERE table_name = '{table}' ORDER BY ordinal_position")]


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A stand-in live database, with config pointed at it and no backup folder set."""
    path = tmp_path / "live" / "roll_call.duckdb"
    path.parent.mkdir()
    _build(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    return path


@pytest.fixture
def copy(live, tmp_path):
    target = tmp_path / "scratch" / "copy.duckdb"
    mutate.copy_database(live, target)
    return target


@pytest.fixture
def con(copy):
    c = duckdb.connect(str(copy))
    yield c
    c.close()


# --------------------------------------------------------------------------- guard

def test_guard_refuses_live_path(live):
    with pytest.raises(mutate.UnsafeTargetError):
        mutate.assert_safe_target(live)
    with pytest.raises(mutate.UnsafeTargetError):
        mutate.assert_safe_target(str(live))


def test_guard_refuses_symlink_to_live(live, tmp_path):
    link = tmp_path / "innocent.duckdb"
    link.symlink_to(live)
    with pytest.raises(mutate.UnsafeTargetError):
        mutate.assert_safe_target(link)


def test_guard_refuses_symlinked_folder_to_live(live, tmp_path):
    folder = tmp_path / "elsewhere"
    folder.symlink_to(live.parent, target_is_directory=True)
    with pytest.raises(mutate.UnsafeTargetError):
        mutate.assert_safe_target(folder / live.name)


def test_guard_refuses_dotdot_path_to_live(live, tmp_path):
    (tmp_path / "other").mkdir()
    sneaky = tmp_path / "other" / ".." / "live" / "roll_call.duckdb"
    with pytest.raises(mutate.UnsafeTargetError):
        mutate.assert_safe_target(sneaky)


def test_guard_refuses_relative_path_to_live(live, monkeypatch):
    monkeypatch.chdir(live.parent.parent)
    with pytest.raises(mutate.UnsafeTargetError):
        mutate.assert_safe_target(Path("live") / "roll_call.duckdb")


def test_guard_refuses_hard_link_to_live(live, tmp_path):
    hard = tmp_path / "hard.duckdb"
    os.link(live, hard)
    with pytest.raises(mutate.UnsafeTargetError):
        mutate.assert_safe_target(hard)


def test_guard_refuses_backup_folder(live, tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    (backup / "2026-01-05").mkdir(parents=True)
    monkeypatch.setenv("BACKUP_DIR", str(backup))
    for bad in (backup / "roll_call-2026-01-05.duckdb", backup / "2026-01-05" / "x.duckdb",
                tmp_path / "live" / ".." / "backup" / "x.duckdb"):
        with pytest.raises(mutate.UnsafeTargetError, match="backup"):
            mutate.assert_safe_target(bad)


def test_guard_refuses_symlink_into_backup_folder(live, tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "copy.duckdb").write_bytes(b"")
    monkeypatch.setenv("BACKUP_DIR", str(backup))
    link = tmp_path / "looks-fine.duckdb"
    link.symlink_to(backup / "copy.duckdb")
    with pytest.raises(mutate.UnsafeTargetError):
        mutate.assert_safe_target(link)


def test_guard_allows_a_copy(live, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backup"))
    mutate.assert_safe_target(tmp_path / "scratch.duckdb")
    mutate.assert_safe_target(tmp_path / "backup-not-really" / "scratch.duckdb")
    mutate.assert_safe_target(live.with_name("roll_call_copy.duckdb"))


def test_mutation_refuses_a_connection_to_live(live):
    c = duckdb.connect(str(live))
    try:
        for name, fn in mutate.MUTATIONS.items():
            with pytest.raises(mutate.UnsafeTargetError):
                fn(c, "weather_daily", column="temperature_2m_mean", seed=1)
    finally:
        c.close()


def test_mutate_entry_point_refuses_live(live):
    before = _digest(live)
    with pytest.raises(mutate.UnsafeTargetError):
        mutate.mutate(live, "weather_daily", "null_spike", column="temperature_2m_mean", rate=0.5)
    assert _digest(live) == before


# --------------------------------------------------------------------------- mutations

def test_schema_drift_drops_named_column(con):
    text = mutate.schema_drift(con, "weather_daily", column="precipitation_sum")
    cols = [r[0] for r in con.execute("DESCRIBE weather_daily").fetchall()]
    assert "precipitation_sum" not in cols
    assert "dropped column weather_daily.precipitation_sum" in text


def test_schema_drift_drop_before_an_indexed_column_keeps_rows_and_key(con):
    before = con.execute("SELECT obs_date, water_temp_c FROM gauge_daily ORDER BY 1").fetchall()
    mutate.schema_drift(con, "gauge_daily", column="unit")
    assert "unit" not in [r[0] for r in con.execute("DESCRIBE gauge_daily").fetchall()]
    assert con.execute("SELECT obs_date, water_temp_c FROM gauge_daily ORDER BY 1").fetchall() == before
    key = con.execute("SELECT constraint_column_names FROM duckdb_constraints() "
                      "WHERE table_name = 'gauge_daily' AND constraint_type = 'PRIMARY KEY'").fetchall()
    assert key == [(["obs_date", "approval_status"],)]


def test_schema_drift_drop_picks_a_non_key_column_reproducibly(live, tmp_path):
    picked = []
    for i in range(2):
        target = tmp_path / f"c{i}.duckdb"
        mutate.copy_database(live, target)
        picked.append(mutate.mutate(target, "gauge_daily", "schema_drift", seed=7))
    assert picked[0] == picked[1]
    dropped = picked[0].split("gauge_daily.")[1].split(" ")[0]
    assert dropped in ("water_temp_c", "unit")


def test_schema_drift_adds_column(con):
    text = mutate.schema_drift(con, "weather_daily", drift="add")
    assert con.execute("SELECT count(*), count(drift_extra) FROM weather_daily").fetchone() == (N_DAYS, 0)
    assert "added column weather_daily.drift_extra" in text


def test_null_spike_nulls_the_rate(con, capsys):
    text = mutate.null_spike(con, "weather_daily", column="temperature_2m_mean", rate=0.3, seed=1)
    nulls = con.execute("SELECT count(*) FROM weather_daily WHERE temperature_2m_mean IS NULL").fetchone()[0]
    assert nulls == round(0.3 * N_DAYS) == 6
    assert f"set 6 of {N_DAYS} non-null values in weather_daily.temperature_2m_mean" in text
    assert text in capsys.readouterr().out
    # Other columns untouched.
    assert con.execute("SELECT count(temperature_2m_max) FROM weather_daily").fetchone()[0] == N_DAYS


def test_null_spike_is_reproducible_with_a_seed(live, tmp_path):
    def nulled(name, seed):
        target = tmp_path / name
        mutate.copy_database(live, target)
        mutate.mutate(target, "weather_daily", "null_spike", column="temperature_2m_mean", rate=0.4, seed=seed)
        return _rows(target, "SELECT obs_date FROM weather_daily WHERE temperature_2m_mean IS NULL ORDER BY 1")

    assert nulled("a.duckdb", 3) == nulled("b.duckdb", 3)
    assert nulled("a.duckdb", 3) != nulled("c.duckdb", 4)


def test_null_spike_counts_only_values_it_removed(con):
    mutate.null_spike(con, "loose", column="value", rate=0.5, seed=1)
    text = mutate.null_spike(con, "loose", column="value", rate=0.5, seed=1)
    assert f"set 5 of {N_DAYS // 2} non-null values" in text
    assert con.execute("SELECT count(value) FROM loose").fetchone()[0] == 5


def test_null_spike_rejects_bad_rate(con):
    with pytest.raises(ValueError):
        mutate.null_spike(con, "weather_daily", column="temperature_2m_mean", rate=1.5)


def test_unit_change_converts_celsius_to_fahrenheit(con):
    before = dict(con.execute("SELECT obs_date, water_temp_c FROM gauge_daily").fetchall())
    text = mutate.unit_change(con, "gauge_daily", column="water_temp_c")
    after = dict(con.execute("SELECT obs_date, water_temp_c FROM gauge_daily").fetchall())
    assert all(after[d] == pytest.approx(before[d] * 9 / 5 + 32) for d in before)
    assert con.execute("SELECT DISTINCT unit FROM gauge_daily").fetchall() == [("degC",)]
    assert "°C to °F" in text and f"on {N_DAYS} non-null values" in text


def test_unit_change_takes_factor_and_offset(con):
    mutate.unit_change(con, "loose", column="value", factor=10, offset=-1)
    assert con.execute("SELECT value FROM loose ORDER BY day LIMIT 3").fetchall() == [(-1.0,), (9.0,), (19.0,)]


def test_unit_change_refuses_text_column(con):
    with pytest.raises(ValueError, match="not numeric"):
        mutate.unit_change(con, "gauge_daily", column="unit")


def test_duplicates_in_a_keyless_table(con):
    text = mutate.duplicates(con, "loose", rate=0.25, seed=2)
    assert con.execute("SELECT count(*) FROM loose").fetchone()[0] == N_DAYS + 5
    dup_days = con.execute("SELECT count(*) FROM (SELECT day FROM loose GROUP BY ALL HAVING count(*) > 1)").fetchone()[0]
    assert dup_days == 5
    assert "copied 5 of 20 rows in loose" in text and "shadow" not in text


def test_duplicates_in_a_keyed_table_go_into_a_keyless_shadow(con):
    before = sorted(con.execute("SELECT * FROM gauge_daily").fetchall())
    types_before = con.execute("DESCRIBE gauge_daily").fetchall()
    text = mutate.duplicates(con, "gauge_daily", rate=0.2, seed=2)
    after = con.execute("SELECT * FROM gauge_daily").fetchall()
    assert len(after) == N_DAYS + 4
    assert sorted(set(after)) == before  # only exact copies were added
    assert con.execute("SELECT count(*) FROM (SELECT obs_date, approval_status FROM gauge_daily "
                       "GROUP BY ALL HAVING count(*) > 1)").fetchone()[0] == 4
    assert [r[:2] for r in con.execute("DESCRIBE gauge_daily").fetchall()] == [r[:2] for r in types_before]
    keys = con.execute("SELECT count(*) FROM duckdb_constraints() WHERE table_name = 'gauge_daily' "
                       "AND constraint_type IN ('PRIMARY KEY', 'UNIQUE')").fetchone()[0]
    assert keys == 0
    assert "keyless shadow" in text and "obs_date, approval_status" in text
    tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert tables == {"weather_daily", "gauge_daily", "loose"}


def test_duplicates_are_reproducible_with_a_seed(live, tmp_path):
    def duplicated(name):
        target = tmp_path / name
        mutate.copy_database(live, target)
        mutate.mutate(target, "weather_daily", "duplicates", rate=0.3, seed=11)
        return _rows(target, "SELECT obs_date FROM weather_daily GROUP BY 1 HAVING count(*) > 1 ORDER BY 1")

    first = duplicated("a.duckdb")
    assert len(first) == 6
    assert first == duplicated("b.duckdb")


def test_renamed_key_defaults_to_first_key_column(con):
    text = mutate.renamed_key(con, "gauge_daily")
    cols = [r[0] for r in con.execute("DESCRIBE gauge_daily").fetchall()]
    assert "obs_date" not in cols and "obs_date_renamed" in cols
    assert text.strip() == "renamed_key: renamed primary-key column gauge_daily.obs_date to obs_date_renamed"


def test_renamed_key_takes_column_and_new_name(con):
    mutate.renamed_key(con, "weather_daily", column="obs_date", new_name="date")
    assert con.execute("SELECT count(date) FROM weather_daily").fetchone()[0] == N_DAYS


def test_renamed_key_needs_column_when_table_has_no_key(con):
    with pytest.raises(ValueError, match="no primary key"):
        mutate.renamed_key(con, "loose")


def test_unknown_table_is_an_error(con):
    with pytest.raises(ValueError, match="does not exist"):
        mutate.null_spike(con, "nope", column="x")


# --------------------------------------------------------------------------- copy and CLI

def test_copy_database_leaves_source_untouched_and_drops_stale_wal(live, tmp_path):
    before = _digest(live)
    target = tmp_path / "c.duckdb"
    Path(f"{target}.wal").write_bytes(b"stale log from an old copy")
    mutate.copy_database(live, target)
    assert _digest(live) == before
    assert not Path(f"{target}.wal").exists()
    assert _rows(target, "SELECT count(*) FROM weather_daily") == [(N_DAYS,)]


def test_copy_database_refuses_live_as_target(live, tmp_path):
    other = tmp_path / "other.duckdb"
    _build(other)
    before = _digest(live)
    with pytest.raises(mutate.UnsafeTargetError):
        mutate.copy_database(other, live)
    assert _digest(live) == before


def test_cli_copies_then_mutates(live, tmp_path, capsys):
    before = _digest(live)
    target = tmp_path / "scratch" / "copy.duckdb"
    code = mutate.main(["--copy-from", str(live), "--db", str(target), "--table", "weather_daily",
                        "--mutation", "null_spike", "--column", "temperature_2m_mean",
                        "--rate", "0.5", "--seed", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "copied" in out and "null_spike: set 10 of 20" in out
    assert _rows(target, "SELECT count(*) FROM weather_daily WHERE temperature_2m_mean IS NULL") == [(10,)]
    assert _digest(live) == before


def test_cli_refuses_live_target(live, capsys):
    before = _digest(live)
    code = mutate.main(["--db", str(live), "--table", "weather_daily", "--mutation", "duplicates"])
    assert code == 2
    assert "refusing the live database" in capsys.readouterr().err
    assert _digest(live) == before


def test_cli_refuses_copy_onto_live(live, tmp_path, capsys):
    other = tmp_path / "other.duckdb"
    _build(other)
    before = _digest(live)
    code = mutate.main(["--copy-from", str(other), "--db", str(live), "--table", "weather_daily",
                        "--mutation", "renamed_key"])
    assert code == 2
    assert _digest(live) == before


def test_cli_needs_an_existing_copy(live, tmp_path, capsys):
    code = mutate.main(["--db", str(tmp_path / "missing.duckdb"), "--table", "weather_daily",
                        "--mutation", "duplicates"])
    assert code == 2
    assert "--copy-from" in capsys.readouterr().err


def test_cli_passes_mutation_options(copy, capsys):
    assert mutate.main(["--db", str(copy), "--table", "gauge_daily", "--mutation", "unit_change",
                        "--column", "water_temp_c", "--factor", "2", "--offset", "0"]) == 0
    assert _rows(copy, "SELECT min(water_temp_c) FROM gauge_daily") == [(30.0,)]
    assert mutate.main(["--db", str(copy), "--table", "gauge_daily", "--mutation", "renamed_key",
                        "--new-name", "day"]) == 0
    assert "day" in _columns(copy, "gauge_daily")
    assert mutate.main(["--db", str(copy), "--table", "loose", "--mutation", "schema_drift",
                        "--drift", "add", "--column", "surprise"]) == 0
    assert "surprise" in _columns(copy, "loose")


def test_script_runs_end_to_end(live, tmp_path):
    """Run the file as a script, the way the owner will, with the live path set by ROLL_CALL_DB."""
    target = tmp_path / "scratch.duckdb"
    env = {**os.environ, "ROLL_CALL_DB": str(live)}
    env.pop("BACKUP_DIR", None)
    ok = subprocess.run(
        [sys.executable, str(REPO / "tools" / "mutate.py"), "--copy-from", str(live), "--db", str(target),
         "--table", "weather_daily", "--mutation", "duplicates", "--rate", "0.1", "--seed", "5"],
        capture_output=True, text=True, env=env, cwd=tmp_path)
    assert ok.returncode == 0, ok.stderr
    assert "duplicates: copied 2 of 20 rows in weather_daily" in ok.stdout
    refused = subprocess.run(
        [sys.executable, str(REPO / "tools" / "mutate.py"), "--db", "live/../live/roll_call.duckdb",
         "--table", "weather_daily", "--mutation", "duplicates"],
        capture_output=True, text=True, env=env, cwd=tmp_path)
    assert refused.returncode == 2
    assert "refusing the live database" in refused.stderr
