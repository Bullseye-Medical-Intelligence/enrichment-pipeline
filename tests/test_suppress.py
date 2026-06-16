"""
tests/test_suppress.py
Tests for suppress_run.py. Deterministic, no network, no LLM.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from suppress_run import run_suppress_pass, run_suppress_preview


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    practice_name="Test Clinic",
    npi_number="1234567890",
    address_zip="78701",
    address_state="TX",
    customer_suppressed=False,
) -> dict:
    rec = {
        "record_id": f"rec-{practice_name[:4]}",
        "practice_name": practice_name,
        "npi_number": npi_number,
        "address_zip": address_zip,
        "address_state": address_state,
        "enrichment_status": "enriched",
        "target_tier": "Contender",
        "bullseye_score": 55,
        "exclusion_status": "CLEAR",
        "exclusion_reason": "",
        "exclusion_primary_gate": "",
        "signals": [],
        "call_brief": {},
        "_customer_suppressed": False,
        "_suppression_reason": "",
    }
    if customer_suppressed:
        rec["_customer_suppressed"] = True
        rec["exclusion_status"] = "EXCLUDED"
        rec["target_tier"] = "Excluded"
    return rec


def _write_targets(run_dir, records):
    data = {"run_id": "RUN-20260101-120000", "records": records}
    (run_dir / "enriched_targets.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def _write_suppression(path, rows):
    """Write a suppression CSV with NPI, name, zip, state columns."""
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["npi_number", "practice_name", "address_zip", "address_state"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# run_suppress_preview
# ---------------------------------------------------------------------------

class TestSuppressPreview:

    def test_identifies_matching_records(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        supp_csv = tmp_path / "supp.csv"
        _write_targets(run_dir, [
            _make_record(practice_name="Alpha Clinic", npi_number="1111111111"),
            _make_record(practice_name="Beta Clinic", npi_number="2222222222"),
        ])
        _write_suppression(supp_csv, [
            {"npi_number": "1111111111", "practice_name": "Alpha Clinic",
             "address_zip": "78701", "address_state": "TX"},
        ])
        stats = run_suppress_preview(run_dir, supp_csv)
        assert stats["preview"] is True
        assert stats["would_suppress"] == 1
        assert "Alpha Clinic" in stats["would_suppress_names"]
        assert stats["already_suppressed"] == 0

    def test_skips_already_suppressed(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        supp_csv = tmp_path / "supp.csv"
        _write_targets(run_dir, [
            _make_record(practice_name="Alpha Clinic", npi_number="1111111111",
                         customer_suppressed=True),
        ])
        _write_suppression(supp_csv, [
            {"npi_number": "1111111111", "practice_name": "Alpha Clinic",
             "address_zip": "78701", "address_state": "TX"},
        ])
        stats = run_suppress_preview(run_dir, supp_csv)
        assert stats["would_suppress"] == 0
        assert stats["already_suppressed"] == 1

    def test_preview_does_not_write(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        supp_csv = tmp_path / "supp.csv"
        _write_targets(run_dir, [_make_record(npi_number="1111111111")])
        _write_suppression(supp_csv, [
            {"npi_number": "1111111111", "practice_name": "Test Clinic",
             "address_zip": "78701", "address_state": "TX"},
        ])
        targets_path = run_dir / "enriched_targets.json"
        mtime_before = targets_path.stat().st_mtime
        run_suppress_preview(run_dir, supp_csv)
        assert targets_path.stat().st_mtime == mtime_before

    def test_no_matches_returns_zero(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        supp_csv = tmp_path / "supp.csv"
        _write_targets(run_dir, [_make_record(npi_number="9999999999")])
        _write_suppression(supp_csv, [
            {"npi_number": "1111111111", "practice_name": "Other Clinic",
             "address_zip": "10001", "address_state": "NY"},
        ])
        stats = run_suppress_preview(run_dir, supp_csv)
        assert stats["would_suppress"] == 0
        assert stats["total"] == 1

    def test_missing_targets_raises(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        supp_csv = tmp_path / "supp.csv"
        _write_suppression(supp_csv, [
            {"npi_number": "1111111111", "practice_name": "X",
             "address_zip": "78701", "address_state": "TX"},
        ])
        with pytest.raises(FileNotFoundError):
            run_suppress_preview(run_dir, supp_csv)


# ---------------------------------------------------------------------------
# run_suppress_pass
# ---------------------------------------------------------------------------

class TestSuppressPass:

    def test_suppresses_matched_records(self, tmp_path, monkeypatch):
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc
        monkeypatch.setattr(ec, "apply_exclusions", lambda r, cfg: None)
        monkeypatch.setattr(sc, "validate_and_finalize", lambda r: r)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        supp_csv = tmp_path / "supp.csv"
        _write_targets(run_dir, [
            _make_record(practice_name="Alpha Clinic", npi_number="1111111111"),
        ])
        _write_suppression(supp_csv, [
            {"npi_number": "1111111111", "practice_name": "Alpha Clinic",
             "address_zip": "78701", "address_state": "TX"},
        ])
        stats = run_suppress_pass(run_dir, supp_csv)
        assert stats["newly_suppressed"] == 1
        assert "Alpha Clinic" in stats["newly_suppressed_names"]

        saved = json.loads((run_dir / "enriched_targets.json").read_text())
        assert saved["records"][0]["_customer_suppressed"] is True

    def test_skips_already_suppressed(self, tmp_path, monkeypatch):
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc
        monkeypatch.setattr(ec, "apply_exclusions", lambda r, cfg: None)
        monkeypatch.setattr(sc, "validate_and_finalize", lambda r: r)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        supp_csv = tmp_path / "supp.csv"
        _write_targets(run_dir, [
            _make_record(practice_name="Alpha Clinic", npi_number="1111111111",
                         customer_suppressed=True),
        ])
        _write_suppression(supp_csv, [
            {"npi_number": "1111111111", "practice_name": "Alpha Clinic",
             "address_zip": "78701", "address_state": "TX"},
        ])
        stats = run_suppress_pass(run_dir, supp_csv)
        assert stats["newly_suppressed"] == 0
        assert stats["already_suppressed"] == 1

    def test_no_write_when_no_matches(self, tmp_path, monkeypatch):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        supp_csv = tmp_path / "supp.csv"
        _write_targets(run_dir, [_make_record(npi_number="9999999999")])
        _write_suppression(supp_csv, [
            {"npi_number": "1111111111", "practice_name": "Other",
             "address_zip": "10001", "address_state": "NY"},
        ])
        targets_path = run_dir / "enriched_targets.json"
        mtime_before = targets_path.stat().st_mtime
        stats = run_suppress_pass(run_dir, supp_csv)
        assert stats["newly_suppressed"] == 0
        assert targets_path.stat().st_mtime == mtime_before

    def test_writes_atomically(self, tmp_path, monkeypatch):
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc
        monkeypatch.setattr(ec, "apply_exclusions", lambda r, cfg: None)
        monkeypatch.setattr(sc, "validate_and_finalize", lambda r: r)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        supp_csv = tmp_path / "supp.csv"
        _write_targets(run_dir, [_make_record(npi_number="1111111111")])
        _write_suppression(supp_csv, [
            {"npi_number": "1111111111", "practice_name": "Test Clinic",
             "address_zip": "78701", "address_state": "TX"},
        ])
        run_suppress_pass(run_dir, supp_csv)
        assert not (run_dir / "enriched_targets.json.tmp").exists()

    def test_preserves_non_matching_records(self, tmp_path, monkeypatch):
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc
        monkeypatch.setattr(ec, "apply_exclusions", lambda r, cfg: None)
        monkeypatch.setattr(sc, "validate_and_finalize", lambda r: r)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        supp_csv = tmp_path / "supp.csv"
        _write_targets(run_dir, [
            _make_record(practice_name="Alpha Clinic", npi_number="1111111111"),
            _make_record(practice_name="Beta Clinic", npi_number="2222222222"),
        ])
        _write_suppression(supp_csv, [
            {"npi_number": "1111111111", "practice_name": "Alpha Clinic",
             "address_zip": "78701", "address_state": "TX"},
        ])
        run_suppress_pass(run_dir, supp_csv)
        saved = json.loads((run_dir / "enriched_targets.json").read_text())
        records = saved["records"]
        alpha = next(r for r in records if r["practice_name"] == "Alpha Clinic")
        beta = next(r for r in records if r["practice_name"] == "Beta Clinic")
        assert alpha["_customer_suppressed"] is True
        assert beta.get("_customer_suppressed") is False

    def test_missing_targets_raises(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        supp_csv = tmp_path / "supp.csv"
        _write_suppression(supp_csv, [
            {"npi_number": "1111111111", "practice_name": "X",
             "address_zip": "78701", "address_state": "TX"},
        ])
        with pytest.raises(FileNotFoundError):
            run_suppress_pass(run_dir, supp_csv)

    def test_preserves_wrapper_envelope(self, tmp_path, monkeypatch):
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc
        monkeypatch.setattr(ec, "apply_exclusions", lambda r, cfg: None)
        monkeypatch.setattr(sc, "validate_and_finalize", lambda r: r)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        supp_csv = tmp_path / "supp.csv"
        _write_targets(run_dir, [_make_record(npi_number="1111111111")])
        _write_suppression(supp_csv, [
            {"npi_number": "1111111111", "practice_name": "Test Clinic",
             "address_zip": "78701", "address_state": "TX"},
        ])
        # Add extra envelope metadata
        path = run_dir / "enriched_targets.json"
        data = json.loads(path.read_text())
        data["generated_at"] = "2026-01-01T12:00:00Z"
        path.write_text(json.dumps(data))

        run_suppress_pass(run_dir, supp_csv)
        result = json.loads(path.read_text())
        assert result.get("run_id") == "RUN-20260101-120000"
        assert result.get("generated_at") == "2026-01-01T12:00:00Z"
