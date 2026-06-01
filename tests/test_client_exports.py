"""
Tests for the client deliverable package (client_exports.py) and its route.

Deterministic — builds a run folder on disk, then asserts the ZIP contents,
the executive summary context, and the hard-exclusion rule in the approved CSV.
"""

import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("PIPELINE_API_KEY", "test-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import client_exports  # noqa: E402
import config  # noqa: E402
from schema import RunStatus  # noqa: E402

_EXPECTED_FILES = {
    "Executive_Target_Report.html",
    "Sales_Handoff.html",
    "bullseye_accounts.csv",
    "contender_accounts.csv",
    "excluded_targets.csv",
}


def _build_run(tmp_path):
    """Write a completed run folder with records, reviews, and snapshots."""
    records = [
        {"record_id": "T-1", "practice_name": "Alpha Womens Health",
         "website_url": "https://alpha.example", "address_city": "Austin",
         "address_state": "TX", "target_tier": "Bullseye", "bullseye_score": 88,
         "confidence_score": 80, "exclusion_status": "CLEAR",
         "sales_angle": ["Strong fit on service lines."],
         "signals": [{"signal_label": "IUD", "evidence_text": "IUD listed.",
                      "source_url": "https://alpha.example/services"}]},
        {"record_id": "T-2", "practice_name": "Beta Hospital OBGYN",
         "target_tier": "Excluded", "bullseye_score": 20,
         "exclusion_status": "EXCLUDED"},
        {"record_id": "T-3", "practice_name": "Gamma Clinic",
         "target_tier": "Contender", "bullseye_score": 60,
         "exclusion_status": "CLEAR"},
    ]
    reviews_map = {
        "T-1": {"override_tier": None, "override_reason": None, "qc_status": "approved",
                "analyst_note": "Confirmed independent.", "reviewed_by": "t", "reviewed_at": "now"},
        # Hard-excluded, analyst tried to override to Bullseye + approve.
        "T-2": {"override_tier": "Bullseye", "override_reason": "looks good",
                "qc_status": "approved", "analyst_note": "", "reviewed_by": "t", "reviewed_at": "now"},
        # Pending review.
        "T-3": {"override_tier": None, "override_reason": None, "qc_status": "pending",
                "analyst_note": "", "reviewed_by": None, "reviewed_at": None},
    }
    (tmp_path / "enriched_targets.json").write_text(json.dumps({"records": records}))
    (tmp_path / "reviews.json").write_text(json.dumps(reviews_map))
    (tmp_path / "project_config_snapshot.json").write_text(json.dumps({
        "project_id": "femasys-socal-obgyn", "client_name": "Femasys",
        "product_name": "FemaSeed", "target_specialty": "OBGYN",
        "target_geography": ["CA"], "icp_profile_id": "femaseed-obgyn",
    }))
    (tmp_path / "icp_snapshot.json").write_text(json.dumps({
        "icp_id": "femaseed-obgyn", "name": "FemaSeed OBGYN ICP", "version": "1.0",
        "signals": [{"signal_id": "S-1", "signal_label": "x",
                     "prompt_instruction": "y", "positive_weight": 10}],
    }))


def _status():
    return RunStatus(
        run_id="RUN-20260527-143000-aaaa", project_id="femasys-socal-obgyn",
        source_type="outscraper", input_filename="leads.csv", status="complete",
        created_at="2026-05-27T14:30:00Z", operator="tester", records_input=3,
        records_output=3, client_name="Femasys", product_name="FemaSeed",
        target_specialty="OBGYN", target_geography=["CA"],
        icp_profile_id="femaseed-obgyn", icp_profile_name="FemaSeed OBGYN ICP",
        icp_profile_version="1.0",
    )


def test_package_contains_required_files(tmp_path):
    _build_run(tmp_path)
    buf = client_exports.build_client_package("RUN-20260527-143000-aaaa", tmp_path, _status())
    with zipfile.ZipFile(buf) as zf:
        names = set(zf.namelist())
    assert names == _EXPECTED_FILES


def test_package_excludes_internal_artifacts(tmp_path):
    _build_run(tmp_path)
    buf = client_exports.build_client_package("RUN-20260527-143000-aaaa", tmp_path, _status())
    with zipfile.ZipFile(buf) as zf:
        names = set(zf.namelist())
    assert "reviews.json" not in names
    assert "run_log.json" not in names
    assert "enriched_targets.json" not in names
    assert "executive_summary.md" not in names
    assert "top_target_briefs.md" not in names
    assert "methodology.md" not in names
    assert "approved_targets.csv" not in names


def test_bullseye_csv_includes_overridden_excluded(tmp_path):
    """Hard-excluded record with explicit analyst override_tier=Bullseye + approved → in bullseye CSV."""
    _build_run(tmp_path)
    buf = client_exports.build_client_package("RUN-20260527-143000-aaaa", tmp_path, _status())
    with zipfile.ZipFile(buf) as zf:
        bullseye = zf.read("bullseye_accounts.csv").decode("utf-8")
    assert "T-1" in bullseye         # CLEAR + Bullseye tier + approved → included
    assert "T-2" in bullseye         # EXCLUDED + analyst override Bullseye + approved → included
    assert "T-3" not in bullseye     # pending review → not included


def test_approved_csv_blocks_excluded_without_override(tmp_path):
    """Hard-excluded record with no override_tier stays out of approved CSV."""
    records = [
        {"record_id": "T-X", "practice_name": "Big Hospital OBGYN",
         "target_tier": "Excluded", "bullseye_score": 25,
         "exclusion_status": "EXCLUDED"},
    ]
    reviews_map = {
        "T-X": {"override_tier": None, "override_reason": None,
                "qc_status": "approved", "analyst_note": "",
                "reviewed_by": "t", "reviewed_at": "now"},
    }
    (tmp_path / "enriched_targets.json").write_text(json.dumps({"records": records}))
    (tmp_path / "reviews.json").write_text(json.dumps(reviews_map))

    import exports
    buf = exports.build_approved_csv("RUN-test", tmp_path)
    content = buf.getvalue().decode("utf-8")
    assert "T-X" not in content  # approved but no override → hard exclusion still blocks


def test_run_metadata_has_context(tmp_path):
    _build_run(tmp_path)
    manifest = client_exports.build_run_manifest("RUN-20260527-143000-aaaa", tmp_path, _status())
    meta = json.loads(manifest.decode("utf-8"))
    assert meta["client_name"] == "Femasys"
    assert meta["project_id"] == "femasys-socal-obgyn"
    assert meta["product_name"] == "FemaSeed"
    assert meta["target_specialty"] == "OBGYN"
    assert meta["icp_profile_name"] == "FemaSeed OBGYN ICP"
    assert meta["records_approved"] == 2  # T-1 (CLEAR) + T-2 (EXCLUDED+override)


def test_run_manifest_is_internal_only(tmp_path):
    """The run manifest is built on demand and must NOT be in the client package."""
    _build_run(tmp_path)
    buf = client_exports.build_client_package("RUN-20260527-143000-aaaa", tmp_path, _status())
    with zipfile.ZipFile(buf) as zf:
        assert "run_metadata.json" not in zf.namelist()


def test_manifest_methodology_excludes_phi_language(tmp_path):
    _build_run(tmp_path)
    manifest = client_exports.build_run_manifest("RUN-20260527-143000-aaaa", tmp_path, _status())
    meta = json.loads(manifest.decode("utf-8"))
    assert "does not use PHI" in meta["methodology"]


def test_executive_report_present_and_not_empty(tmp_path):
    _build_run(tmp_path)
    buf = client_exports.build_client_package("RUN-20260527-143000-aaaa", tmp_path, _status())
    with zipfile.ZipFile(buf) as zf:
        report = zf.read("Executive_Target_Report.html").decode("utf-8")
    assert len(report) > 0
    # Self-contained HTML report (not a WeasyPrint PDF, not an error page).
    assert "<html" in report.lower()
    assert "generation failed" not in report.lower()


def test_contender_csv_empty_when_no_contender_records(tmp_path):
    """Fixture has only Bullseye-tier approved records — contender CSV should contain none."""
    _build_run(tmp_path)
    buf = client_exports.build_client_package("RUN-20260527-143000-aaaa", tmp_path, _status())
    with zipfile.ZipFile(buf) as zf:
        contender = zf.read("contender_accounts.csv").decode("utf-8")
    # T-1 is Bullseye, T-2 is EXCLUDED+override Bullseye → no contender records
    assert "T-1" not in contender
    assert "T-2" not in contender


def test_handoff_account_mapping_for_rep_fields():
    """Why It Matters carries the sales angle (no internal score); opener, verify,
    and landmine map to opener / not_found signals / objection respectively."""
    import sales_export
    rec = {
        "practice_name": "Austin Womens Health",
        "website_url": "https://austinwh.example",
        "confidence_band": "High", "bullseye_score": 91, "target_tier": "Bullseye",
        "sales_angle": ["Offers in-office IUD placement.", "Independent ownership."],
        "signals": [
            {"signal_label": "IUD", "signal_state": "yes", "positive_weight": 20},
            {"signal_label": "Referral Reach", "signal_state": "not_found", "positive_weight": 10},
        ],
        "call_brief": {
            "why_contact": "OBGYN practice: IUD + Cash Pay (fit 84).",
            "opening_line": "Saw you offer in-office IUD placement.",
            "likely_objection": "We already have a device vendor.",
            "discovery_question": "How do you source IUD inventory?",
            "missing_to_verify": [],
            "disqualifier_risk": [],
        },
    }
    acct = sales_export._record_to_account(rec, "Bullseye")
    # Why It Matters = sales angle, and the internal fit score never leaks.
    assert acct.why_it_matters == "Offers in-office IUD placement. Independent ownership."
    assert "84" not in (acct.why_it_matters or "")
    assert "fit 84" not in (acct.why_it_matters or "")
    # Example opener carries the LLM opener, not the verify list.
    assert acct.wedge == "Saw you offer in-office IUD placement."
    # Verify lists the not_found desirable signal to uncover, not the scripted question.
    assert acct.verify == ["Referral Reach"]
    assert "How do you source" not in acct.verify
    # Landmine surfaces the likely objection (and never crashes on a list input).
    assert acct.landmine and "We already have a device vendor" in acct.landmine


# ---------------------------------------------------------------------------
# Route: requires a complete run
# ---------------------------------------------------------------------------

def test_rejected_bullseye_absent_from_handoff(tmp_path):
    """A Bullseye record explicitly rejected by the analyst must not appear in the handoff."""
    records = [
        {"record_id": "T-OK", "practice_name": "Alpha Clinic",
         "target_tier": "Bullseye", "bullseye_score": 88,
         "exclusion_status": "CLEAR"},
        {"record_id": "T-REJ", "practice_name": "Beta Clinic",
         "target_tier": "Bullseye", "bullseye_score": 82,
         "exclusion_status": "CLEAR"},
    ]
    reviews_map = {
        "T-OK":  {"override_tier": None, "override_reason": None, "qc_status": "approved",
                  "analyst_note": "", "reviewed_by": "t", "reviewed_at": "now"},
        "T-REJ": {"override_tier": None, "override_reason": None, "qc_status": "rejected",
                  "analyst_note": "Not a fit.", "reviewed_by": "t", "reviewed_at": "now"},
    }
    (tmp_path / "enriched_targets.json").write_text(json.dumps({"records": records}))
    (tmp_path / "reviews.json").write_text(json.dumps(reviews_map))
    (tmp_path / "project_config_snapshot.json").write_text(json.dumps({"project_id": "p"}))
    (tmp_path / "icp_snapshot.json").write_text(json.dumps({"icp_id": "i", "name": "N", "version": "1", "signals": []}))

    import sales_export
    # Client-facing handoff must filter rejected records; internal handoff shows all.
    html = sales_export._build_client_handoff_html("RUN-test", tmp_path, _status()).decode("utf-8")
    assert "Alpha Clinic" in html       # approved Bullseye → present
    assert "Beta Clinic" not in html    # rejected Bullseye → absent from client handoff


# ---------------------------------------------------------------------------
# Route: requires a complete run
# ---------------------------------------------------------------------------

def test_client_package_route_requires_auth():
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as client:
        # Unauthenticated → redirected to login, never a ZIP.
        r = client.get(
            "/runs/RUN-20260527-143000-zzzz/client-package",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def test_client_package_route_404_for_missing_run():
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as client:
        client.post("/login", data={"username": "tester", "password": "secret-pw"})
        # Authenticated, but the run does not exist → 404, not a 200 ZIP.
        r = client.get("/runs/RUN-20260527-143000-zzzz/client-package")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Roster delete route
# ---------------------------------------------------------------------------

def _write_ingested_run(tmp_path, monkeypatch):
    """Write a minimal ingested run (pre-enrichment roster) and point the API at it."""
    import runs
    import record_adapter
    import reviews

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_dir)

    run_id = "RUN-20260601-120000"
    run_d = runs_dir / run_id
    run_d.mkdir()

    records = [
        {"record_id": "R-1", "practice_name": "Miami OB", "address_city": "Miami",
         "address_state": "FL", "target_tier": "", "enrichment_status": "not_enriched",
         "exclusion_status": "CLEAR"},
        {"record_id": "R-2", "practice_name": "Tampa OB", "address_city": "Tampa",
         "address_state": "FL", "target_tier": "", "enrichment_status": "not_enriched",
         "exclusion_status": "CLEAR"},
        {"record_id": "R-3", "practice_name": "Orlando OB", "address_city": "Orlando",
         "address_state": "FL", "target_tier": "", "enrichment_status": "not_enriched",
         "exclusion_status": "CLEAR"},
    ]
    payload = {"run_id": run_id, "generated_at": "2026-06-01T12:00:00Z",
               "record_count": len(records), "records": records}
    reviews._atomic_write(run_d / "enriched_targets.json", payload)

    status = RunStatus(
        run_id=run_id, project_id="p-001", source_type="outscraper",
        input_filename="test.csv", status="ingested", created_at="2026-06-01T12:00:00Z",
        operator="tester", records_input=len(records),
    )
    reviews._atomic_write(run_d / "status.json", status.model_dump())
    return run_id, run_d


def test_roster_delete_removes_records(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main

    run_id, run_d = _write_ingested_run(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        client.post("/login", data={"username": "tester", "password": "secret-pw"})
        r = client.post(
            f"/runs/{run_id}/roster/delete",
            json={"record_ids": ["R-1", "R-3"]},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["removed"] == 2
    assert body["remaining"] == 1

    import record_adapter, json
    with open(run_d / "enriched_targets.json") as f:
        remaining = record_adapter.normalize_records_payload(json.load(f))
    assert len(remaining) == 1
    assert remaining[0]["record_id"] == "R-2"


def test_roster_delete_rejects_non_ingested_run(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    import runs, reviews

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_dir)

    run_id = "RUN-20260601-130000"
    run_d = runs_dir / run_id
    run_d.mkdir()
    status = RunStatus(
        run_id=run_id, project_id="p-001", source_type="outscraper",
        input_filename="test.csv", status="complete", created_at="2026-06-01T13:00:00Z",
        operator="tester", records_input=3,
    )
    reviews._atomic_write(run_d / "status.json", status.model_dump())

    with TestClient(main.app) as client:
        client.post("/login", data={"username": "tester", "password": "secret-pw"})
        r = client.post(
            f"/runs/{run_id}/roster/delete",
            json={"record_ids": ["R-1"]},
        )

    assert r.status_code == 409


def test_roster_delete_rejects_empty_ids(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main

    run_id, _ = _write_ingested_run(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        client.post("/login", data={"username": "tester", "password": "secret-pw"})
        r = client.post(f"/runs/{run_id}/roster/delete", json={"record_ids": []})

    assert r.status_code == 422
