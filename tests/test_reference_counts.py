"""Schema checks on the hand-transcribed reference set. A typo in the CSV should fail here,
not surface later as a parser 'bug'."""
import csv
from datetime import date
from pathlib import Path

CSV = Path(__file__).resolve().parents[1] / "data" / "reference" / "blue_spring_manatee_counts_2025_2026.csv"
COLUMNS = ["date", "count", "count_source", "late_arrivals", "derived", "notes"]
SOURCES = {"SMC roll call", "SMC estimate", "Park staff"}


def _rows():
    with CSV.open(newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == COLUMNS
        return list(reader)


def test_dates_are_iso_sorted_and_unique():
    dates = [date.fromisoformat(r["date"]) for r in _rows()]
    assert dates == sorted(dates)
    assert len(dates) == len(set(dates))


def test_counts_are_non_negative_integers():
    for r in _rows():
        assert r["count"].isdigit(), r
        assert r["late_arrivals"] == "" or r["late_arrivals"].isdigit(), r


def test_enums():
    for r in _rows():
        assert r["count_source"] in SOURCES, r
        assert r["derived"] in {"TRUE", "FALSE"}, r
