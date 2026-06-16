"""
tests/test_bulk_approve.py
Tests for reviews.bulk_approve. Deterministic, no network.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline-api"))

from reviews import bulk_approve, get_reviews


def _write_reviews(run_dir, data):
    (run_dir / "reviews.json").write_text(json.dumps(data), encoding="utf-8")


class TestBulkApprove:

    def test_approves_pending_records(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        count = bulk_approve("R1", ["rec-1", "rec-2"], "analyst", run_dir)
        assert count == 2
        saved = get_reviews("R1", run_dir)
        assert saved["rec-1"]["qc_status"] == "approved"
        assert saved["rec-2"]["qc_status"] == "approved"
        assert saved["rec-1"]["reviewed_by"] == "analyst"

    def test_skips_already_approved(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_reviews(run_dir, {"rec-1": {"qc_status": "approved", "reviewed_by": "prev"}})
        count = bulk_approve("R1", ["rec-1", "rec-2"], "analyst", run_dir)
        assert count == 1  # only rec-2 approved
        saved = get_reviews("R1", run_dir)
        assert saved["rec-1"]["reviewed_by"] == "prev"   # untouched
        assert saved["rec-2"]["qc_status"] == "approved"

    def test_empty_list_is_noop(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        count = bulk_approve("R1", [], "analyst", run_dir)
        assert count == 0
        assert not (run_dir / "reviews.json").exists()

    def test_preserves_existing_analyst_note(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_reviews(run_dir, {"rec-1": {"qc_status": "pending", "analyst_note": "good signal"}})
        bulk_approve("R1", ["rec-1"], "analyst", run_dir)
        saved = get_reviews("R1", run_dir)
        assert saved["rec-1"]["analyst_note"] == "good signal"

    def test_preserves_existing_override_tier(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_reviews(run_dir, {"rec-1": {
            "qc_status": "pending",
            "override_tier": "Contender",
            "override_reason": "Manual override",
        }})
        bulk_approve("R1", ["rec-1"], "analyst", run_dir)
        saved = get_reviews("R1", run_dir)
        assert saved["rec-1"]["override_tier"] == "Contender"
        assert saved["rec-1"]["override_reason"] == "Manual override"
        assert saved["rec-1"]["qc_status"] == "approved"

    def test_single_atomic_write(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        count = bulk_approve("R1", ["a", "b", "c"], "analyst", run_dir)
        assert count == 3
        reviews_path = run_dir / "reviews.json"
        assert reviews_path.exists()
        saved = json.loads(reviews_path.read_text())
        assert set(saved.keys()) == {"a", "b", "c"}
