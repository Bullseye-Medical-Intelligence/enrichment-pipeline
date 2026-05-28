"""
Tests for pre-flight CSV inspection (validator.preflight_summary).

Deterministic — no network, no subprocess. Covers the import summary used by
the upload confirmation modal: row counts, duplicate detection, dropped rows,
and data-quality warnings. Hard validation failures still raise ValueError.
"""

import os
import sys
from pathlib import Path

import pytest

# pipeline-api modules import each other by bare name; put the dir on the path.
_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

# Configure required env BEFORE importing config-bound modules.
os.environ.setdefault("PIPELINE_API_KEY", "test-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import validator  # noqa: E402


def _csv(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


_HEADER = "name,phone,site,type,state"


def test_summary_counts_clean_rows():
    content = _csv(
        _HEADER,
        "Acme Clinic,555-1000,https://acme.example,OBGYN,TX",
        "Beta Clinic,555-2000,https://beta.example,OBGYN,FL",
    )
    summary = validator.preflight_summary(content, "outscraper")
    assert summary["row_count"] == 2
    assert summary["importable"] == 2
    assert summary["duplicate_count"] == 0
    assert summary["dropped_count"] == 0
    assert summary["warnings"] == []


def test_summary_detects_duplicate_by_website():
    content = _csv(
        _HEADER,
        "Acme Clinic,555-1000,https://acme.example/,OBGYN,TX",
        "Acme Clinic Duplicate,555-9999,https://acme.example,OBGYN,TX",
    )
    summary = validator.preflight_summary(content, "outscraper")
    assert summary["duplicate_count"] == 1
    assert any("duplicate" in w.lower() for w in summary["warnings"])


def test_summary_warns_when_type_column_missing():
    content = _csv(
        "name,phone,site",
        "Acme Clinic,555-1000,https://acme.example",
    )
    summary = validator.preflight_summary(content, "outscraper")
    assert any("type" in w.lower() for w in summary["warnings"])


def test_summary_counts_rows_missing_name_as_dropped():
    content = _csv(
        _HEADER,
        ",555-1000,https://acme.example,OBGYN,TX",
        "Beta Clinic,555-2000,https://beta.example,OBGYN,FL",
    )
    summary = validator.preflight_summary(content, "outscraper")
    assert summary["dropped_count"] == 1
    assert summary["importable"] == 1
    assert any("no practice name" in w.lower() for w in summary["warnings"])


def test_summary_raises_on_missing_required_column():
    # Outscraper requires a website column; this CSV has none.
    content = _csv("name,phone", "Acme Clinic,555-1000")
    with pytest.raises(ValueError):
        validator.preflight_summary(content, "outscraper")


def test_summary_raises_on_empty_csv():
    content = _csv(_HEADER)  # header only, no data rows
    with pytest.raises(ValueError):
        validator.preflight_summary(content, "outscraper")
