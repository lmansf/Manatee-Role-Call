"""The run log and the backup job, against a throwaway DuckDB file."""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from roll_call.ingest.base import IngestRun
from roll_call.storage import db

_spec = importlib.util.spec_from_file_location(
    "backup_db", Path(__file__).resolve().parents[1] / "jobs" / "backup_db.py")
backup_db = importlib.util.module_from_spec(_spec)
sys.modules["backup_db"] = backup_db
_spec.loader.exec_module(backup_db)


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "test.duckdb")
    yield c
    c.close()


def test_last_successful_run_ignores_failures(con):
    t0 = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
    ok = IngestRun(source="s", started_at=t0).succeed(3)
    later_failure = IngestRun(source="s", started_at=t0 + timedelta(days=1)).fail(RuntimeError("x"))
    other_source = IngestRun(source="other", started_at=t0 + timedelta(days=2)).succeed(1)
    for r in (ok, later_failure, other_source):
        db.record_run(con, r)
    assert db.last_successful_run(con, "s") == datetime(2026, 1, 5, 0, 0)
    assert db.last_successful_run(con, "never") is None


def test_record_run_is_idempotent(con):
    run = IngestRun(source="s")
    db.record_run(con, run)
    db.record_run(con, run.succeed(7))
    rows = con.execute("SELECT status, row_count FROM ingest_runs").fetchall()
    assert rows == [("succeeded", 7)]


def test_timestamps_stored_as_utc(con):
    est = timezone(timedelta(hours=-5))
    db.record_run(con, IngestRun(source="s", started_at=datetime(2026, 1, 5, 19, 0, tzinfo=est)).succeed(0))
    assert db.last_successful_run(con, "s") == datetime(2026, 1, 6, 0, 0)


def test_export_records_writes_existing_tables_only(con, tmp_path):
    con.execute("CREATE TABLE clearing_decisions (obs_id VARCHAR, decision VARCHAR, reason VARCHAR)")
    con.execute("INSERT INTO clearing_decisions VALUES ('b', 'rejected', 'parser bug'), ('a', 'confirmed', 'cold snap')")
    written = backup_db.export_records(con, tmp_path / "records")
    assert [p.name for p in written] == ["clearing_decisions.csv"]
    lines = written[0].read_text().splitlines()
    assert lines[0] == "obs_id,decision,reason" and lines[1].startswith("a,")


def test_copy_database_prunes_and_refuses_missing_dir(tmp_path):
    src = tmp_path / "roll_call.duckdb"
    src.write_bytes(b"x")
    dest = tmp_path / "backups"
    with pytest.raises(RuntimeError, match="mounted"):
        backup_db.copy_database(src, dest)
    dest.mkdir()
    for d in ("2026-01-01", "2026-01-08", "2026-01-15"):
        (dest / f"roll_call_{d}.duckdb").write_bytes(b"old")
    target = backup_db.copy_database(src, dest, keep=2)
    assert target.exists()
    assert len(list(dest.glob("roll_call_*.duckdb"))) == 2
