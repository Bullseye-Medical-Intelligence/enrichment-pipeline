"""
test_bulk_review_route.py

Route + template tests for the grouped header dropdowns and the bulk-QC bar:
POST /dashboard/{run_id}/bulk-review (action=accept|reject|reset) and the
Reprocess / Export / Audit dropdown toggles on the results page.

Deterministic — no network, no subprocess, no LLM. Uses the FastAPI TestClient
against a mock run directory, mirroring tests/test_signal_override_route.py.

Guarantees under test: the results page renders the new dropdown toggles for a
complete run; bulk-review maps accept/reject/reset onto qc_status and stamps (or
clears) reviewed_by/reviewed_at; the route requires auth; an invalid action is
rejected (4xx); and bulk-review never writes enriched_targets.json.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

# pipeline-api modules import each other by bare name; put the dir on the path.
_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

# Configure required env BEFORE importing config-bound modules.
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import runs  # noqa: E402

_RUN_ID = "RUN-20260621-150000-cccc"
_MISSING_RUN_ID = "RUN-20260621-160000-dddd"
_BULK_PATH = f"/dashboard/{_RUN_ID}/bulk-review"


def _record(rid: str, name: str):
    """Minimal enriched record with a tier, score, and one signal."""
    return {
        "id": rid,
        "practice_name": name,
        "bullseye_score": 64,
        "fit_signal_score": 60,
        "confidence_score": 70,
        "target_tier": "Contender",
        "exclusion_status": "CLEAR",
        "source_confidence": "high",
        "signals": [
            {"signal_id": "S-ICP-001", "signal_label": "IUI offered",
             "signal_state": "yes", "evidence_text": "we offer IUI",
             "source_url": "https://x.example.com", "confidence": "high"},
        ],
    }


def _write_run(run_directory):
    """Write a complete run dir: status.json + enriched_targets.json (3 records)."""
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-06-21T15:00:00+00:00", "operator": "tester",
        "records_input": 3, "records_output": 3,
    }))
    (run_directory / "enriched_targets.json").write_text(json.dumps({
        "run_id": _RUN_ID,
        "records": [
            _record("T-1", "Acme Women's Health"),
            _record("T-2", "Bright OBGYN"),
            _record("T-3", "Cedar Fertility"),
        ],
    }, indent=2))


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    """Point OUTPUT_RUNS_PATH at tmp_path and create a complete run."""
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path)
    run_directory = tmp_path / _RUN_ID
    _write_run(run_directory)
    return run_directory


@pytest.fixture
def client(run_env):
    """Logged-in TestClient (does NOT follow redirects) sharing the run env."""
    with TestClient(main.app, follow_redirects=False) as c:
        r = c.post("/login", data={"username": "tester", "password": "secret-pw"})
        assert r.status_code in (200, 303, 302)
        yield c


def _reviews(run_env) -> dict:
    """Read the persisted reviews.json (or {} when absent)."""
    p = run_env / "reviews.json"
    return json.loads(p.read_text()) if p.exists() else {}


# ---------------------------------------------------------------------------
# 1 — results page renders the grouped header dropdown toggles
# ---------------------------------------------------------------------------

def test_results_page_renders_header_dropdowns(client):
    r = client.get(f"/dashboard/{_RUN_ID}")
    assert r.status_code == 200
    html = r.text
    assert "Reprocess ▾" in html
    assert "Export ▾" in html
    assert "Audit ▾" in html
    # Review All bar control + the upward-opening menu modifier.
    assert "Review All ▾" in html
    assert "drop-up" in html


# ---------------------------------------------------------------------------
# 2 — accept → approved, with reviewed_by stamped
# ---------------------------------------------------------------------------

def test_bulk_accept_sets_approved(client, run_env):
    r = client.post(_BULK_PATH, data={"action": "accept", "record_ids": ["T-1", "T-2"]})
    assert r.status_code == 303
    stored = _reviews(run_env)
    for rid in ("T-1", "T-2"):
        assert stored[rid]["qc_status"] == "approved"
        assert stored[rid]["reviewed_by"] == "tester"
        assert stored[rid]["reviewed_at"]
    assert "T-3" not in stored  # untouched


# ---------------------------------------------------------------------------
# 3 — reject → rejected
# ---------------------------------------------------------------------------

def test_bulk_reject_sets_rejected(client, run_env):
    r = client.post(_BULK_PATH, data={"action": "reject", "record_ids": ["T-3"]})
    assert r.status_code == 303
    stored = _reviews(run_env)
    assert stored["T-3"]["qc_status"] == "rejected"
    assert stored["T-3"]["reviewed_by"] == "tester"
    assert stored["T-3"]["reviewed_at"]


# ---------------------------------------------------------------------------
# 4 — reset → pending and clears reviewed_at / reviewed_by
# ---------------------------------------------------------------------------

def test_bulk_reset_clears_review_meta(client, run_env):
    # First approve so there is meta to clear, then reset it.
    client.post(_BULK_PATH, data={"action": "accept", "record_ids": ["T-1"]})
    assert _reviews(run_env)["T-1"]["reviewed_at"] is not None

    r = client.post(_BULK_PATH, data={"action": "reset", "record_ids": ["T-1"]})
    assert r.status_code == 303
    entry = _reviews(run_env)["T-1"]
    assert entry["qc_status"] == "pending"
    assert entry["reviewed_at"] is None
    assert entry["reviewed_by"] is None


# ---------------------------------------------------------------------------
# 5 — existing signal_overrides / extra_sales_angles are preserved
# ---------------------------------------------------------------------------

def test_bulk_review_preserves_overlay(client, run_env):
    # Seed a review with overrides + angles, then bulk-accept.
    seeded = {
        "T-2": {
            "analyst_note": "keep me",
            "override_tier": None,
            "override_reason": None,
            "qc_status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "extra_sales_angles": ["angle one"],
            "signal_overrides": {"S-ICP-001": {"override_state": "yes"}},
        }
    }
    (run_env / "reviews.json").write_text(json.dumps(seeded))

    r = client.post(_BULK_PATH, data={"action": "accept", "record_ids": ["T-2"]})
    assert r.status_code == 303
    entry = _reviews(run_env)["T-2"]
    assert entry["qc_status"] == "approved"
    assert entry["analyst_note"] == "keep me"
    assert entry["extra_sales_angles"] == ["angle one"]
    assert entry["signal_overrides"] == {"S-ICP-001": {"override_state": "yes"}}


# ---------------------------------------------------------------------------
# 6 — invalid action is rejected (4xx) and writes nothing
# ---------------------------------------------------------------------------

def test_invalid_action_rejected(client, run_env):
    r = client.post(_BULK_PATH, data={"action": "delete", "record_ids": ["T-1"]})
    assert r.status_code == 400
    assert not (run_env / "reviews.json").exists()


# ---------------------------------------------------------------------------
# 7 — nonexistent run -> 404
# ---------------------------------------------------------------------------

def test_nonexistent_run_404(client):
    r = client.post(
        f"/dashboard/{_MISSING_RUN_ID}/bulk-review",
        data={"action": "accept", "record_ids": ["T-1"]},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 8 — empty selection is a no-op redirect, writes nothing
# ---------------------------------------------------------------------------

def test_empty_selection_noop(client, run_env):
    r = client.post(_BULK_PATH, data={"action": "accept"})
    assert r.status_code == 303
    assert not (run_env / "reviews.json").exists()


# ---------------------------------------------------------------------------
# 9 — unauthenticated request is rejected (redirect to /login)
# ---------------------------------------------------------------------------

def test_unauthenticated_rejected(run_env):
    with TestClient(main.app, follow_redirects=False) as c:
        r = c.post(_BULK_PATH, data={"action": "accept", "record_ids": ["T-1"]})
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 10 — enriched_targets.json is byte-identical after the POST
# ---------------------------------------------------------------------------

def test_enriched_targets_untouched(client, run_env):
    et = run_env / "enriched_targets.json"
    before = hashlib.sha256(et.read_bytes()).hexdigest()
    r = client.post(_BULK_PATH, data={"action": "reject", "record_ids": ["T-1", "T-2", "T-3"]})
    assert r.status_code == 303
    after = hashlib.sha256(et.read_bytes()).hexdigest()
    assert before == after
