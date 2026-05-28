"""
API-layer tests for pipeline-api.
Deterministic — no network, no subprocess. Covers record_adapter, run_id
guard, review persistence/validation, filtered exports (including the
hard-exclusion bypass rule), auth, and basic route wiring.
"""

import csv
import io
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

import record_adapter  # noqa: E402
import reviews  # noqa: E402
import exports  # noqa: E402
import runs  # noqa: E402
import auth  # noqa: E402
from schema import ReviewEdit  # noqa: E402


# ---------------------------------------------------------------------------
# record_adapter
# ---------------------------------------------------------------------------

def test_get_record_id_prefers_record_id():
    assert record_adapter.get_record_id({"record_id": "R-1", "id": "X"}) == "R-1"


def test_get_record_id_falls_back_to_id():
    assert record_adapter.get_record_id({"id": "X-9"}) == "X-9"


def test_get_record_id_missing_returns_empty():
    assert record_adapter.get_record_id({"practice_name": "Acme"}) == ""


def test_normalize_payload_wrapper_dict():
    payload = {"run_id": "R", "records": [{"id": 1}, {"id": 2}]}
    assert record_adapter.normalize_records_payload(payload) == [{"id": 1}, {"id": 2}]


def test_normalize_payload_bare_list():
    assert record_adapter.normalize_records_payload([{"id": 1}]) == [{"id": 1}]


def test_normalize_payload_junk_returns_empty():
    assert record_adapter.normalize_records_payload("nonsense") == []


# ---------------------------------------------------------------------------
# run_id guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rid", [
    "RUN-20260527-143000",
    "RUN-20260527-143000-a3f9",
])
def test_valid_run_ids_accepted(rid):
    assert runs.is_valid_run_id(rid) is True


@pytest.mark.parametrize("rid", [
    "../../etc/passwd",
    "RUN-../secret",
    "RUN-2026",
    "",
    "RUN-20260527-143000-XYZZ",
])
def test_invalid_run_ids_rejected(rid):
    assert runs.is_valid_run_id(rid) is False


def test_run_dir_raises_on_traversal():
    with pytest.raises(ValueError):
        runs.run_dir("../../etc")


def test_get_run_returns_none_for_invalid_id():
    assert runs.get_run("../../etc") is None


# ---------------------------------------------------------------------------
# reviews persistence + validation
# ---------------------------------------------------------------------------

def test_override_without_reason_rejected(tmp_path):
    edit = ReviewEdit(override_tier="Bullseye", override_reason="", qc_status="approved")
    with pytest.raises(ValueError):
        reviews.save_review("RUN-20260527-143000-aaaa", "T-1", edit, "tester", tmp_path)


def test_override_with_reason_persists(tmp_path):
    edit = ReviewEdit(
        override_tier="Bullseye",
        override_reason="Website confirms target service line.",
        qc_status="approved",
    )
    saved = reviews.save_review("RUN-20260527-143000-aaaa", "T-1", edit, "tester", tmp_path)
    assert saved["override_tier"] == "Bullseye"
    assert saved["reviewed_by"] == "tester"
    assert saved["reviewed_at"]  # server-set

    # reviews.json was created and holds the entry
    stored = json.loads((tmp_path / "reviews.json").read_text())
    assert stored["T-1"]["qc_status"] == "approved"


def test_review_does_not_touch_enriched_targets(tmp_path):
    target = tmp_path / "enriched_targets.json"
    original = json.dumps({"records": [{"record_id": "T-1", "target_tier": "Warm"}]})
    target.write_text(original)

    edit = ReviewEdit(qc_status="approved")
    reviews.save_review("RUN-20260527-143000-aaaa", "T-1", edit, "tester", tmp_path)

    assert target.read_text() == original  # byte-identical


# ---------------------------------------------------------------------------
# filtered exports
# ---------------------------------------------------------------------------

def _write_run(tmp_path, records, reviews_map):
    (tmp_path / "enriched_targets.json").write_text(json.dumps({"records": records}))
    (tmp_path / "reviews.json").write_text(json.dumps(reviews_map))


def _csv_ids(buf: io.BytesIO):
    text = buf.getvalue().decode("utf-8")
    return {row["record_id"] for row in csv.DictReader(io.StringIO(text))}


def test_approved_export_includes_overridden_excluded(tmp_path):
    """EXCLUDED record with explicit analyst override_tier + approved → appears in approved CSV."""
    records = [
        {"record_id": "T-1", "target_tier": "Bullseye", "exclusion_status": "CLEAR"},
        {"record_id": "T-2", "target_tier": "Excluded", "exclusion_status": "EXCLUDED"},
        {"record_id": "T-3", "target_tier": "Watchlist", "exclusion_status": "CLEAR"},
    ]
    reviews_map = {
        "T-1": {"override_tier": None, "override_reason": None, "qc_status": "approved",
                "analyst_note": "", "reviewed_by": "t", "reviewed_at": "now"},
        "T-2": {"override_tier": "Bullseye", "override_reason": "looks good",
                "qc_status": "approved", "analyst_note": "", "reviewed_by": "t",
                "reviewed_at": "now"},
        "T-3": {"override_tier": None, "override_reason": None, "qc_status": "pending",
                "analyst_note": "", "reviewed_by": None, "reviewed_at": None},
    }
    _write_run(tmp_path, records, reviews_map)

    ids = _csv_ids(exports.build_approved_csv("RUN-20260527-143000-aaaa", tmp_path))
    assert "T-1" in ids   # CLEAR + approved → in
    assert "T-2" in ids   # EXCLUDED + analyst override Bullseye + approved → in
    assert "T-3" not in ids  # pending → out


def test_approved_export_blocks_excluded_without_override(tmp_path):
    """EXCLUDED record with no analyst override_tier stays out of approved CSV."""
    records = [
        {"record_id": "T-X", "target_tier": "Excluded", "exclusion_status": "EXCLUDED"},
    ]
    reviews_map = {
        "T-X": {"override_tier": None, "override_reason": None, "qc_status": "approved",
                "analyst_note": "", "reviewed_by": "t", "reviewed_at": "now"},
    }
    _write_run(tmp_path, records, reviews_map)

    ids = _csv_ids(exports.build_approved_csv("RUN-20260527-143000-aaaa", tmp_path))
    assert "T-X" not in ids  # no override → hard exclusion still blocks


def test_excluded_export_includes_excluded(tmp_path):
    records = [
        {"record_id": "T-1", "target_tier": "Bullseye", "exclusion_status": "CLEAR"},
        {"record_id": "T-4", "target_tier": "Excluded", "exclusion_status": "EXCLUDED"},
    ]
    _write_run(tmp_path, records, {})
    ids = _csv_ids(exports.build_excluded_csv("RUN-20260527-143000-aaaa", tmp_path))
    assert ids == {"T-4"}


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def test_validate_credentials_correct():
    assert auth.validate_credentials("tester", "secret-pw") is True


def test_validate_credentials_wrong_password():
    assert auth.validate_credentials("tester", "nope") is False


def test_validate_credentials_unknown_user():
    assert auth.validate_credentials("ghost", "secret-pw") is False


# ---------------------------------------------------------------------------
# route wiring (auth enforcement)
# ---------------------------------------------------------------------------

def test_runs_endpoint_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_RUNS_PATH", str(tmp_path))
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as client:
        unauth = client.get("/runs")
        assert unauth.status_code in (401, 403)

        ok = client.get("/runs", headers={"Authorization": "Bearer test-api-key"})
        assert ok.status_code == 200
        assert "runs" in ok.json()
