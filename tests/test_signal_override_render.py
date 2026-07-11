"""
test_signal_override_render.py

Display-only tests for Prompt 4a: wiring apply_signal_overrides into the
dashboard render path (_load_merged_records) and showing an override badge in
results.html. No edit UI, no write path.

Covers: overridden signal moves into the FOUND group, badge renders for
overridden signals only, tier/qc/note overlay coexists with a signal override,
records without overrides render unchanged, and enriched_targets.json is never
written by a render.

Deterministic — no network, no subprocess.
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import runs  # noqa: E402
import ui  # noqa: E402

_RUN_ID = "RUN-20260621-120000-aaaa"
_COMPLETE = SimpleNamespace(status="complete")


def _record():
    """Record with one crawl-confirmed yes (S-ICP-001) and one not_found (S-ICP-007)."""
    return {
        "id": "T-1",
        "practice_name": "Acme Women's Health",
        "bullseye_score": 72,
        "fit_signal_score": 68,
        "confidence_score": 80,
        "target_tier": "Contender",
        "exclusion_status": "CLEAR",
        "enrichment_status": "complete",
        "signals": [
            {"signal_id": "S-ICP-001", "signal_label": "IUI offered",
             "signal_state": "yes", "evidence_text": "Lists IUI",
             "source_url": "https://orig.example.com/services",
             "confidence": "high", "positive_weight": 25},
            {"signal_id": "S-ICP-007", "signal_label": "Cash-pay visible",
             "signal_state": "not_found", "evidence_text": "",
             "source_url": "", "confidence": "low", "positive_weight": 16},
        ],
    }


def _write_run(run_directory, reviews_map=None):
    """Write status.json + enriched_targets.json (+ optional reviews.json)."""
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-06-21T12:00:00+00:00", "completed_at": "2026-06-21T12:30:00+00:00",
        "operator": "tester",
    }))
    (run_directory / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": [_record()]}, indent=2)
    )
    if reviews_map is not None:
        (run_directory / "reviews.json").write_text(json.dumps(reviews_map, indent=2))


def _override_entry(signal_id, state, source_url, original_state, note=""):
    """Build a stored signal_override entry as save_signal_override would write it."""
    return {
        "signal_id": signal_id, "override_state": state, "source_url": source_url,
        "override_note": note, "override_by": "tester",
        "override_at": "2026-06-21T13:00:00+00:00", "original_state": original_state,
    }


def _reviews_with_override(signal_id, state, source_url, original_state, **extra):
    """A reviews.json map with one record carrying a signal override (+ extra fields)."""
    entry = {
        "analyst_note": "", "override_tier": None, "override_reason": None,
        "qc_status": "pending", "reviewed_by": None, "reviewed_at": None,
        "extra_sales_angles": [],
        "signal_overrides": {
            signal_id: _override_entry(signal_id, state, source_url, original_state),
        },
    }
    entry.update(extra)
    return {"T-1": entry}


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """OUTPUT_RUNS_PATH -> tmp_path; return the (uncreated) run directory path."""
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path)
    return tmp_path / _RUN_ID


def _signals_by_id(record):
    return {s["signal_id"]: s for s in record["signals"]}


def _login(c):
    r = c.post("/login", data={"username": "tester", "password": "secret-pw"})
    assert r.status_code in (200, 302, 303)


# ---------------------------------------------------------------------------
# 1 — not_found -> yes lands in FOUND, with badge + operator source_url
# ---------------------------------------------------------------------------

def test_override_not_found_to_yes(run_dir):
    _write_run(run_dir, _reviews_with_override(
        "S-ICP-007", "yes", "https://acme.example.com/financing", "not_found"))

    merged = ui._load_merged_records(_RUN_ID, _COMPLETE)
    sig = _signals_by_id(merged[0])["S-ICP-007"]
    assert sig["signal_state"] == "yes"          # now FOUND
    assert sig["is_override"] is True
    assert sig["source_url"] == "https://acme.example.com/financing"

    with TestClient(main.app) as c:
        _login(c)
        html = c.get(f"/dashboard/{_RUN_ID}").text
    assert ">Override</span>" in html
    assert "https://acme.example.com/financing" in html


# ---------------------------------------------------------------------------
# 2 — yes -> no drops out of the FOUND group; badge present
# ---------------------------------------------------------------------------

def test_override_yes_to_no(run_dir):
    _write_run(run_dir, _reviews_with_override(
        "S-ICP-001", "no", "https://acme.example.com/closed", "yes"))

    merged = ui._load_merged_records(_RUN_ID, _COMPLETE)
    sig = _signals_by_id(merged[0])["S-ICP-001"]
    assert sig["signal_state"] == "no"
    assert sig["is_override"] is True
    # FOUND group = state == "yes" or state_inferred; this signal is no longer there.
    found = [s for s in merged[0]["signals"]
             if s["signal_state"] == "yes" or s.get("state_inferred")]
    assert "S-ICP-001" not in {s["signal_id"] for s in found}

    with TestClient(main.app) as c:
        _login(c)
        html = c.get(f"/dashboard/{_RUN_ID}").text
    assert ">Override</span>" in html


# ---------------------------------------------------------------------------
# 3 — no overrides: signals render exactly as before (regression)
# ---------------------------------------------------------------------------

def test_no_overrides_signals_unchanged(run_dir):
    _write_run(run_dir, {})  # empty reviews map: no overrides
    merged = ui._load_merged_records(_RUN_ID, _COMPLETE)
    assert merged[0]["signals"] == _record()["signals"]
    for s in merged[0]["signals"]:
        assert "is_override" not in s


# ---------------------------------------------------------------------------
# 4 — tier/qc/note overlay coexists with a signal override
# ---------------------------------------------------------------------------

def test_tier_and_signal_overlay_coexist(run_dir):
    reviews_map = _reviews_with_override(
        "S-ICP-007", "yes", "https://acme.example.com/pay", "not_found",
        override_tier="Bullseye", override_reason="Operator confirmed cash-pay",
        qc_status="approved", analyst_note="Called and confirmed",
    )
    _write_run(run_dir, reviews_map)

    merged = ui._load_merged_records(_RUN_ID, _COMPLETE)
    rec = merged[0]
    # Tier overlay intact.
    assert rec["displayed_tier"] == "Bullseye"
    assert rec["review"]["qc_status"] == "approved"
    assert rec["review"]["analyst_note"] == "Called and confirmed"
    # Signal overlay intact.
    assert _signals_by_id(rec)["S-ICP-007"]["is_override"] is True
    assert _signals_by_id(rec)["S-ICP-007"]["signal_state"] == "yes"
    # Scores untouched (no rescore).
    assert rec["bullseye_score"] == 72
    assert rec["fit_signal_score"] == 68


# ---------------------------------------------------------------------------
# 5 — badge appears only on overridden signals, not crawl-confirmed ones
# ---------------------------------------------------------------------------

def test_badge_only_on_overridden_signal(run_dir):
    _write_run(run_dir, _reviews_with_override(
        "S-ICP-007", "yes", "https://acme.example.com/pay", "not_found"))

    merged = ui._load_merged_records(_RUN_ID, _COMPLETE)
    by_id = _signals_by_id(merged[0])
    assert by_id["S-ICP-007"].get("is_override") is True
    assert "is_override" not in by_id["S-ICP-001"]  # crawl-confirmed, untouched

    with TestClient(main.app) as c:
        _login(c)
        html = c.get(f"/dashboard/{_RUN_ID}").text
    # Exactly one override badge for the one overridden signal.
    assert html.count(">Override</span>") == 1


# ---------------------------------------------------------------------------
# 6 — enriched_targets.json byte-identical after a dashboard render
# ---------------------------------------------------------------------------

def test_enriched_targets_untouched_by_render(run_dir):
    _write_run(run_dir, _reviews_with_override(
        "S-ICP-007", "yes", "https://acme.example.com/pay", "not_found"))
    et = run_dir / "enriched_targets.json"
    before = hashlib.sha256(et.read_bytes()).hexdigest()

    with TestClient(main.app) as c:
        _login(c)
        assert c.get(f"/dashboard/{_RUN_ID}").status_code == 200
    ui._load_merged_records(_RUN_ID, _COMPLETE)

    assert hashlib.sha256(et.read_bytes()).hexdigest() == before


# ---------------------------------------------------------------------------
# 7 — a run with zero reviews renders identically to pre-change (full guard)
# ---------------------------------------------------------------------------

def test_zero_overrides_full_passthrough(run_dir):
    # No reviews.json at all (the most common case).
    _write_run(run_dir, reviews_map=None)
    merged = ui._load_merged_records(_RUN_ID, _COMPLETE)
    # apply_signal_overrides is a pure pass-through here: signals deep-equal raw,
    # no is_override flag introduced anywhere.
    assert merged[0]["signals"] == _record()["signals"]
    assert all("is_override" not in s for s in merged[0]["signals"])
    # And the rest of the overlay (displayed_tier from default review) is intact.
    assert merged[0]["displayed_tier"] == "Contender"
