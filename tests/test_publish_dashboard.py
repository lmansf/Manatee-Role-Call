"""The publish job, against a throwaway git repo with a local bare remote. No network."""
import csv
import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from roll_call.ingest.base import IngestRun
from roll_call.storage import db

_spec = importlib.util.spec_from_file_location(
    "publish_dashboard", Path(__file__).resolve().parents[1] / "jobs" / "publish_dashboard.py")
publish_dashboard = importlib.util.module_from_spec(_spec)
sys.modules["publish_dashboard"] = publish_dashboard
_spec.loader.exec_module(publish_dashboard)

DATA_REL = "dashboard/sources/roll_call"
RECORDS_REL = "data/records"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path, monkeypatch):
    """Keep the machine's git config (signing, hooks, identity) out of the tests."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = Test\n\temail = test@example.com\n"
                   "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def remote(tmp_path):
    """A bare remote holding one commit of pipeline code."""
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "init", "--quiet", "-b", "main", str(seed))
    (seed / "roll_call").mkdir()
    (seed / "roll_call" / "__init__.py").write_text("")
    (seed / "README.md").write_text("seed\n")
    git(seed, "add", "--all")
    git(seed, "commit", "--quiet", "-m", "Seed")
    git(seed, "push", "--quiet", str(bare), "main")
    return bare


@pytest.fixture
def runner(tmp_path, remote):
    """The dedicated clone the timer runs from."""
    clone = tmp_path / "runner"
    git(tmp_path, "clone", "--quiet", str(remote), str(clone))
    return clone


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "test.duckdb")  # outside the clone, like a real .gitignored file
    db.record_run(c, IngestRun(source="counts",
                               started_at=datetime(2026, 1, 6, 0, 0, tzinfo=timezone.utc)).succeed(1))
    yield c
    c.close()


def publish(con, runner, **kwargs):
    return publish_dashboard.publish(con, repo=runner, data_dir=runner / DATA_REL,
                                     records_dir=runner / RECORDS_REL, branch="main", **kwargs)


def test_commits_and_pushes_only_the_data_folder(con, runner, remote):
    assert publish(con, runner) is True
    files = git(remote, "show", "--name-only", "--format=", "main").splitlines()
    assert sorted(files) == [f"{DATA_REL}/{name}" for name in
                             ("baseline_drift.csv", "meta.csv", "quarantine_queue.csv",
                              "source_status.csv")]
    assert git(remote, "log", "-1", "--format=%s", "main").startswith("Update dashboard data 20")
    with (runner / DATA_REL / "meta.csv").open(newline="") as f:
        meta = next(csv.DictReader(f))
    assert meta["git_sha"] == git(runner, "rev-parse", "HEAD~1")[:7]  # the seed, not the data commit


def test_no_commit_when_nothing_changed(con, runner, remote):
    assert publish(con, runner) is True
    head = git(remote, "rev-parse", "main")
    assert publish(con, runner) is False
    assert git(remote, "rev-parse", "main") == head
    assert git(runner, "status", "--porcelain") == ""


def test_new_data_makes_a_new_commit(con, runner, remote):
    publish(con, runner)
    db.record_run(con, IngestRun(source="counts",
                                 started_at=datetime(2026, 1, 7, 0, 0, tzinfo=timezone.utc)).succeed(2))
    assert publish(con, runner) is True
    assert git(remote, "rev-list", "--count", "main") == "3"


def test_no_push_commits_locally_only(con, runner, remote):
    before = git(remote, "rev-parse", "main")
    assert publish(con, runner, push=False) is True
    assert git(remote, "rev-parse", "main") == before
    assert git(runner, "rev-parse", "HEAD") != before


def test_refuses_on_wrong_branch(con, runner, remote):
    git(runner, "checkout", "--quiet", "-b", "feature")
    with pytest.raises(publish_dashboard.PublishError, match="feature"):
        publish(con, runner)
    assert not (runner / DATA_REL).exists()


@pytest.mark.parametrize("change", ["modified", "untracked"])
def test_refuses_when_other_files_are_dirty(con, runner, remote, change):
    if change == "modified":
        (runner / "README.md").write_text("edited\n")
    else:
        (runner / "notes.txt").write_text("stray\n")
    with pytest.raises(publish_dashboard.PublishError, match="uncommitted"):
        publish(con, runner)
    assert git(remote, "rev-list", "--count", "main") == "1"


def test_commits_records_with_the_dashboard_data(con, runner, remote):
    publish(con, runner)
    records = runner / RECORDS_REL
    records.mkdir(parents=True)
    (records / "clearing_decisions.csv").write_text("obs_id,decision\na,confirmed\n")
    assert publish(con, runner) is True
    files = git(remote, "show", "--name-only", "--format=", "main").splitlines()
    assert files == [f"{RECORDS_REL}/clearing_decisions.csv"]
    assert git(remote, "log", "-1", "--format=%s", "main").startswith("Update published data 20")


def test_dirty_records_do_not_hide_other_changes(con, runner, remote):
    (runner / RECORDS_REL).mkdir(parents=True)
    (runner / RECORDS_REL / "baseline_refreshes.csv").write_text("x\n")
    (runner / "README.md").write_text("edited\n")
    with pytest.raises(publish_dashboard.PublishError, match="README.md"):
        publish(con, runner)
    assert git(remote, "rev-list", "--count", "main") == "1"


def test_main_exits_non_zero_on_refusal(runner, tmp_path, monkeypatch):
    git(runner, "checkout", "--quiet", "-b", "feature")
    monkeypatch.setattr(publish_dashboard.config, "REPO_ROOT", runner)
    monkeypatch.setattr(publish_dashboard.config, "DASHBOARD_DATA_DIR", runner / DATA_REL)
    monkeypatch.setattr(publish_dashboard.config, "RECORDS_DIR", runner / RECORDS_REL)
    monkeypatch.setattr(publish_dashboard.config, "PUBLISH_BRANCH", "main")
    connect = db.connect
    monkeypatch.setattr(publish_dashboard.db, "connect", lambda: connect(tmp_path / "main.duckdb"))
    assert publish_dashboard.main(["--no-push"]) == 1
