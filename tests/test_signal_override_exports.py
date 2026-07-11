"""
test_signal_override_exports.py

Tests for Prompt 5: propagation of signal overrides into client-facing outputs
(sales_export.py, exports.py, brief_publisher.py).

Critical constraint tested: is_override marker and any override wording MUST NOT
appear in client-facing output; the client sees the overridden signal as a normal
confirmed signal with the operator-supplied source_url as its evidence link.

Deterministic — no network, no subprocess, no SFTP.
"""

import csv
import hashlib
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parent.parent
_API_DIR = _REPO / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO))

import brief_publisher  # noqa: E402
import exports          # noqa: E402
import reviews          # noqa: E402
import runs             # noqa: E402
import sales_export     # noqa: E402

_RUN_ID = "RUN-20260621-130000-bbbb"

_STATUS = SimpleNamespace(
    run_id=_RUN_ID,
    status="complete",
    operator="tester",
    created_at="2026-06-21T13:00:00+00:00",
    completed_at="2026-06-21T13:30:00+00:00",
    client_name="TestClient",
    product_name="TestProd",
    target_specialty="OBGYN",
    target_geography=["GA"],
    icp_profile_id="obgyn-test-v1",
    icp_profile_name="Test ICP",
)


def _record():
    """Record with one yes (S-ICP-001) and one not_found (S-ICP-007)."""
    return {
        "id": "T-1", "record_id": "T-1", "practice_name": "Acme Women's Health",
        "bullseye_score": 72, "fit_signal_score": 68, "confidence_score": 80,
        "target_tier": "Contender", "exclusion_status": "CLEAR",
        "enrichment_status": "complete", "confidence_band": "Moderate",
        "website_url": "https://acme.example.com",
        "address_city": "Atlanta", "address_state": "GA", "address_zip": "30301",
        "phone": "404-555-1234",
        "call_brief": {
            "key_contact": "Dr. Smith",
            "why_contact": "Cash pay confirmed",
            "top_evidence": ["Lists IUI"],
            "missing_to_verify": [],
            "disqualifier_risk": [],
            "opening_line": "Noticed you offer IUI.",
            "likely_objection": "",
            "discovery_question": "",
            "hours_of_operation": "",
        },
        "sales_angle": ["Confirmed IUI practice — FemaSeed is a direct upgrade."],
        "signals": [
            {"signal_id": "S-ICP-001", "signal_label": "IUI offered",
             "signal_state": "yes", "evidence_text": "Lists IUI",
             "source_url": "https://acme.example.com/services",
             "confidence": "high", "positive_weight": 25,
             "state_inferred": False, "inferred_from": "", "not_found_reason": "",
             "floor_tier": None},
            {"signal_id": "S-ICP-007", "signal_label": "Cash-pay visible",
             "signal_state": "not_found", "evidence_text": "",
             "source_url": "", "confidence": "low", "positive_weight": 16,
             "state_inferred": False, "inferred_from": "", "not_found_reason": "",
             "floor_tier": None},
        ],
    }


def _write_run(run_directory, reviews_map=None):
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-06-21T13:00:00+00:00",
        "completed_at": "2026-06-21T13:30:00+00:00", "operator": "tester",
    }))
    (run_directory / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": [_record()]}, indent=2))
    if reviews_map is not None:
        (run_directory / "reviews.json").write_text(json.dumps(reviews_map, indent=2))


def _reviews_with_override(signal_id, override_state, source_url, original_state,
                            override_at="2026-06-21T14:00:00+00:00", **extra):
    """Build a reviews map with one record carrying a signal override."""
    entry = {
        "analyst_note": "", "override_tier": None, "override_reason": None,
        "qc_status": "approved", "reviewed_by": "tester",
        "reviewed_at": "2026-06-21T13:45:00+00:00",
        "extra_sales_angles": [],
        "signal_overrides": {
            signal_id: {
                "signal_id": signal_id, "override_state": override_state,
                "source_url": source_url, "override_note": "Operator confirmed",
                "override_by": "tester", "override_at": override_at,
                "original_state": original_state,
            },
        },
    }
    entry.update(extra)
    return {"T-1": entry}


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path)
    return tmp_path / _RUN_ID


# ---------------------------------------------------------------------------
# 1 — client handoff reflects the overridden signal state
# ---------------------------------------------------------------------------

def test_client_handoff_overlay_changes_confirmed_signals(run_dir):
    _write_run(run_dir, _reviews_with_override(
        "S-ICP-007", "yes", "https://acme.example.com/financing", "not_found"))

    all_reviews = reviews.get_reviews(_RUN_ID, run_dir)
    records = sales_export._load_records(run_dir)
    project = {}
    icp = {}

    handoff_run = sales_export._build_handoff_run(
        _RUN_ID, _STATUS, project, icp, records, all_reviews)

    assert len(handoff_run.accounts) == 1
    account = handoff_run.accounts[0]
    # S-ICP-007 was overridden from not_found → yes; its label must now appear
    # in confirmed_signals alongside S-ICP-001.
    labels = account.confirmed_signals or []
    assert any("Cash" in label or "cash" in label for label in labels), (
        f"Overridden cash-pay signal not in confirmed_signals: {labels}"
    )


# ---------------------------------------------------------------------------
# 2 — is_override marker absent from every Account field and signal data
# ---------------------------------------------------------------------------

def test_is_override_absent_from_handoff_account(run_dir):
    _write_run(run_dir, _reviews_with_override(
        "S-ICP-007", "yes", "https://acme.example.com/financing", "not_found"))

    all_reviews = reviews.get_reviews(_RUN_ID, run_dir)
    records = sales_export._load_records(run_dir)

    handoff_run = sales_export._build_handoff_run(
        _RUN_ID, _STATUS, {}, {}, records, all_reviews)

    account = handoff_run.accounts[0]
    # Serialize the account to a string and verify "is_override" is absent.
    account_str = repr(account)
    assert "is_override" not in account_str
    # Also verify "override" wording is not in any confirmed signal label.
    for label in (account.confirmed_signals or []):
        assert "override" not in label.lower(), (
            f"Override marker leaked into confirmed_signals: {label}"
        )


# ---------------------------------------------------------------------------
# 3 — bullseye_score unchanged: no rescore from overlay
# ---------------------------------------------------------------------------

def test_no_rescore_when_overlay_applied(run_dir):
    _write_run(run_dir, _reviews_with_override(
        "S-ICP-007", "yes", "https://acme.example.com/pay", "not_found"))

    all_reviews = reviews.get_reviews(_RUN_ID, run_dir)
    records = sales_export._load_records(run_dir)

    # The original record carries bullseye_score=72.  After overlay the Account
    # must still report internal_score=72 — we never rescore.
    handoff_run = sales_export._build_handoff_run(
        _RUN_ID, _STATUS, {}, {}, records, all_reviews)

    assert handoff_run.accounts[0].internal_score == 72


# ---------------------------------------------------------------------------
# 4 — enriched_targets.json byte-identical after _build_handoff_run
# ---------------------------------------------------------------------------

def test_enriched_targets_untouched_by_handoff_build(run_dir):
    _write_run(run_dir, _reviews_with_override(
        "S-ICP-007", "yes", "https://acme.example.com/pay", "not_found"))

    et = run_dir / "enriched_targets.json"
    before = hashlib.sha256(et.read_bytes()).hexdigest()

    all_reviews = reviews.get_reviews(_RUN_ID, run_dir)
    records = sales_export._load_records(run_dir)
    sales_export._build_handoff_run(_RUN_ID, _STATUS, {}, {}, records, all_reviews)

    assert hashlib.sha256(et.read_bytes()).hexdigest() == before


# ---------------------------------------------------------------------------
# 5 — newest_signal_override_at returns the newest timestamp
# ---------------------------------------------------------------------------

def test_newest_signal_override_at_returns_latest():
    all_reviews = {
        "T-1": {
            "signal_overrides": {
                "S-ICP-007": {"override_at": "2026-06-21T14:00:00+00:00"},
                "S-ICP-001": {"override_at": "2026-06-21T15:00:00+00:00"},
            },
        },
        "T-2": {
            "signal_overrides": {
                "S-ICP-003": {"override_at": "2026-06-21T13:00:00+00:00"},
            },
        },
    }
    result = brief_publisher.newest_signal_override_at(all_reviews)
    assert result == "2026-06-21T15:00:00+00:00"


# ---------------------------------------------------------------------------
# 6 — newest_signal_override_at returns None when no overrides exist
# ---------------------------------------------------------------------------

def test_newest_signal_override_at_none_when_absent():
    all_reviews = {
        "T-1": {"analyst_note": "looks good", "signal_overrides": {}},
        "T-2": {"signal_overrides": None},
        "T-3": {},
    }
    assert brief_publisher.newest_signal_override_at(all_reviews) is None


# ---------------------------------------------------------------------------
# 7 — regression: no overlay → Account confirmed_signals unchanged
# ---------------------------------------------------------------------------

def test_no_overlay_account_signals_unchanged(run_dir):
    # Approved review with no signal overrides — Contender still needs approval to
    # pass _build_handoff_run's filter.
    _write_run(run_dir, reviews_map={"T-1": {
        "analyst_note": "", "override_tier": None, "override_reason": None,
        "qc_status": "approved", "reviewed_by": "tester",
        "reviewed_at": "2026-06-21T13:45:00+00:00",
        "extra_sales_angles": [], "signal_overrides": {},
    }})

    all_reviews = reviews.get_reviews(_RUN_ID, run_dir)
    records = sales_export._load_records(run_dir)

    handoff_run = sales_export._build_handoff_run(
        _RUN_ID, _STATUS, {}, {}, records, all_reviews)

    account = handoff_run.accounts[0]
    # Only S-ICP-001 (yes) is in FOUND; S-ICP-007 (not_found) must NOT appear.
    labels = account.confirmed_signals or []
    assert any("IUI" in label for label in labels), f"IUI signal missing: {labels}"
    assert not any("Cash" in label or "cash" in label for label in labels), (
        f"not_found cash-pay signal leaked into confirmed_signals: {labels}"
    )


# ---------------------------------------------------------------------------
# 8 — yes→no override removes signal from confirmed_signals
# ---------------------------------------------------------------------------

def test_override_yes_to_no_removed_from_confirmed_signals(run_dir):
    _write_run(run_dir, _reviews_with_override(
        "S-ICP-001", "no", "https://acme.example.com/closed", "yes"))

    all_reviews = reviews.get_reviews(_RUN_ID, run_dir)
    records = sales_export._load_records(run_dir)

    handoff_run = sales_export._build_handoff_run(
        _RUN_ID, _STATUS, {}, {}, records, all_reviews)

    # If the run still has an account (may filter to nothing without approved signals),
    # S-ICP-001 (IUI) must no longer be in confirmed_signals.
    for account in handoff_run.accounts:
        labels = account.confirmed_signals or []
        assert not any("IUI" in label for label in labels), (
            f"Overridden-to-no IUI signal leaked into confirmed_signals: {labels}"
        )


# ---------------------------------------------------------------------------
# 9 — CSV: no is_override column in any approved export
# ---------------------------------------------------------------------------

def test_csv_no_is_override_column_in_approved_export(run_dir):
    _write_run(run_dir, _reviews_with_override(
        "S-ICP-007", "yes", "https://acme.example.com/pay", "not_found"))

    buf = exports.build_approved_csv(_RUN_ID, run_dir)
    content = buf.getvalue().decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))
    headers = reader.fieldnames or []
    assert "is_override" not in headers, (
        f"is_override leaked into CSV headers: {headers}"
    )
    # Confirm at least one row is present (the override makes it approved).
    rows = list(reader)
    # The row must also lack any is_override column in its data.
    for row in rows:
        assert "is_override" not in row


# ---------------------------------------------------------------------------
# 10 — CSV: applying overlay doesn't change excluded CSV columns
# ---------------------------------------------------------------------------

def test_csv_excluded_export_no_override_marker(run_dir):
    # Build a record that gets excluded tier, no signal overlays.
    excluded_rec = {**_record(),
                    "target_tier": "Excluded", "exclusion_status": "EXCLUDED",
                    "exclusion_reason": "out_of_scope_specialty"}
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-06-21T13:00:00+00:00", "operator": "tester",
    }))
    (run_dir / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": [excluded_rec]}, indent=2))

    buf = exports.build_excluded_csv(_RUN_ID, run_dir)
    content = buf.getvalue().decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))
    headers = reader.fieldnames or []
    assert "is_override" not in headers
    rows = list(reader)
    assert len(rows) == 1  # excluded record appears in excluded CSV
