"""
Tests for record_adapter presentation helpers.

Deterministic — no filesystem, no network. Focus on the Contact Priority
relabel, which maps the displayed tier (override if set, else pipeline tier) to
a rep-facing label and sort rank.
"""

import os
import sys
from pathlib import Path

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("PIPELINE_API_KEY", "test-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import record_adapter  # noqa: E402


def test_bullseye_maps_to_priority_outreach():
    rec = {"target_tier": "Bullseye"}
    assert record_adapter.contact_priority(rec, {}) == "Priority Outreach"


def test_needs_verification_maps_to_verify_engage():
    rec = {"target_tier": "Needs Verification"}
    assert record_adapter.contact_priority(rec, {}) == "Verify & Engage"


def test_watchlist_maps_to_develop():
    rec = {"target_tier": "Watchlist"}
    assert record_adapter.contact_priority(rec, {}) == "Develop"


def test_excluded_maps_to_do_not_pursue():
    rec = {"target_tier": "Excluded"}
    assert record_adapter.contact_priority(rec, {}) == "Do Not Pursue"


def test_override_drives_priority():
    rec = {"target_tier": "Watchlist"}
    review = {"override_tier": "Bullseye"}
    assert record_adapter.contact_priority(rec, review) == "Priority Outreach"


def test_rank_orders_priority_above_develop():
    bullseye = record_adapter.contact_priority_rank({"target_tier": "Bullseye"}, {})
    develop = record_adapter.contact_priority_rank({"target_tier": "Watchlist"}, {})
    excluded = record_adapter.contact_priority_rank({"target_tier": "Excluded"}, {})
    assert bullseye > develop > excluded


def test_unknown_tier_falls_back_to_develop():
    rec = {"target_tier": "SomethingNew"}
    assert record_adapter.contact_priority(rec, {}) == "Develop"
