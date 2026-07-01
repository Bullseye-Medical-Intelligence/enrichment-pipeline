"""Tests for output.csv_writer — flat CSV export and formula-injection escaping.

Deterministic: write_csv writes to a tmp dir; no network, no API.
"""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from output.csv_writer import _escape_csv_cell, write_csv  # noqa: E402


def test_escape_neutralizes_formula_prefixes():
    for bad in ("=1+1", "+1", "-1", "@cmd", "\tx", "\rx"):
        assert _escape_csv_cell(bad) == "'" + bad


def test_escape_leaves_safe_cells_untouched():
    for ok in ("Women's Health", "123 Main St", "", "a=b-c"):
        assert _escape_csv_cell(ok) == ok


def test_write_csv_escapes_malicious_fields(tmp_path):
    records = [{
        "id": "T-1",
        "practice_name": '=HYPERLINK("http://evil","Clinic")',
        "phone": "+15125551234",
        "bullseye_score": 90,
        "target_tier": "Bullseye",
    }]
    path = write_csv(records, output_dir=str(tmp_path))
    with open(path, newline="", encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert row["practice_name"].startswith("'="), "formula-injection name not neutralized"
    assert row["phone"].startswith("'+"), "leading-+ phone not neutralized"
