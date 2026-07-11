"""
Tests for record_adapter presentation helpers.

Deterministic — no filesystem, no network. Contact Priority IS the displayed
tier (override if set, else pipeline tier); the four-tier ladder already names
the action, so there is no separate relabel. Rank orders the queue.
"""

import os
import sys
from pathlib import Path

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import record_adapter  # noqa: E402
from schema import VALID_OVERRIDE_TIERS  # noqa: E402


def test_override_tiers_match_four_tier_ladder():
    """Analyst override options use the four-tier ladder; Warm/Strong/Cold are gone."""
    assert VALID_OVERRIDE_TIERS == frozenset(
        {"Bullseye", "Needs Verification", "Contender", "Excluded"}
    )


def test_bullseye_priority_is_tier():
    rec = {"target_tier": "Bullseye"}
    assert record_adapter.contact_priority(rec, {}) == "Bullseye"


def test_needs_verification_priority_is_tier():
    rec = {"target_tier": "Needs Verification"}
    assert record_adapter.contact_priority(rec, {}) == "Needs Verification"


def test_contender_priority_is_tier():
    rec = {"target_tier": "Contender"}
    assert record_adapter.contact_priority(rec, {}) == "Contender"


def test_excluded_priority_is_tier():
    rec = {"target_tier": "Excluded"}
    assert record_adapter.contact_priority(rec, {}) == "Excluded"


def test_override_drives_priority():
    rec = {"target_tier": "Contender"}
    review = {"override_tier": "Bullseye"}
    assert record_adapter.contact_priority(rec, review) == "Bullseye"


def test_rank_orders_bullseye_above_contender_above_excluded():
    bullseye = record_adapter.contact_priority_rank({"target_tier": "Bullseye"}, {})
    contender = record_adapter.contact_priority_rank({"target_tier": "Contender"}, {})
    excluded = record_adapter.contact_priority_rank({"target_tier": "Excluded"}, {})
    assert bullseye > contender > excluded


def test_unknown_tier_label_is_verbatim_rank_falls_back():
    rec = {"target_tier": "SomethingNew"}
    assert record_adapter.contact_priority(rec, {}) == "SomethingNew"
    # Unknown tiers sort at the Contender rank, never above a known tier.
    assert record_adapter.contact_priority_rank(rec, {}) == 1


# ---------------------------------------------------------------------------
# signal_column_state — dashboard at-a-glance ICP-signal columns
# ---------------------------------------------------------------------------

def test_signal_column_state_strongest_wins():
    rec = {"signals": [
        {"signal_id": "a", "signal_state": "no"},
        {"signal_id": "b", "signal_state": "yes"},
    ]}
    assert record_adapter.signal_column_state(rec, ["a", "b"]) == "yes"


def test_signal_column_state_inferred_beats_no():
    rec = {"signals": [
        {"signal_id": "a", "signal_state": "no"},
        {"signal_id": "b", "signal_state": "not_found", "state_inferred": True},
    ]}
    assert record_adapter.signal_column_state(rec, ["a", "b"]) == "inferred"


def test_signal_column_state_no_beats_not_found():
    rec = {"signals": [
        {"signal_id": "a", "signal_state": "not_found"},
        {"signal_id": "b", "signal_state": "no"},
    ]}
    assert record_adapter.signal_column_state(rec, ["a", "b"]) == "no"


def test_signal_column_state_blank_when_absent():
    rec = {"signals": [{"signal_id": "x", "signal_state": "yes"}]}
    assert record_adapter.signal_column_state(rec, ["a", "b"]) == ""


def test_signal_column_state_confirmed_yes_beats_inferred_flag():
    rec = {"signals": [{"signal_id": "a", "signal_state": "yes", "state_inferred": True}]}
    assert record_adapter.signal_column_state(rec, ["a"]) == "yes"


def test_signal_column_state_handles_missing_signals_key():
    assert record_adapter.signal_column_state({}, ["a"]) == ""
