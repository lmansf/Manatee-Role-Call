"""One-off: backfill weather history from the Open-Meteo archive.

Feeds two things: training features for the historical seasons (2018-19 onward), and the
weather baseline, which is the normal for each day of the year over the trailing 30 years
(spec §5). The replayed baseline refreshes need history 30 years before the earliest replayed
year, so backfill from about 1988.

Pull one calendar year per request to keep each raw payload small, and write through
db.write_ingest_result like any other run. Run once; re-run only to rebuild.

TODO(you): implement. The field set is fixed (open_meteo.DAILY_FIELDS, HOURLY_FIELDS).
Changing it later means re-backfilling, which is fine but should be a conscious act.
"""
from __future__ import annotations


def main(start_year: int, end_year: int) -> None:
    raise NotImplementedError


if __name__ == "__main__":
    import sys

    main(int(sys.argv[1]), int(sys.argv[2]))
