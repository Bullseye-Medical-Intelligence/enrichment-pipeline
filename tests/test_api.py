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
    original = json.dumps({"records": [{"record_id": "T-1", "target_tier": "Contender"}]})
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
        {"record_id": "T-3", "target_tier": "Contender", "exclusion_status": "CLEAR"},
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


import sys as _sys  # noqa: E402 (re-import to avoid name collision)
from ui import _friendly_error, _compute_readiness, _pending_review_count, _parse_signals_from_form  # noqa: E402


# ---------------------------------------------------------------------------
# UX helpers: _friendly_error
# ---------------------------------------------------------------------------

def test_friendly_error_none_for_empty():
    assert _friendly_error(None) is None
    assert _friendly_error("") is None


def test_friendly_error_known_patterns():
    cases = [
        ("enriched_targets.json was not written",
         "Run ended before results were written. Try re-running."),
        ("malformed json in output",
         "Pipeline output file was corrupted. Try re-running."),
        ("UnicodeEncodeError in encoder",
         "Character encoding error — check that the input CSV has no unusual characters."),
        ("No module named anthropic",
         "Pipeline environment error: a required package is missing. Contact support."),
        ("SyntaxError on line 42",
         "Pipeline code error. Contact support."),
        ("Interrupted by server restart at step 3",
         "The server was restarted while this run was in progress."),
    ]
    for raw, expected in cases:
        assert _friendly_error(raw) == expected, f"Pattern mismatch for: {raw!r}"


def test_friendly_error_pass_through_patterns():
    assert _friendly_error("Missing required columns: specialty") == "Missing required columns: specialty"
    assert _friendly_error("Too many runs in progress") == "Too many runs in progress"


def test_friendly_error_fallback_truncates():
    long_raw = "X" * 400
    result = _friendly_error(long_raw)
    assert result == "X" * 300


# ---------------------------------------------------------------------------
# UX helpers: _compute_readiness
# ---------------------------------------------------------------------------

def test_compute_readiness_needs_review():
    """Pending Bullseye blocks readiness; Contender-approved does not count toward Bullseye total."""
    records = [
        {"review": {"qc_status": "pending"}, "displayed_tier": "Bullseye"},
        {"review": {"qc_status": "approved"}, "displayed_tier": "Contender"},
    ]
    r = _compute_readiness(records)
    assert r["state"] == "needs_review"
    assert r["pending_count"] == 1
    assert r["approved_count"] == 0  # only Bullseye-approved counts


def test_compute_readiness_ready():
    """Ready when all Bullseye are approved; Contender does not affect state."""
    records = [
        {"review": {"qc_status": "approved"}, "displayed_tier": "Bullseye"},
        {"review": {"qc_status": "approved"}, "displayed_tier": "Contender"},
    ]
    r = _compute_readiness(records)
    assert r["state"] == "ready"
    assert r["approved_count"] == 1  # only Bullseye-approved counts


def test_compute_readiness_no_approved():
    records = [
        {"review": {"qc_status": "rejected"}, "displayed_tier": "Bullseye"},
    ]
    r = _compute_readiness(records)
    assert r["state"] == "no_approved"


def test_compute_readiness_ignores_pending_non_call_tiers():
    """Only Bullseye + Contender require QC; pending NV / Manual Review do not block."""
    records = [
        {"review": {"qc_status": "approved"}, "displayed_tier": "Bullseye"},
        {"review": {"qc_status": "pending"}, "displayed_tier": "Needs Verification"},
        {"review": {"qc_status": "pending"}, "displayed_tier": "Manual Review"},
        {"review": {"qc_status": "pending"}, "displayed_tier": "Excluded"},
    ]
    r = _compute_readiness(records)
    assert r["state"] == "ready"
    assert r["approved_count"] == 1
    assert r["pending_count"] == 0


def test_compute_readiness_excluded_not_counted():
    """Zero Bullseye records means nothing to gate — run is ready even if only Excluded exist."""
    records = [
        {"review": {"qc_status": "approved"}, "displayed_tier": "Excluded"},
    ]
    r = _compute_readiness(records)
    assert r["state"] == "ready"
    assert r["approved_count"] == 0


def test_compute_readiness_no_approved_requires_existing_bullseye():
    """no_approved only fires when there ARE Bullseye records but none have been approved."""
    records = [
        {"review": {"qc_status": "rejected"}, "displayed_tier": "Bullseye"},
        {"review": {"qc_status": "approved"}, "displayed_tier": "Excluded"},
    ]
    r = _compute_readiness(records)
    assert r["state"] == "no_approved"


def test_pending_review_count_only_counts_bullseye(tmp_path):
    """The client-package gate counts only pending Bullseye; Contender/NV/MR/Excluded are exempt."""
    records = [
        {"record_id": "T-1", "target_tier": "Bullseye", "exclusion_status": "CLEAR"},
        {"record_id": "T-2", "target_tier": "Contender", "exclusion_status": "CLEAR"},
        {"record_id": "T-3", "target_tier": "Manual Review", "exclusion_status": "CLEAR"},
        {"record_id": "T-4", "target_tier": "Needs Verification", "exclusion_status": "CLEAR"},
        {"record_id": "T-5", "target_tier": "Excluded", "exclusion_status": "EXCLUDED"},
    ]
    _write_run(tmp_path, records, {})  # all pending by default
    # Only T-1 (Bullseye) counts toward the gate.
    assert _pending_review_count("RUN-20260527-143000-aaaa", tmp_path) == 1


def test_parse_signals_skips_blank_and_preserves_hidden_fields():
    """A removed (blank-id) row is dropped; cap_tier / exclude_if_yes survive an edit."""
    form = {
        "signal_id_0": "S-1", "signal_label_0": "Cash pay", "prompt_instruction_0": "?",
        "positive_weight_0": "25", "cap_tier_0": "", "exclude_if_yes_0": "",
        # row 1 removed by the UI: disabled inputs -> absent from form data
        "signal_id_2": "S-3", "signal_label_2": "Hospital owned", "prompt_instruction_2": "?",
        "positive_weight_2": "0", "cap_tier_2": "Contender", "exclude_if_yes_2": "1",
        "reinforces_2": "S-1", "verification_required_2": "1",
    }
    out = _parse_signals_from_form(form, signal_count=3)
    ids = [s["signal_id"] for s in out]
    assert ids == ["S-1", "S-3"]              # blank/removed row 1 dropped
    s3 = next(s for s in out if s["signal_id"] == "S-3")
    assert s3["cap_tier"] == "Contender"      # preserved
    assert s3["exclude_if_yes"] is True
    assert s3["reinforces"] == "S-1"
    assert s3["verification_required"] is True


# ---------------------------------------------------------------------------
# Project edit route
# ---------------------------------------------------------------------------

def test_project_edit_route_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_RUNS_PATH", str(tmp_path))
    monkeypatch.setenv("PROJECTS_PATH", str(tmp_path))
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as client:
        client.post("/login", data={"username": "tester", "password": "secret-pw"})
        r = client.get("/projects/nonexistent-project/edit")
        assert r.status_code != 405
