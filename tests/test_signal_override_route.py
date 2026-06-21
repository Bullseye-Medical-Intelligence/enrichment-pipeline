"""
test_signal_override_route.py

Route tests for POST /api/ui/reviews/{run_id}/{record_id}/signal-override
(Prompt 3 of the override build). Uses the FastAPI TestClient against a mock
run directory. Deterministic — no network, no subprocess, no rescore.

Guarantees under test: schema validation (422), run/record/signal existence
(404/422), server-owned override_by, idempotency, and that the route never
writes enriched_targets.json.
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
os.environ.setdefault("PIPELINE_API_KEY", "test-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import runs  # noqa: E402

_RUN_ID = "RUN-20260621-120000-aaaa"
_MISSING_RUN_ID = "RUN-20260621-130000-bbbb"
_OVERRIDE_PATH = f"/api/ui/reviews/{_RUN_ID}/T-1/signal-override"


def _record():
    """Minimal enriched record with scores, tier, and two signals."""
    return {
        "id": "T-1",
        "practice_name": "Acme Women's Health",
        "bullseye_score": 72,
        "fit_signal_score": 68,
        "confidence_score": 80,
        "target_tier": "Contender",
        "exclusion_status": "CLEAR",
        "signals": [
            {"signal_id": "S-ICP-001", "signal_label": "IUI offered",
             "signal_state": "yes", "evidence_text": "orig",
             "source_url": "https://orig.example.com", "confidence": "high"},
            {"signal_id": "S-ICP-007", "signal_label": "Cash-pay visible",
             "signal_state": "not_found", "evidence_text": "",
             "source_url": "", "confidence": "low"},
        ],
    }


def _write_run(run_directory):
    """Write a complete run dir: status.json + enriched_targets.json."""
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-06-21T12:00:00+00:00", "operator": "tester",
    }))
    (run_directory / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": [_record()]}, indent=2)
    )


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    """Point OUTPUT_RUNS_PATH at tmp_path and create a complete run."""
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path)
    run_directory = tmp_path / _RUN_ID
    _write_run(run_directory)
    return run_directory


@pytest.fixture
def client(run_env):
    """Logged-in TestClient sharing the monkeypatched run environment."""
    with TestClient(main.app) as c:
        r = c.post("/login", data={"username": "tester", "password": "secret-pw"})
        assert r.status_code in (200, 303, 302)
        yield c


# ---------------------------------------------------------------------------
# 1 — valid override succeeds and captures original_state
# ---------------------------------------------------------------------------

def test_valid_override_succeeds(client):
    r = client.post(_OVERRIDE_PATH, json={
        "signal_id": "S-ICP-007", "override_state": "yes",
        "source_url": "https://acme.example.com/financing",
        "override_note": "Self-pay pricing page",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    ov = body["signal_override"]
    assert ov["override_state"] == "yes"
    assert ov["source_url"] == "https://acme.example.com/financing"
    assert ov["original_state"] == "not_found"  # captured from the record
    assert ov["override_at"]  # server-stamped


# ---------------------------------------------------------------------------
# 2 / 3 — schema rejects bad state and empty source_url
# ---------------------------------------------------------------------------

def test_invalid_override_state_422(client):
    r = client.post(_OVERRIDE_PATH, json={
        "signal_id": "S-ICP-007", "override_state": "maybe",
        "source_url": "https://x.example.com",
    })
    assert r.status_code == 422


def test_empty_source_url_422(client):
    r = client.post(_OVERRIDE_PATH, json={
        "signal_id": "S-ICP-007", "override_state": "yes", "source_url": "",
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 4 — nonexistent run -> 404
# ---------------------------------------------------------------------------

def test_nonexistent_run_404(client):
    r = client.post(
        f"/api/ui/reviews/{_MISSING_RUN_ID}/T-1/signal-override",
        json={"signal_id": "S-ICP-007", "override_state": "yes",
              "source_url": "https://x.example.com"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 5 — nonexistent record -> 404
# ---------------------------------------------------------------------------

def test_nonexistent_record_404(client):
    r = client.post(
        f"/api/ui/reviews/{_RUN_ID}/T-NOPE/signal-override",
        json={"signal_id": "S-ICP-007", "override_state": "yes",
              "source_url": "https://x.example.com"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 6 — signal not on the record -> 422 with a clear message
# ---------------------------------------------------------------------------

def test_signal_not_on_record_422(client):
    r = client.post(_OVERRIDE_PATH, json={
        "signal_id": "S-DOES-NOT-EXIST", "override_state": "yes",
        "source_url": "https://x.example.com",
    })
    assert r.status_code == 422
    assert "S-DOES-NOT-EXIST" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 7 — override_by comes from the session, never the request body
# ---------------------------------------------------------------------------

def test_override_by_is_session_user(client, run_env):
    r = client.post(_OVERRIDE_PATH, json={
        "signal_id": "S-ICP-007", "override_state": "yes",
        "source_url": "https://x.example.com",
        "override_by": "attacker-supplied",  # must be ignored
    })
    assert r.status_code == 200
    assert r.json()["signal_override"]["override_by"] == "tester"
    # And it is what was persisted, not just echoed.
    stored = json.loads((run_env / "reviews.json").read_text())
    assert stored["T-1"]["signal_overrides"]["S-ICP-007"]["override_by"] == "tester"


# ---------------------------------------------------------------------------
# 8 — idempotency: same payload twice, original_state unchanged
# ---------------------------------------------------------------------------

def test_idempotent_double_post(client, run_env):
    payload = {"signal_id": "S-ICP-007", "override_state": "yes",
               "source_url": "https://x.example.com", "override_note": "n"}
    r1 = client.post(_OVERRIDE_PATH, json=payload)
    r2 = client.post(_OVERRIDE_PATH, json=payload)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["signal_override"]["original_state"] == "not_found"

    # Exactly one override entry persisted for the signal.
    stored = json.loads((run_env / "reviews.json").read_text())
    overrides = stored["T-1"]["signal_overrides"]
    assert list(overrides.keys()) == ["S-ICP-007"]
    assert overrides["S-ICP-007"]["original_state"] == "not_found"


# ---------------------------------------------------------------------------
# 9 — unauthenticated request is rejected (redirect to /login)
# ---------------------------------------------------------------------------

def test_unauthenticated_rejected(run_env):
    with TestClient(main.app, follow_redirects=False) as c:
        r = c.post(_OVERRIDE_PATH, json={
            "signal_id": "S-ICP-007", "override_state": "yes",
            "source_url": "https://x.example.com",
        })
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 10 — enriched_targets.json is byte-identical after the POST
# ---------------------------------------------------------------------------

def test_enriched_targets_untouched(client, run_env):
    et = run_env / "enriched_targets.json"
    before = hashlib.sha256(et.read_bytes()).hexdigest()
    r = client.post(_OVERRIDE_PATH, json={
        "signal_id": "S-ICP-007", "override_state": "yes",
        "source_url": "https://x.example.com",
    })
    assert r.status_code == 200
    after = hashlib.sha256(et.read_bytes()).hexdigest()
    assert before == after
