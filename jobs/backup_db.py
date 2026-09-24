"""Weekly backup: export the records only a person can make, then copy the database file.

Started by the weekly systemd timer (docs/scheduling.md), or by hand:

    .venv/bin/python jobs/backup_db.py

1. Clearing decisions and the baseline refresh log are written as CSV to data/records/.
   Commit those files: they are the history no source can give back.
2. The DuckDB file is copied to BACKUP_DIR (from .env), a folder on a second disk, keeping the
   newest few copies. The job fails loudly if BACKUP_DIR is missing or not mounted, so a
   detached disk shows up as a failed unit rather than a quietly skipped backup.
"""
from __future__ import annotations

import logging
import shutil
from datetime import date
from pathlib import Path

from roll_call import config
from roll_call.storage import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("roll_call.jobs.backup_db")

KEEP_COPIES = 8


def export_records(con, out_dir: Path = config.RECORDS_DIR) -> list[Path]:
    """Write each record table that exists to CSV, ordered for stable diffs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    written = []
    for table in config.RECORD_TABLES:
        if table not in existing:
            log.info("%s does not exist yet; nothing to export", table)
            continue
        path = out_dir / f"{table}.csv"
        con.execute(f"COPY (SELECT * FROM {table} ORDER BY ALL) TO '{path}' (HEADER)")
        written.append(path)
    return written


def copy_database(db_path: Path, backup_dir: Path, keep: int = KEEP_COPIES) -> Path:
    """Copy the (closed, checkpointed) database file to backup_dir and prune old copies."""
    if not backup_dir.is_dir():
        raise RuntimeError(f"BACKUP_DIR {backup_dir} does not exist; is the second disk mounted?")
    target = backup_dir / f"{db_path.stem}_{date.today().isoformat()}{db_path.suffix}"
    shutil.copy2(db_path, target)
    copies = sorted(backup_dir.glob(f"{db_path.stem}_*{db_path.suffix}"))
    for old in copies[:-keep]:
        old.unlink()
    return target


def main() -> int:
    backup_dir = Path(config.env("BACKUP_DIR"))
    con = db.connect()
    try:
        for path in export_records(con):
            log.info("exported %s", path.relative_to(config.REPO_ROOT))
        con.execute("CHECKPOINT")  # fold the write-ahead log into the file before copying
    finally:
        con.close()
    target = copy_database(config.DB_PATH, backup_dir)
    log.info("copied database to %s", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
