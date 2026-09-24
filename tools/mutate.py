"""Synthetic mutation generator. Standalone, on demand, never scheduled.

Decision (spec §3.4): point it at a *copy* of the database file to prove that every quality
check fires. It is not part of the daily job.

Usage:

    python tools/mutate.py --copy-from data/roll_call.duckdb --db /tmp/scratch.duckdb \
        --table weather_daily --mutation null_spike --column temperature_2m_mean --rate 0.3 --seed 1

`--copy-from` copies a database file (usually the live one) to `--db` first. It only reads the
source. Without it, `--db` must already be a copy.

Mutations, one function each: schema_drift (drop or add a column), null_spike, unit_change
(°C to °F on one column, or any factor and offset), duplicates, renamed_key. Each prints and
returns one line saying exactly what it changed. Keep that line beside the check report: every
mutation should show up there as an incident or a quarantined observation.

GUARDRAIL: every entry point refuses the live database and anything in the backup folder.
The mutation functions check the file behind their connection too, so a caller who opens the
live file by hand still gets refused.
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import duckdb

if __package__ in (None, ""):  # run as a script: make `roll_call` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roll_call import config  # noqa: E402

NUMERIC_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "FLOAT", "DOUBLE", "DECIMAL",
                 "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT")
# Bookkeeping columns every parsed table carries. Picking one at random would test the
# storage layer, not a source changing, so automatic picks skip them.
BOOKKEEPING_COLUMNS = ("run_id", "ingested_at")
DEFAULT_RATE = 0.2
DEFAULT_ADDED_COLUMN = "drift_extra"


class UnsafeTargetError(RuntimeError):
    """The target is the live database or sits in the backup folder."""


# --------------------------------------------------------------------------- guard

def _backup_dirs() -> list[Path]:
    """Resolved candidates for BACKUP_DIR, or none when it is not set.

    The backup job reads BACKUP_DIR as given, so a relative value depends on the working
    directory. Both readings are refused: relative to the repo root and relative to here.
    """
    raw = config.env("BACKUP_DIR", required=False)
    if not raw:
        return []
    path = Path(raw).expanduser()
    if path.is_absolute():
        return [path.resolve()]
    return [(config.REPO_ROOT / path).resolve(), path.resolve()]


def assert_safe_target(db_path: Path | str) -> None:
    """Raise UnsafeTargetError unless `db_path` is a different file from the live database.

    Paths are compared after resolving, so a relative path, a symlink or a `..` cannot slip
    past. A hard link to the live file is caught by comparing inodes when both files exist.
    Anything inside BACKUP_DIR (when set) is refused too: a backup is the copy you restore from.
    """
    target = Path(db_path).expanduser().resolve()
    live = Path(config.DB_PATH).expanduser().resolve()
    if target == live or (target.exists() and live.exists() and os.path.samefile(target, live)):
        raise UnsafeTargetError(f"refusing the live database {live}; mutate a copy instead")
    for backup in _backup_dirs():
        if target == backup or target.is_relative_to(backup):
            raise UnsafeTargetError(f"refusing {target}: it is inside the backup folder {backup}")


def _assert_safe_connection(con: duckdb.DuckDBPyConnection) -> None:
    """Refuse a connection whose current database file is unsafe. In-memory is fine."""
    row = con.execute(
        "SELECT path FROM duckdb_databases() WHERE database_name = current_database()").fetchone()
    if row and row[0]:
        assert_safe_target(row[0])


# --------------------------------------------------------------------------- helpers

def _columns(con, table: str) -> list[tuple[str, str]]:
    rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = ? AND table_schema = current_schema() ORDER BY ordinal_position",
        [table]).fetchall()
    if not rows:
        raise ValueError(f"table {table} does not exist")
    return rows


def _key_columns(con, table: str, kinds: tuple[str, ...] = ("PRIMARY KEY",)) -> list[str]:
    """Columns in the table's constraints of the given kinds, primary key first."""
    rows = con.execute(
        "SELECT constraint_type, constraint_column_names FROM duckdb_constraints() "
        "WHERE table_name = ? AND schema_name = current_schema()", [table]).fetchall()
    cols: list[str] = []
    for kind in kinds:
        for ctype, names in rows:
            if ctype == kind:
                cols += [n for n in names if n not in cols]
    return cols


def _is_numeric(data_type: str) -> bool:
    return data_type.upper().split("(")[0] in NUMERIC_TYPES


def _require_column(con, table: str, column: str) -> str:
    types = dict(_columns(con, table))
    if column not in types:
        raise ValueError(f"{table} has no column {column}; columns: {', '.join(types)}")
    return types[column]


def _pick_column(con, table: str, rng: random.Random, numeric: bool = False) -> str:
    """A seeded choice among the table's non-key, non-bookkeeping columns."""
    keys = set(_key_columns(con, table, ("PRIMARY KEY", "UNIQUE")))
    candidates = [name for name, dtype in _columns(con, table)
                  if name not in keys and name not in BOOKKEEPING_COLUMNS
                  and (not numeric or _is_numeric(dtype))]
    if not candidates:
        kind = "numeric " if numeric else ""
        raise ValueError(f"{table} has no {kind}column to pick; pass --column")
    return rng.choice(candidates)


def _q(name: str) -> str:
    """Quote an identifier."""
    return '"' + name.replace('"', '""') + '"'


def _sample_rowids(con, table: str, rate: float, rng: random.Random, where: str = "TRUE") -> tuple[list[int], int]:
    """A seeded sample of `rate` of the matching rows, as rowids, and the number that matched."""
    if not 0 <= rate <= 1:
        raise ValueError(f"rate must be between 0 and 1, got {rate}")
    rowids = [r[0] for r in con.execute(
        f"SELECT rowid FROM {_q(table)} WHERE {where} ORDER BY rowid").fetchall()]
    k = round(rate * len(rowids))
    return sorted(rng.sample(rowids, k)), len(rowids)


def _replace_table(con, table: str, select_sql: str) -> None:
    """Replace `table` with the result of `select_sql`, same name, row order kept.

    The new table has the selected columns and types and no constraints.
    """
    shadow = f"{table}__mutate_shadow"
    con.execute(f"CREATE TABLE {_q(shadow)} AS {select_sql} ORDER BY rowid")
    con.execute(f"DROP TABLE {_q(table)}")
    con.execute(f"ALTER TABLE {_q(shadow)} RENAME TO {_q(table)}")


def _report(text: str) -> str:
    print(text)
    return text


# --------------------------------------------------------------------------- mutations

def schema_drift(con, table: str, *, column: str | None = None, drift: str = "drop",
                 seed: int | None = None, **_) -> str:
    """Drop a column, or add one the source never sent.

    drop: removes `column`, or a seeded choice among the non-key columns. DuckDB will not drop
    a column that sits before an indexed one, so then the table is rebuilt without the column
    and its primary key is put back. DuckDB refuses to drop a key column; use renamed_key to
    break a key.
    add: adds a VARCHAR column named `column` (default drift_extra), NULL in every row.
    """
    _assert_safe_connection(con)
    if drift == "drop":
        column = column or _pick_column(con, table, random.Random(seed))
        dtype = _require_column(con, table, column)
        try:
            con.execute(f"ALTER TABLE {_q(table)} DROP COLUMN {_q(column)}")
        except duckdb.CatalogException as exc:
            # DuckDB will not drop a column that sits before an indexed one. Rebuild the
            # table without it and put the primary key back.
            if "index depends on a column after it" not in str(exc):
                raise
            keys = _key_columns(con, table)
            _replace_table(con, table, f"SELECT * EXCLUDE ({_q(column)}) FROM {_q(table)}")
            if keys:
                con.execute(f"ALTER TABLE {_q(table)} ADD PRIMARY KEY ({', '.join(map(_q, keys))})")
        return _report(f"schema_drift: dropped column {table}.{column} ({dtype})")
    if drift == "add":
        column = column or DEFAULT_ADDED_COLUMN
        if column in dict(_columns(con, table)):
            raise ValueError(f"{table} already has a column {column}")
        con.execute(f"ALTER TABLE {_q(table)} ADD COLUMN {_q(column)} VARCHAR")
        return _report(f"schema_drift: added column {table}.{column} (VARCHAR, NULL in every row)")
    raise ValueError(f"drift must be 'drop' or 'add', got {drift!r}")


def null_spike(con, table: str, *, column: str | None = None, rate: float = DEFAULT_RATE,
               seed: int | None = None, **_) -> str:
    """Set a seeded `rate` of the column's non-null values to NULL.

    The sample is drawn from rows where the column is not already NULL, so the count in the
    description is exactly the number of values removed.
    """
    _assert_safe_connection(con)
    rng = random.Random(seed)
    column = column or _pick_column(con, table, rng)
    _require_column(con, table, column)
    rowids, eligible = _sample_rowids(con, table, rate, rng, f"{_q(column)} IS NOT NULL")
    if rowids:
        con.execute(f"UPDATE {_q(table)} SET {_q(column)} = NULL WHERE rowid IN "
                    f"(SELECT unnest(?::BIGINT[]))", [rowids])
    total = con.execute(f"SELECT count(*) FROM {_q(table)}").fetchone()[0]
    return _report(f"null_spike: set {len(rowids)} of {eligible} non-null values in {table}.{column} "
                   f"to NULL (rate {rate}, seed {seed}; table has {total} rows)")


def unit_change(con, table: str, *, column: str | None = None, factor: float = 9 / 5,
                offset: float = 32.0, seed: int | None = None, **_) -> str:
    """Rewrite one numeric column as value * factor + offset. The default is °C to °F.

    Every row changes. Unit labels in the table (units, unit) are left alone on purpose: a
    source that silently switches units still claims the old one.
    An integer column keeps its type, so the results are rounded.
    """
    _assert_safe_connection(con)
    column = column or _pick_column(con, table, random.Random(seed), numeric=True)
    dtype = _require_column(con, table, column)
    if not _is_numeric(dtype):
        raise ValueError(f"{table}.{column} is {dtype}, not numeric")
    changed = con.execute(f"SELECT count(*) FROM {_q(table)} WHERE {_q(column)} IS NOT NULL").fetchone()[0]
    con.execute(f"UPDATE {_q(table)} SET {_q(column)} = {_q(column)} * ? + ?", [factor, offset])
    label = " (°C to °F)" if (factor, offset) == (9 / 5, 32.0) else ""
    return _report(f"unit_change: {table}.{column} = value * {factor:g} + {offset:g}{label} "
                   f"on {changed} non-null values; unit labels unchanged")


def duplicates(con, table: str, *, rate: float = DEFAULT_RATE, seed: int | None = None, **_) -> str:
    """Append exact copies of a seeded `rate` of the rows.

    A table with a primary key (or a unique constraint) would reject the copies. That is the
    key doing its job, but then the duplicate check never sees anything. So the table is first
    replaced by a keyless shadow: an identical copy under the same name, with the same columns
    and types and no constraints. The copies then go into the shadow. This stands in for a
    source that re-sends an observation into a table whose key has gone missing.
    """
    _assert_safe_connection(con)
    rng = random.Random(seed)
    rowids, total = _sample_rowids(con, table, rate, rng)
    keys = _key_columns(con, table, ("PRIMARY KEY", "UNIQUE"))
    copies = "mutate_duplicate_rows"
    con.execute(f"CREATE OR REPLACE TEMP TABLE {copies} AS SELECT * FROM {_q(table)} "
                f"WHERE rowid IN (SELECT unnest(?::BIGINT[])) ORDER BY rowid", [rowids])
    note = ""
    if keys:
        _replace_table(con, table, f"SELECT * FROM {_q(table)}")
        note = f"; replaced {table} with a keyless shadow (dropped key on {', '.join(keys)})"
    con.execute(f"INSERT INTO {_q(table)} SELECT * FROM {copies}")
    con.execute(f"DROP TABLE {copies}")
    return _report(f"duplicates: copied {len(rowids)} of {total} rows in {table} "
                   f"(rate {rate}, seed {seed}){note}")


def renamed_key(con, table: str, *, column: str | None = None, new_name: str | None = None, **_) -> str:
    """Rename a key column, as a source renaming a field would.

    The default column is the first primary-key column. The default new name is
    `<column>_renamed`. The key constraint follows the rename.
    """
    _assert_safe_connection(con)
    keys = _key_columns(con, table)
    if column is None:
        if not keys:
            raise ValueError(f"{table} has no primary key; pass --column")
        column = keys[0]
    _require_column(con, table, column)
    new_name = new_name or f"{column}_renamed"
    if new_name in dict(_columns(con, table)):
        raise ValueError(f"{table} already has a column {new_name}")
    con.execute(f"ALTER TABLE {_q(table)} RENAME COLUMN {_q(column)} TO {_q(new_name)}")
    role = "primary-key column" if column in keys else "column"
    return _report(f"renamed_key: renamed {role} {table}.{column} to {new_name}")


MUTATIONS: dict[str, Callable[..., str]] = {
    "schema_drift": schema_drift,
    "null_spike": null_spike,
    "unit_change": unit_change,
    "duplicates": duplicates,
    "renamed_key": renamed_key,
}


# --------------------------------------------------------------------------- entry points

def copy_database(source: Path | str, target: Path | str) -> None:
    """Copy a database file (and its write-ahead log, if any) to `target`. Reads the source only.

    A stale log beside the target is removed first: DuckDB would replay it over the new copy.
    """
    assert_safe_target(target)
    source, target = Path(source), Path(target)
    if not source.is_file():
        raise FileNotFoundError(f"no database file at {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    source_wal, target_wal = Path(f"{source}.wal"), Path(f"{target}.wal")
    target_wal.unlink(missing_ok=True)
    if source_wal.is_file():
        shutil.copyfile(source_wal, target_wal)


def mutate(db_path: Path | str, table: str, mutation: str, **options) -> str:
    """Guard the target, open it, apply one mutation and return its description."""
    assert_safe_target(db_path)
    if mutation not in MUTATIONS:
        raise ValueError(f"unknown mutation {mutation!r}; choose from {', '.join(MUTATIONS)}")
    con = duckdb.connect(str(db_path))
    try:
        return MUTATIONS[mutation](con, table, **options)
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mutate a copy of the Roll Call database so the quality checks have something to find.")
    parser.add_argument("--db", required=True, help="the copy to mutate; never the live database")
    parser.add_argument("--table", required=True)
    parser.add_argument("--mutation", required=True, choices=sorted(MUTATIONS))
    parser.add_argument("--column", help="column to mutate (default: a seeded pick, or the first key column for renamed_key)")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE, help="fraction of rows for null_spike and duplicates")
    parser.add_argument("--seed", type=int, help="random seed, for reproducible runs")
    parser.add_argument("--copy-from", help="copy this database file to --db first (it is only read)")
    parser.add_argument("--drift", choices=("drop", "add"), default="drop", help="schema_drift: drop or add a column")
    parser.add_argument("--factor", type=float, default=9 / 5, help="unit_change factor (default °C to °F)")
    parser.add_argument("--offset", type=float, default=32.0, help="unit_change offset")
    parser.add_argument("--new-name", help="renamed_key: the new column name")
    args = parser.parse_args(argv)

    try:
        assert_safe_target(args.db)
        if args.copy_from:
            copy_database(args.copy_from, args.db)
            print(f"copied {args.copy_from} to {args.db}")
        elif not Path(args.db).is_file():
            raise FileNotFoundError(f"no database file at {args.db}; pass --copy-from to make one")
        mutate(args.db, args.table, args.mutation, column=args.column, rate=args.rate, seed=args.seed,
               drift=args.drift, factor=args.factor, offset=args.offset, new_name=args.new_name)
    except (UnsafeTargetError, ValueError, FileNotFoundError, duckdb.Error) as exc:
        print(f"mutate: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
