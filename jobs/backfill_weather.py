"""One-off: backfill the weather baseline from Open-Meteo reanalysis.

Spec §5: backfill the initial baseline from reanalysis rather than waiting months.
Pull in yearly chunks to keep each request small. Run once, then never again unless the
baseline needs rebuilding.

TODO(you): implement once open question 2 (fields, forecasts) is decided. The field
set chosen here is the field set the baseline is computed on; changing it later means
re-backfilling, which is fine but should be a conscious act.
"""
from __future__ import annotations


def main(start_year: int, end_year: int) -> None:
    raise NotImplementedError


if __name__ == "__main__":
    import sys

    main(int(sys.argv[1]), int(sys.argv[2]))
