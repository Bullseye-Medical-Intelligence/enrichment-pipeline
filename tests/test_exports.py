"""
Tests for the approved-export gate (exports.is_approved).

Deterministic — no filesystem, no network. Verifies which (record, review)
pairs are eligible for the client deliverable exports, with focus on the
"Needs Verification" tier: unconfirmed accounts ship only after an analyst
confirms them with an override.
"""

import os
import sys
from pathlib import Path

# pipeline-api modules import each other by bare name; put the dir on the path.
_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("PIPELINE_API_KEY", "test-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import exports  # noqa: E402


def _rev(qc="approved", override=None):
    return {"qc_status": qc, "override_tier": override}


def test_approved_bullseye_is_eligible():
    rec = {"target_tier": "Bullseye", "exclusion_status": "CLEAR"}
    assert exports.is_approved(rec, _rev()) is True


def test_needs_verification_not_eligible_without_override():
    rec = {"target_tier": "Needs Verification", "exclusion_status": "CLEAR"}
    assert exports.is_approved(rec, _rev()) is False


def test_needs_verification_eligible_when_overridden_to_positive_tier():
    rec = {"target_tier": "Needs Verification", "exclusion_status": "CLEAR"}
    assert exports.is_approved(rec, _rev(override="Bullseye")) is True


def test_needs_verification_not_eligible_when_not_approved():
    rec = {"target_tier": "Needs Verification", "exclusion_status": "CLEAR"}
    assert exports.is_approved(rec, _rev(qc="pending")) is False


def test_excluded_still_blocked_without_override():
    rec = {"target_tier": "Excluded", "exclusion_status": "EXCLUDED"}
    assert exports.is_approved(rec, _rev()) is False


def test_manual_review_not_eligible_without_override():
    rec = {"target_tier": "Manual Review", "exclusion_status": "CLEAR"}
    assert exports.is_approved(rec, _rev()) is False


def test_manual_review_eligible_when_overridden_to_positive_tier():
    rec = {"target_tier": "Manual Review", "exclusion_status": "CLEAR"}
    assert exports.is_approved(rec, _rev(override="Contender")) is True


def test_low_score_enriched_contender_blocked_as_displayed_manual_review():
    """A Contender promoted to Manual Review by displayed_tier (low score, enriched)
    must NOT pass the approved gate even though raw target_tier reads 'Contender'."""
    rec = {
        "target_tier": "Contender", "exclusion_status": "CLEAR",
        "bullseye_score": 30, "enrichment_status": "enriched",
    }
    assert exports.is_approved(rec, _rev()) is False


def test_high_score_enriched_contender_still_eligible():
    """A Contender that stays Contender (score above the Manual Review floor) is eligible."""
    rec = {
        "target_tier": "Contender", "exclusion_status": "CLEAR",
        "bullseye_score": 70, "enrichment_status": "enriched",
    }
    assert exports.is_approved(rec, _rev()) is True
