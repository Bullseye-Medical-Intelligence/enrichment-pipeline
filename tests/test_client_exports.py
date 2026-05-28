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
    "executive_summary.md",
    "approved_targets.csv",
    "excluded_targets.csv",
    "top_target_briefs.md",
    "methodology.md",
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
         "target_tier": "Watchlist", "bullseye_score": 60,
         "exclusion_status": "CLEAR"},
    ]
    reviews_map = {
        "T-1": {"override_tier": None, "override_reason": None, "qc_status": "approved",
                "analyst_note": "Confirmed independent.", "reviewed_by": "t", "reviewed_at": "now"},
        # Hard-excluded, analyst tried to override to Bullseye + approve.
        "T-2": {"override_tier": "Bullseye", "override_reason": "looks good",
                "qc_status": "approved", "analyst_note": "", "reviewed_by": "t", "reviewed_at": "now"},
        # Approved but only Watchlist tier (still approved → included).
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


def test_approved_csv_excludes_hard_excluded(tmp_path):
    _build_run(tmp_path)
    buf = client_exports.build_client_package("RUN-20260527-143000-aaaa", tmp_path, _status())
    with zipfile.ZipFile(buf) as zf:
        approved = zf.read("approved_targets.csv").decode("utf-8")
    assert "T-1" in approved
    assert "T-2" not in approved  # hard exclusion cannot be bypassed by override
    assert "T-3" not in approved  # not approved (pending)


def test_executive_summary_has_context(tmp_path):
    _build_run(tmp_path)
    buf = client_exports.build_client_package("RUN-20260527-143000-aaaa", tmp_path, _status())
    with zipfile.ZipFile(buf) as zf:
        summary = zf.read("executive_summary.md").decode("utf-8")
    assert "Femasys" in summary
    assert "femasys-socal-obgyn" in summary
    assert "FemaSeed" in summary
    assert "OBGYN" in summary
    assert "FemaSeed OBGYN ICP" in summary
    assert "Approved targets:** 1" in summary


def test_methodology_excludes_phi_language(tmp_path):
    _build_run(tmp_path)
    buf = client_exports.build_client_package("RUN-20260527-143000-aaaa", tmp_path, _status())
    with zipfile.ZipFile(buf) as zf:
        methodology = zf.read("methodology.md").decode("utf-8")
    assert "does not use PHI" in methodology


def test_top_briefs_lists_approved(tmp_path):
    _build_run(tmp_path)
    buf = client_exports.build_client_package("RUN-20260527-143000-aaaa", tmp_path, _status())
    with zipfile.ZipFile(buf) as zf:
        briefs = zf.read("top_target_briefs.md").decode("utf-8")
    assert "Alpha Womens Health" in briefs
    assert "Beta Hospital OBGYN" not in briefs  # excluded, not in client briefs


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
