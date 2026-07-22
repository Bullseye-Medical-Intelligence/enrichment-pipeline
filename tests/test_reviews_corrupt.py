"""
test_reviews_corrupt.py

Fail-closed handling of a corrupt analyst-review overlay (reviews.json).

A reviews.json that exists but cannot be parsed used to read as {} — the next
save then atomically replaced the file with only the entry being touched,
erasing every prior analyst note, approval, override tier, and signal edit.
Now every reader raises reviews.ReviewsLoadError, every write path aborts
before mutating anything, and the damaged file's bytes are preserved exactly.

Deterministic — no network, no subprocess, no LLM.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import reviews  # noqa: E402
import runner  # noqa: E402
import runs  # noqa: E402
from schema import ReviewEdit, SignalOverride  # noqa: E402

_RUN_ID = "RUN-20260711-090000-abcd"

# A prior overlay worth protecting, then damaged in place.
_PRIOR_OVERLAY = {
    "T-1": {
        "analyst_note": "confirmed by phone", "override_tier": "Bullseye",
        "override_reason": "verified cash pay", "qc_status": "approved",
        "reviewed_by": "ana", "reviewed_at": "2026-07-01T10:00:00+00:00",
        "extra_sales_angles": ["They sell Botox."], "signal_overrides": {},
    },
}
_CORRUPT_BYTES = b'{"T-1": {"analyst_note": "confirmed by ph'  # truncated write


def _record(rid: str) -> dict:
    return {
        "id": rid, "practice_name": f"Clinic {rid}", "bullseye_score": 80,
        "target_tier": "Contender", "exclusion_status": "CLEAR",
        "source_confidence": "high",
        "signals": [{"signal_id": "S-01", "signal_state": "not_found",
                     "evidence_text": "", "source_url": "", "confidence": "low"}],
    }


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """A complete run whose reviews.json is corrupt (truncated mid-write)."""
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path)
    rd = tmp_path / _RUN_ID
    rd.mkdir(parents=True)
    (rd / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-07-11T09:00:00+00:00", "operator": "tester",
        "records_input": 1, "records_output": 1,
    }))
    (rd / "enriched_targets.json").write_text(json.dumps(
        {"run_id": _RUN_ID, "records": [_record("T-1")]}))
    (rd / "reviews.json").write_bytes(_CORRUPT_BYTES)
    return rd


@pytest.fixture
def client(run_dir):
    with TestClient(main.app, follow_redirects=False) as c:
        r = c.post("/login", data={"username": "tester", "password": "secret-pw"})
        assert r.status_code in (200, 302, 303)
        yield c


def _assert_untouched(run_dir):
    """The damaged file must be preserved byte-for-byte."""
    assert (run_dir / "reviews.json").read_bytes() == _CORRUPT_BYTES


# ---------------------------------------------------------------------------
# get_reviews contract
# ---------------------------------------------------------------------------

def test_missing_file_is_valid_empty_state(tmp_path):
    assert reviews.get_reviews(_RUN_ID, tmp_path) == {}


def test_corrupt_file_raises_load_error(run_dir):
    with pytest.raises(reviews.ReviewsLoadError) as exc:
        reviews.get_reviews(_RUN_ID, run_dir)
    # Operator-facing recovery message names the file and the fail-closed intent.
    assert "reviews.json" in str(exc.value)
    assert "No changes were written" in str(exc.value)
    _assert_untouched(run_dir)


def test_non_object_root_raises_load_error(tmp_path):
    (tmp_path / "reviews.json").write_text('["not", "a", "dict"]')
    with pytest.raises(reviews.ReviewsLoadError):
        reviews.get_reviews(_RUN_ID, tmp_path)


def test_malformed_entry_raises_load_error(tmp_path):
    """A non-object entry value fails closed like a non-object root — merging
    over it could erase analyst work."""
    (tmp_path / "reviews.json").write_text(
        '{"T-good": {"qc_status": "approved"}, "T-bad": "just a string"}'
    )
    with pytest.raises(reviews.ReviewsLoadError) as exc:
        reviews.get_reviews(_RUN_ID, tmp_path)
    assert "T-bad" in str(exc.value)


def test_unreadable_file_raises_load_error(run_dir, monkeypatch):
    def _boom(*a, **k):
        raise OSError("I/O error reading device")
    monkeypatch.setattr("builtins.open", _boom)
    with pytest.raises(reviews.ReviewsLoadError):
        reviews.get_reviews(_RUN_ID, run_dir)


# ---------------------------------------------------------------------------
# Every write path aborts without replacing the file
# ---------------------------------------------------------------------------

def test_save_review_fails_closed(run_dir):
    edit = ReviewEdit(analyst_note="new note", override_tier=None,
                      override_reason=None, qc_status="approved")
    with pytest.raises(reviews.ReviewsLoadError):
        reviews.save_review(_RUN_ID, "T-1", edit, "tester", run_dir)
    _assert_untouched(run_dir)


def test_bulk_approve_fails_closed(run_dir):
    with pytest.raises(reviews.ReviewsLoadError):
        reviews.bulk_approve(_RUN_ID, ["T-1"], "tester", run_dir)
    _assert_untouched(run_dir)


def test_bulk_set_qc_status_fails_closed(run_dir):
    with pytest.raises(reviews.ReviewsLoadError):
        reviews.bulk_set_qc_status(run_dir, ["T-1"], "approved", "tester")
    _assert_untouched(run_dir)


def test_save_signal_override_fails_closed(run_dir):
    ov = SignalOverride(signal_id="S-01", override_state="yes",
                        source_url="https://x.example.com",
                        override_note="verified", override_by="tester")
    with pytest.raises(reviews.ReviewsLoadError):
        reviews.save_signal_override(_RUN_ID, "T-1", ov, run_dir)
    _assert_untouched(run_dir)


def test_stamp_reenriched_fails_closed(run_dir):
    with pytest.raises(reviews.ReviewsLoadError):
        reviews.stamp_reenriched(_RUN_ID, "T-1", run_dir, "browser re-crawl")
    _assert_untouched(run_dir)


# ---------------------------------------------------------------------------
# Route-level behavior: clear operator-facing error, damaged file untouched
# ---------------------------------------------------------------------------

def test_save_review_route_409(client, run_dir):
    r = client.post(f"/api/ui/reviews/{_RUN_ID}/T-1", json={
        "analyst_note": "n", "override_tier": None,
        "override_reason": None, "qc_status": "approved"})
    assert r.status_code == 409
    assert "could not be read" in r.json()["detail"]
    _assert_untouched(run_dir)


def test_add_sales_angle_route_409(client, run_dir):
    r = client.post(f"/api/ui/reviews/{_RUN_ID}/T-1/add-sales-angle",
                    json={"angle": "They sell Botox."})
    assert r.status_code == 409
    _assert_untouched(run_dir)


def test_bulk_review_route_409(client, run_dir):
    r = client.post(f"/dashboard/{_RUN_ID}/bulk-review",
                    data={"record_ids": ["T-1"], "action": "accept"})
    assert r.status_code == 409
    _assert_untouched(run_dir)


def test_dashboard_read_view_surfaces_damage_not_empty_state(client, run_dir):
    """A corrupt overlay must not render as a healthy 'no reviews' dashboard."""
    r = client.get(f"/dashboard/{_RUN_ID}")
    assert r.status_code == 409
    assert "could not be read" in r.text
    _assert_untouched(run_dir)


# ---------------------------------------------------------------------------
# Background merge paths skip the stamp but never abort or wipe
# ---------------------------------------------------------------------------

class _FakeProcess:
    returncode = 0

    def communicate(self):
        return (b"", b"")


def test_batch_reenrich_merge_survives_corrupt_overlay(run_dir, monkeypatch):
    """The merge persists (LLM spend already happened), the stamp is skipped,
    and the damaged overlay is untouched."""
    monkeypatch.setattr(runner.runs, "OUTPUT_RUNS_PATH", run_dir.parent)
    scratch = run_dir / ".recrawl_corrupt_test"
    scratch.mkdir()
    updated = _record("T-1")
    updated["target_tier"] = "Bullseye"
    updated["signals"][0]["signal_state"] = "yes"
    updated["signals"][0]["evidence_text"] = "we offer IUI"
    (scratch / "enriched_targets.json").write_text(
        json.dumps({"records": [updated]}))
    (scratch / "run_log.json").write_text(json.dumps(
        {"run_id": "x", "records_output": 1, "records_excluded": 0,
         "records_failed": 0}))

    asyncio.run(runner._monitor_batch_reenrich(
        _RUN_ID, scratch, ["T-1"], _FakeProcess()))

    merged = json.loads((run_dir / "enriched_targets.json").read_text())
    assert merged["records"][0]["target_tier"] == "Bullseye"  # merge persisted
    _assert_untouched(run_dir)                                # overlay preserved
