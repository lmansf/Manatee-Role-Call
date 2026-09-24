"""Publish the dashboard: export the summary CSVs, then commit and push the published folders.

The last step of the daily run (jobs/ingest_daily.py), or by hand:

    .venv/bin/python jobs/publish_dashboard.py            # export, commit, push
    .venv/bin/python jobs/publish_dashboard.py --no-push  # export and commit, no push

Two folders are published. The dashboard data folder (config.DASHBOARD_DATA_DIR) feeds the
Evidence site, which Vercel rebuilds on every push to PUBLISH_BRANCH. data/records/ holds the
CSVs that jobs/backup_db.py writes each week. This job commits them too, so the backup never
leaves the runner clone dirty.

The timer runs from a dedicated clean "runner" clone. The job refuses to publish from any branch
but PUBLISH_BRANCH, or while files outside the two folders have uncommitted changes. Git's own
configured credentials (an SSH deploy key) do the pushing; this code never handles them.

Steps: check the branch, check the tree is clean, `git pull --ff-only`, export, stage the two
folders, commit if anything is staged, push. Any failure exits non-zero.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from roll_call import config
from roll_call.export import dashboard
from roll_call.storage import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("roll_call.jobs.publish_dashboard")

REMOTE = "origin"


class PublishError(RuntimeError):
    """The repo is not in a state the job may publish from."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=check)


def check_ready(repo: Path, publishable: list[str], branch: str) -> None:
    """Refuse to publish from the wrong branch or from a tree with other uncommitted changes."""
    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current != branch:
        raise PublishError(f"on branch {current!r}; dashboard data is published from {branch!r} only")
    excludes = [f":(exclude){rel}" for rel in publishable]
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=all", "--", ".", *excludes).stdout
    if dirty.strip():
        raise PublishError("uncommitted changes outside the published folders:\n" + dirty.rstrip())


def _known(repo: Path, rel: str) -> bool:
    """True when git add can match the path: it exists, or git still tracks files under it."""
    return (repo / rel).exists() or bool(_git(repo, "ls-files", "--", rel).stdout.strip())


def publish(con, repo: Path | None = None, data_dir: Path | None = None,
            records_dir: Path | None = None, branch: str | None = None,
            push: bool = True) -> bool:
    """Export the dashboard data, then commit it with any new records. Returns True when it
    made a commit.

    Takes an open connection so the daily run can pass its own. The other arguments default to
    the settings in roll_call.config. Raises PublishError when the repo is not ready, and
    CalledProcessError when a git command fails.
    """
    repo = Path(repo or config.REPO_ROOT).resolve()
    data_dir = Path(data_dir or config.DASHBOARD_DATA_DIR).resolve()
    records_dir = Path(records_dir or config.RECORDS_DIR).resolve()
    branch = branch or config.PUBLISH_BRANCH
    data_rel = data_dir.relative_to(repo).as_posix()
    records_rel = records_dir.relative_to(repo).as_posix()

    check_ready(repo, [data_rel, records_rel], branch)
    _git(repo, "pull", "--ff-only", REMOTE, branch)

    dashboard.export(con, data_dir, git_sha=dashboard.pipeline_sha(repo))

    staged = [rel for rel in (data_rel, records_rel) if _known(repo, rel)]
    _git(repo, "add", "--all", "--", *staged)
    if _git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        log.info("dashboard data and records unchanged; nothing to commit")
        return False
    records_changed = _git(repo, "diff", "--cached", "--quiet", "--", records_rel,
                           check=False).returncode != 0
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()
    message = f"Update {'published' if records_changed else 'dashboard'} data {today}"
    _git(repo, "commit", "--quiet", "-m", message)
    log.info("committed: %s", message)
    if push:
        _git(repo, "push", "--quiet", REMOTE, f"HEAD:{branch}")
        log.info("pushed to %s/%s", REMOTE, branch)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-push", action="store_true", help="commit locally but do not push")
    args = parser.parse_args(argv)
    con = db.connect()
    try:
        publish(con, push=not args.no_push)
    except PublishError as exc:
        log.error("refusing to publish: %s", exc)
        return 1
    except subprocess.CalledProcessError as exc:
        log.error("%s failed:\n%s", " ".join(exc.cmd), (exc.stderr or "").rstrip())
        return 1
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
