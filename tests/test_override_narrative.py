"""
test_override_narrative.py

An operator override changes a record's signal states but cannot rewrite the
rep-facing prose composed from them. Before this, rejecting a false positive
left the card's opener and sales angles still citing the retracted claim, and
the client Bullseye Target Report never saw the overlay at all.

Covers reviews.apply_signal_overrides' narrative invalidation, the
retracted_signal_labels helper the operator UI explains it with, and the
client-package path that now carries the overlay.

Deterministic — no network, no subprocess, no LLM.
"""

import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_API_DIR = _REPO / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO))

import client_exports  # noqa: E402
import reviews  # noqa: E402

_SUITE = "Surgical suite available"
_CASH = "Cash-pay / self-pay offering"


def _signal(signal_id, label, state, weight=30):
    return {
        "signal_id": signal_id,
        "signal_label": label,
        "signal_state": state,
        "evidence_text": "" if state != "yes" else f"{label} stated on the site.",
        "source_url": "" if state != "yes" else "https://practice.example/services",
        "confidence": "high",
        "positive_weight": weight,
        "state_inferred": False,
    }


def _record(**over):
    """A record whose prose cites both confirmed signals."""
    rec = {
        "id": "T-1",
        "record_id": "T-1",
        "practice_name": "Acme Women's Health",
        "bullseye_score": 78,
        "target_tier": "Bullseye",
        "exclusion_status": "CLEAR",
        "enrichment_status": "complete",
        "signals": [
            _signal("S-01", _SUITE, "yes"),
            _signal("S-02", _CASH, "yes"),
        ],
        "sales_angle": [
            "They run procedures in their own surgical suite.",
            "Self-pay pricing is already published.",
        ],
        "call_brief": {
            "why_contact": f"OBGYN practice: {_SUITE} + {_CASH} (fit 78).",
            "key_contact": "Practice Manager",
            "opening_line": "I saw you have a surgical suite on site.",
            "likely_objection": "We already have a device we like.",
            "discovery_question": "How do you handle facility fees today?",
            "hours_of_operation": "Mon-Fri 8:00-5:00",
            "top_evidence": [
                {"point": _SUITE, "evidence": "Surgical suite on site.",
                 "source_url": "https://practice.example/services"},
                {"point": _CASH, "evidence": "Self-pay pricing published.",
                 "source_url": "https://practice.example/billing"},
            ],
            "missing_to_verify": [],
            "disqualifier_risk": [],
        },
    }
    rec.update(over)
    return rec


def _review(signal_id, override_state, original_state):
    return {
        "qc_status": "approved",
        "override_tier": None,
        "signal_overrides": {
            signal_id: {
                "signal_id": signal_id,
                "override_state": override_state,
                "original_state": original_state,
                "source_url": "",
                "override_note": "Checked the site, not offered.",
                "override_by": "rajiv",
                "override_at": "2026-08-20T12:00:00+00:00",
            }
        },
    }


_RETRACTION = _review("S-01", "no", "yes")


# ---------------------------------------------------------------------------
# A retraction withdraws the prose composed from it
# ---------------------------------------------------------------------------

def test_retracted_signal_clears_sales_angles():
    """The exact reported failure: an analyst rejects the surgical suite, and
    the card must stop offering an angle built on it."""
    merged = reviews.apply_signal_overrides(_record(), _RETRACTION)
    assert merged["sales_angle"] == []


def test_retracted_signal_clears_the_composed_prep_lines():
    merged = reviews.apply_signal_overrides(_record(), _RETRACTION)
    brief = merged["call_brief"]
    assert brief["opening_line"] == ""
    assert brief["likely_objection"] == ""
    assert brief["discovery_question"] == ""
    assert brief["why_contact"] == ""


def test_retraction_keeps_hours_of_operation():
    """Office hours are a fact about the practice, not a claim about a signal."""
    merged = reviews.apply_signal_overrides(_record(), _RETRACTION)
    assert merged["call_brief"]["hours_of_operation"] == "Mon-Fri 8:00-5:00"


def test_retracted_evidence_drops_out_of_top_evidence():
    """Rejected evidence must not keep rendering as evidence; the signal that
    still stands keeps its entry."""
    merged = reviews.apply_signal_overrides(_record(), _RETRACTION)
    points = [e["point"] for e in merged["call_brief"]["top_evidence"]]
    assert _SUITE not in points
    assert _CASH in points


def test_legacy_bare_string_evidence_does_not_raise():
    """Runs frozen before top_evidence became structured carry bare label
    strings. The filter must read them, not crash the whole dashboard."""
    rec = _record()
    rec["call_brief"]["top_evidence"] = [_SUITE, "Lists IUI"]

    merged = reviews.apply_signal_overrides(rec, _RETRACTION)

    assert merged["call_brief"]["top_evidence"] == ["Lists IUI"]
    assert merged["call_brief"]["opening_line"] == ""


def test_retraction_leaves_grounded_signal_fields_alone():
    merged = reviews.apply_signal_overrides(_record(), _RETRACTION)
    assert merged["call_brief"]["key_contact"] == "Practice Manager"
    assert len(merged["signals"]) == 2


def test_no_confirmed_signal_left_clears_everything():
    """Overriding away the last confirmed signal leaves nothing to open with."""
    review = _review("S-01", "no", "yes")
    review["signal_overrides"]["S-02"] = dict(
        _review("S-02", "not_found", "yes")["signal_overrides"]["S-02"])
    merged = reviews.apply_signal_overrides(_record(), review)
    assert merged["sales_angle"] == []
    assert merged["call_brief"]["top_evidence"] == []


# ---------------------------------------------------------------------------
# Overrides that do NOT retract evidence must leave the prose intact
# ---------------------------------------------------------------------------

def test_override_that_adds_evidence_keeps_the_narrative():
    """not_found -> yes can only make the prose understate the account, never
    overstate it. Clearing here would destroy rep value for no integrity gain."""
    rec = _record(signals=[_signal("S-01", _SUITE, "yes"),
                           _signal("S-03", "Patient financing visible", "not_found")])
    merged = reviews.apply_signal_overrides(rec, _review("S-03", "yes", "not_found"))
    assert merged["sales_angle"] == rec["sales_angle"]
    assert merged["call_brief"]["opening_line"]


def test_override_that_re_sources_a_confirmed_signal_keeps_the_narrative():
    """yes -> yes with new evidence text: the claim still stands."""
    merged = reviews.apply_signal_overrides(_record(), _review("S-01", "yes", "yes"))
    assert merged["sales_angle"]
    assert merged["call_brief"]["opening_line"]


def test_record_without_overrides_is_returned_unchanged():
    rec = _record()
    assert reviews.apply_signal_overrides(rec, reviews.default_review()) is rec
    assert reviews.apply_signal_overrides(rec, {"qc_status": "pending"}) is rec


def test_missing_call_brief_does_not_invent_one():
    rec = _record()
    rec.pop("call_brief")
    merged = reviews.apply_signal_overrides(rec, _RETRACTION)
    assert merged["call_brief"] == {}
    assert merged["sales_angle"] == []


# ---------------------------------------------------------------------------
# retracted_signal_labels — what the operator UI explains the removal with
# ---------------------------------------------------------------------------

def test_retracted_labels_name_the_withdrawn_signal():
    assert reviews.retracted_signal_labels(_record(), _RETRACTION) == [_SUITE]


def test_retracted_labels_empty_when_an_override_adds_evidence():
    rec = _record(signals=[_signal("S-03", "Patient financing visible", "not_found")])
    assert reviews.retracted_signal_labels(rec, _review("S-03", "yes", "not_found")) == []


def test_retracted_labels_empty_without_overrides():
    assert reviews.retracted_signal_labels(_record(), reviews.default_review()) == []


# ---------------------------------------------------------------------------
# strip_override_markers — the internal marker never reaches a client
# ---------------------------------------------------------------------------

def test_strip_override_markers_removes_the_internal_flag():
    merged = reviews.apply_signal_overrides(_record(), _review("S-01", "yes", "yes"))
    assert any(s.get("is_override") for s in merged["signals"])
    stripped = reviews.strip_override_markers(merged)
    assert not any("is_override" in s for s in stripped["signals"])


# ---------------------------------------------------------------------------
# The client package carries the overlay
# ---------------------------------------------------------------------------

def test_client_report_records_carry_the_override():
    """Regression: the Bullseye Target Report was handed raw pipeline records,
    so a signal an analyst rejected still reached the client as confirmed."""
    approved = client_exports._approved_records([_record()], {"T-1": _RETRACTION})

    assert len(approved) == 1
    by_id = {s["signal_id"]: s for s in approved[0]["signals"]}
    assert by_id["S-01"]["signal_state"] == "no"
    # And the prose built on it is gone rather than shipped to the client.
    assert approved[0]["sales_angle"] == []
    assert approved[0]["call_brief"]["opening_line"] == ""
    # The internal operator marker never travels with it.
    assert not any("is_override" in s for s in approved[0]["signals"])


def test_client_report_selection_is_unchanged_by_the_overlay():
    """The overlay must not silently change which records ship — selection reads
    tier and exclusion status, neither of which a signal override touches."""
    records = [_record(), _record(id="T-2", record_id="T-2", target_tier="Contender")]
    all_reviews = {"T-1": _RETRACTION, "T-2": {"qc_status": "approved"}}

    approved = client_exports._approved_records(records, all_reviews)

    assert sorted(r["id"] for r in approved) == ["T-1", "T-2"]


def test_client_report_ordering_still_follows_score():
    records = [_record(id="T-lo", record_id="T-lo", bullseye_score=40),
               _record(id="T-hi", record_id="T-hi", bullseye_score=95)]
    approved = client_exports._approved_records(
        records, {"T-lo": {"qc_status": "approved"}, "T-hi": {"qc_status": "approved"}})
    assert [r["id"] for r in approved] == ["T-hi", "T-lo"]


def test_reviews_json_is_never_written_by_the_merge(tmp_path):
    """The overlay is read-only on this path — no state escapes the merge."""
    before = sorted(p.name for p in tmp_path.iterdir())
    reviews.apply_signal_overrides(_record(), _RETRACTION)
    reviews.retracted_signal_labels(_record(), _RETRACTION)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_merge_never_recomputes_scores_or_tier():
    merged = reviews.apply_signal_overrides(_record(), _RETRACTION)
    assert merged["bullseye_score"] == 78
    assert merged["target_tier"] == "Bullseye"


def test_original_record_is_not_mutated():
    """Callers reuse the raw record (retracted_signal_labels reads its original
    states), so the merge must return a copy."""
    rec = _record()
    snapshot = json.dumps(rec, sort_keys=True)
    reviews.apply_signal_overrides(rec, _RETRACTION)
    assert json.dumps(rec, sort_keys=True) == snapshot
