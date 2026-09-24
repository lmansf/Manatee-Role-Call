"""The dashboard export, against a throwaway DuckDB file."""
import csv
from datetime import datetime, timezone

import duckdb
import pytest

from roll_call.export import dashboard
from roll_call.ingest.base import IngestRun
from roll_call.storage import db

NOW = datetime(2026, 1, 7, 0, 30, tzinfo=timezone.utc)

# The contract with the Evidence project in dashboard/. Spelled out here, not imported, so a
# change to dashboard.COLUMNS fails this test.
CONTRACT = {
    "meta.csv": "generated_at_utc,git_sha",
    "source_status.csv": "run_date,source,status,last_success_utc,row_count,rows_expected,"
                         "null_rate,null_rate_normal",
    "baseline_drift.csv": "refreshed_on,replayed,source,measure,old_value,new_value,change,"
                          "cumulative_change",
    "quarantine_queue.csv": "observation_date,source,check_name,value,status,opened_on,"
                            "cleared_on,days_open",
}


def _run(source, started_at, row_count=None, error=None):
    run = IngestRun(source=source, started_at=started_at)
    return run.fail(error) if error else run.succeed(row_count)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "test.duckdb")
    # Local dates are America/New_York (UTC-5 in January).
    for run in (
        # 5 Jan local: gauge fails at 19:00, then succeeds on the retry.
        _run("gauge", utc(2026, 1, 6, 0, 0), error=RuntimeError("timeout")),
        _run("gauge", utc(2026, 1, 6, 0, 5), row_count=3),
        # 5 Jan local: counts succeed.
        _run("counts", utc(2026, 1, 6, 0, 1), row_count=1),
        # 6 Jan local: counts succeed first, then a later run fails. The day shows the failure.
        _run("counts", utc(2026, 1, 6, 23, 0), row_count=1),
        _run("counts", utc(2026, 1, 7, 0, 10), error=ValueError("page changed")),
        # 6 Jan local: gauge fails; its last success is still the day before.
        _run("gauge", utc(2026, 1, 7, 0, 2), error=RuntimeError("https://x/?key=secret")),
    ):
        db.record_run(c, run)
    yield c
    c.close()


def read_rows(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_headers_match_contract(con, tmp_path):
    out = tmp_path / "out"
    dashboard.export(con, out, git_sha="abc1234", now=NOW)
    assert sorted(p.name for p in out.iterdir()) == sorted(CONTRACT)
    for name, header in CONTRACT.items():
        assert (out / name).read_bytes().split(b"\n")[0] == header.encode()


def test_source_status_latest_run_per_day(con, tmp_path):
    out = tmp_path / "out"
    dashboard.export(con, out, git_sha="abc1234", now=NOW)
    rows = [(r["run_date"], r["source"], r["status"], r["last_success_utc"], r["row_count"])
            for r in read_rows(out / "source_status.csv")]
    assert rows == [
        ("2026-01-05", "counts", "succeeded", "2026-01-06T00:01:00Z", "1"),
        ("2026-01-05", "gauge", "succeeded", "2026-01-06T00:05:00Z", "3"),
        ("2026-01-06", "counts", "failed", "2026-01-06T23:00:00Z", ""),
        ("2026-01-06", "gauge", "failed", "2026-01-06T00:05:00Z", ""),
    ]
    text = (out / "source_status.csv").read_text()
    assert "secret" not in text and "page changed" not in text
    for r in read_rows(out / "source_status.csv"):
        assert r["rows_expected"] == r["null_rate"] == r["null_rate_normal"] == ""


def test_never_succeeded_source_has_blank_last_success(tmp_path):
    c = db.connect(tmp_path / "test.duckdb")
    db.record_run(c, _run("counts", utc(2026, 1, 6, 0, 0), error=RuntimeError("x")))
    rows = dashboard.source_status(c)
    c.close()
    assert rows == [("2026-01-05", "counts", "failed", "", None, None, None, None)]


def test_meta(con, tmp_path):
    out = tmp_path / "out"
    dashboard.export(con, out, git_sha="abc1234", now=NOW)
    assert read_rows(out / "meta.csv") == [
        {"generated_at_utc": "2026-01-07T00:30:00Z", "git_sha": "abc1234"}]


def test_header_only_when_tables_missing(tmp_path):
    path = tmp_path / "empty.duckdb"
    duckdb.connect(str(path)).close()
    c = db.connect(path, read_only=True)  # read-only: no ingest_runs table is created
    out = tmp_path / "out"
    dashboard.export(c, out, git_sha="", now=NOW)
    c.close()
    for name, header in CONTRACT.items():
        if name != "meta.csv":
            assert (out / name).read_text(encoding="utf-8") == header + "\n"
    assert (out / "meta.csv").read_text(encoding="utf-8") == CONTRACT["meta.csv"] + "\n2026-01-07T00:30:00Z,\n"


def test_export_is_deterministic(con, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    dashboard.export(con, first, git_sha="abc1234", now=NOW)
    dashboard.export(con, second, git_sha="abc1234", now=NOW)
    for name in CONTRACT:
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_unchanged_data_rewrites_nothing(con, tmp_path):
    out = tmp_path / "out"
    dashboard.export(con, out, git_sha="abc1234", now=NOW)
    before = {name: (out / name).read_bytes() for name in CONTRACT}
    later = datetime(2026, 1, 8, 12, 0, tzinfo=timezone.utc)
    assert dashboard.export(con, out, git_sha="abc1234", now=later) == []
    assert {name: (out / name).read_bytes() for name in CONTRACT} == before


def test_meta_follows_data_and_code_changes(con, tmp_path):
    out = tmp_path / "out"
    dashboard.export(con, out, git_sha="abc1234", now=NOW)

    later = datetime(2026, 1, 8, 0, 30, tzinfo=timezone.utc)
    changed = dashboard.export(con, out, git_sha="def5678", now=later)
    assert [p.name for p in changed] == ["meta.csv"]
    assert read_rows(out / "meta.csv")[0]["git_sha"] == "def5678"

    db.record_run(con, _run("counts", utc(2026, 1, 8, 0, 0), row_count=2))
    latest = datetime(2026, 1, 9, 0, 30, tzinfo=timezone.utc)
    changed = dashboard.export(con, out, git_sha="def5678", now=latest)
    assert [p.name for p in changed] == ["source_status.csv", "meta.csv"]
    assert read_rows(out / "meta.csv")[0]["generated_at_utc"] == "2026-01-09T00:30:00Z"
