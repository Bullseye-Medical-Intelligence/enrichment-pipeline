"""
test_signal_overrides.py

Data-layer tests for the signal-override overlay (Prompt 2 of the override build).
Covers the SignalOverride schema validation, save_signal_override persistence
(original_state capture + preservation), apply_signal_overrides merge logic,
and the hard integrity guarantees: enriched_targets.json is never written, and
scores/tiers are never recomputed.

Deterministic — no network, no subprocess. Uses tmp_path as the run directory.
"""

import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# pipeline-api modules import each other by bare name; put the dir on the path.
_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("PIPELINE_API_KEY", "test-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import reviews  # noqa: E402
from schema import SignalOverride  # noqa: E402

_RUN_ID = "RUN-20260621-120000-aaaa"


def _signal(signal_id, state, label="Test Signal", weight=10):
    """Build a minimal signal object matching the pipeline output shape."""
    return {
        "signal_id": signal_id,
        "signal_label": label,
        "signal_state": state,
        "evidence_text": "" if state == "not_found" else "original evidence",
        "source_url": "" if state == "not_found" else "https://orig.example.com",
        "confidence": "high" if state == "yes" else "low",
        "positive_weight": weight,
        "state_inferred": False,
    }


def _record(record_id="T-1", tier="Contender", signals=None):
    """Build a minimal enriched record with scores, tier, and signals."""
    return {
        "id": record_id,
        "practice_name": "Acme Women's Health",
        "bullseye_score": 72,
        "fit_signal_score": 68,
        "confidence_score": 80,
        "target_tier": tier,
        "exclusion_status": "CLEAR",
        "signals": signals if signals is not None else [
            _signal("S-ICP-001", "yes"),
            _signal("S-ICP-007", "not_found"),
        ],
    }


def _write_enriched(run_directory, records):
    """Write an enriched_targets.json wrapper payload into the run directory."""
    path = run_directory / "enriched_targets.json"
    path.write_text(json.dumps({"run_id": _RUN_ID, "records": records}, indent=2))
    return path


# ---------------------------------------------------------------------------
# Test 1 — save on a not_found signal captures original_state
# ---------------------------------------------------------------------------

def test_save_override_captures_original_not_found(tmp_path):
    _write_enriched(tmp_path, [_record()])
    ov = SignalOverride(
        signal_id="S-ICP-007",
        override_state="yes",
        source_url="https://operator.example.com/services",
        override_note="Cash pay confirmed on services page",
        override_by="rajiv",
    )
    reviews.save_signal_override(_RUN_ID, "T-1", ov, tmp_path)

    stored = json.loads((tmp_path / "reviews.json").read_text())
    entry = stored["T-1"]["signal_overrides"]["S-ICP-007"]
    assert entry["original_state"] == "not_found"
    assert entry["override_state"] == "yes"
    assert entry["source_url"] == "https://operator.example.com/services"
    assert entry["override_by"] == "rajiv"
    assert entry["override_at"]  # server-stamped, non-empty


# ---------------------------------------------------------------------------
# Test 2 — merge applies the override
# ---------------------------------------------------------------------------

def test_merge_applies_override(tmp_path):
    _write_enriched(tmp_path, [_record()])
    ov = SignalOverride(
        signal_id="S-ICP-007",
        override_state="yes",
        source_url="https://operator.example.com/pay",
        override_note="Self-pay pricing listed",
        override_by="rajiv",
    )
    reviews.save_signal_override(_RUN_ID, "T-1", ov, tmp_path)

    review = reviews.get_review(_RUN_ID, "T-1", tmp_path)
    merged = reviews.apply_signal_overrides(_record(), review)

    by_id = {s["signal_id"]: s for s in merged["signals"]}
    s = by_id["S-ICP-007"]
    assert s["signal_state"] == "yes"
    assert s["is_override"] is True
    assert s["source_url"] == "https://operator.example.com/pay"
    assert s["evidence_text"] == "Self-pay pricing listed"
    # The non-overridden signal is untouched.
    assert "is_override" not in by_id["S-ICP-001"]


def test_merge_blank_note_uses_operator_verified(tmp_path):
    _write_enriched(tmp_path, [_record()])
    ov = SignalOverride(
        signal_id="S-ICP-007",
        override_state="yes",
        source_url="https://operator.example.com/pay",
        override_by="rajiv",
    )
    reviews.save_signal_override(_RUN_ID, "T-1", ov, tmp_path)
    review = reviews.get_review(_RUN_ID, "T-1", tmp_path)
    merged = reviews.apply_signal_overrides(_record(), review)
    s = {x["signal_id"]: x for x in merged["signals"]}["S-ICP-007"]
    assert s["evidence_text"] == "Operator-verified"


# ---------------------------------------------------------------------------
# Test 3 — re-override preserves original_state
# ---------------------------------------------------------------------------

def test_reoverride_preserves_original_state(tmp_path):
    _write_enriched(tmp_path, [_record()])
    first = SignalOverride(
        signal_id="S-ICP-007", override_state="yes",
        source_url="https://a.example.com", override_by="rajiv",
    )
    reviews.save_signal_override(_RUN_ID, "T-1", first, tmp_path)

    # Now rewrite the underlying record so a naive re-read would see "yes",
    # proving the captured original_state is preserved from the FIRST override.
    _write_enriched(tmp_path, [_record(signals=[
        _signal("S-ICP-001", "yes"),
        _signal("S-ICP-007", "yes"),
    ])])
    second = SignalOverride(
        signal_id="S-ICP-007", override_state="no",
        source_url="https://b.example.com", override_by="rajiv",
    )
    reviews.save_signal_override(_RUN_ID, "T-1", second, tmp_path)

    stored = json.loads((tmp_path / "reviews.json").read_text())
    entry = stored["T-1"]["signal_overrides"]["S-ICP-007"]
    assert entry["original_state"] == "not_found"  # from first capture
    assert entry["override_state"] == "no"          # updated
    assert entry["source_url"] == "https://b.example.com"


# ---------------------------------------------------------------------------
# Test 4 — invalid override_state rejected by schema
# ---------------------------------------------------------------------------

def test_invalid_override_state_rejected():
    with pytest.raises(ValidationError):
        SignalOverride(
            signal_id="S-ICP-007", override_state="maybe",
            source_url="https://x.example.com",
        )


# ---------------------------------------------------------------------------
# Test 5 — empty source_url rejected
# ---------------------------------------------------------------------------

def test_empty_source_url_rejected():
    with pytest.raises(ValidationError):
        SignalOverride(
            signal_id="S-ICP-007", override_state="yes", source_url="",
        )


def test_blank_signal_id_rejected():
    with pytest.raises(ValidationError):
        SignalOverride(
            signal_id="   ", override_state="yes",
            source_url="https://x.example.com",
        )


# ---------------------------------------------------------------------------
# Test 6 — enriched_targets.json is byte-identical after an override
# ---------------------------------------------------------------------------

def test_enriched_targets_untouched_by_override(tmp_path):
    path = _write_enriched(tmp_path, [_record()])
    before = path.read_bytes()

    ov = SignalOverride(
        signal_id="S-ICP-007", override_state="yes",
        source_url="https://operator.example.com", override_by="rajiv",
    )
    reviews.save_signal_override(_RUN_ID, "T-1", ov, tmp_path)

    after = path.read_bytes()
    assert before == after


# ---------------------------------------------------------------------------
# Test 7 — scores and tier on the merged record equal the pipeline values
# ---------------------------------------------------------------------------

def test_merge_does_not_change_scores_or_tier(tmp_path):
    _write_enriched(tmp_path, [_record()])
    ov = SignalOverride(
        signal_id="S-ICP-007", override_state="yes",
        source_url="https://operator.example.com", override_by="rajiv",
    )
    reviews.save_signal_override(_RUN_ID, "T-1", ov, tmp_path)
    review = reviews.get_review(_RUN_ID, "T-1", tmp_path)

    original = _record()
    merged = reviews.apply_signal_overrides(original, review)

    assert merged["bullseye_score"] == original["bullseye_score"]
    assert merged["fit_signal_score"] == original["fit_signal_score"]
    assert merged["confidence_score"] == original["confidence_score"]
    assert merged["target_tier"] == original["target_tier"]


# ---------------------------------------------------------------------------
# Test 8 — a record with no signal_overrides merges identically (regression)
# ---------------------------------------------------------------------------

def test_no_overrides_merges_identically(tmp_path):
    original = _record()
    # default_review has an empty signal_overrides map.
    merged = reviews.apply_signal_overrides(original, reviews.default_review())
    assert merged == original
    # Same when the review has no signal_overrides key at all.
    merged2 = reviews.apply_signal_overrides(original, {"qc_status": "pending"})
    assert merged2 == original


def test_existing_tier_overlay_unaffected_by_new_field(tmp_path):
    """A standard tier/QC review still persists and reads back unchanged."""
    from schema import ReviewEdit
    edit = ReviewEdit(
        override_tier="Bullseye", override_reason="Strong fit", qc_status="approved",
    )
    saved = reviews.save_review(_RUN_ID, "T-1", edit, "tester", tmp_path)
    assert saved["override_tier"] == "Bullseye"
    assert saved["qc_status"] == "approved"
    # The new key is present and empty — does not disturb existing behavior.
    assert saved["signal_overrides"] == {}


def test_tier_review_save_preserves_existing_signal_overrides(tmp_path):
    """Saving a tier review must not wipe previously-saved signal overrides."""
    from schema import ReviewEdit
    _write_enriched(tmp_path, [_record()])
    ov = SignalOverride(
        signal_id="S-ICP-007", override_state="yes",
        source_url="https://operator.example.com", override_by="rajiv",
    )
    reviews.save_signal_override(_RUN_ID, "T-1", ov, tmp_path)
    reviews.save_review(
        _RUN_ID, "T-1",
        ReviewEdit(override_tier="Bullseye", override_reason="x", qc_status="approved"),
        "tester", tmp_path,
    )
    overrides = reviews.get_signal_overrides(_RUN_ID, "T-1", tmp_path)
    assert "S-ICP-007" in overrides
    assert overrides["S-ICP-007"]["override_state"] == "yes"


# ---------------------------------------------------------------------------
# Test 9 — works for any signal_id / any ICP (no client-specific keys)
# ---------------------------------------------------------------------------

def test_works_for_arbitrary_signal_ids(tmp_path):
    rec = _record(record_id="X-99", signals=[
        _signal("CUSTOM-FOO", "not_found", label="Arbitrary signal"),
        _signal("ANOTHER-BAR-42", "no", label="Another"),
    ])
    _write_enriched(tmp_path, [rec])
    ov = SignalOverride(
        signal_id="ANOTHER-BAR-42", override_state="yes",
        source_url="https://operator.example.com/x", override_by="op",
    )
    reviews.save_signal_override(_RUN_ID, "X-99", ov, tmp_path)
    review = reviews.get_review(_RUN_ID, "X-99", tmp_path)
    merged = reviews.apply_signal_overrides(rec, review)
    by_id = {s["signal_id"]: s for s in merged["signals"]}
    assert by_id["ANOTHER-BAR-42"]["signal_state"] == "yes"
    assert by_id["ANOTHER-BAR-42"]["is_override"] is True
    assert by_id["CUSTOM-FOO"]["signal_state"] == "not_found"

    stored = reviews.get_signal_overrides(_RUN_ID, "X-99", tmp_path)
    assert stored["ANOTHER-BAR-42"]["original_state"] == "no"


def test_get_signal_overrides_empty_when_none(tmp_path):
    assert reviews.get_signal_overrides(_RUN_ID, "T-1", tmp_path) == {}


def test_original_state_empty_when_no_enriched_file(tmp_path):
    """If enriched_targets.json is absent, original_state degrades to ''."""
    ov = SignalOverride(
        signal_id="S-ICP-007", override_state="yes",
        source_url="https://operator.example.com", override_by="rajiv",
    )
    reviews.save_signal_override(_RUN_ID, "T-1", ov, tmp_path)
    stored = reviews.get_signal_overrides(_RUN_ID, "T-1", tmp_path)
    assert stored["S-ICP-007"]["original_state"] == ""
