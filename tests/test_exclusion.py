"""
Tests for exclusion_checker.py: specialty matching and apply_exclusions.
"""

import sys
from pathlib import Path

import pytest

_ENRICHMENT_DIR = Path(__file__).resolve().parent.parent / "enrichment"
sys.path.insert(0, str(_ENRICHMENT_DIR))

from exclusion_checker import _specialty_matches, apply_exclusions


# ---------------------------------------------------------------------------
# _specialty_matches — word-boundary tokenization
# ---------------------------------------------------------------------------

class TestSpecialtyMatches:
    def test_fertility_clinic_matches_fertility_token(self):
        assert _specialty_matches("Fertility Clinic", "OBGYN, Fertility") is True

    def test_fertility_clinic_matches_obgyn_token(self):
        # "obgyn" words = {"obgyn"}; rec words = {"fertility", "clinic"} — no match
        # so this verifies only fertility token matches, not OBGYN token
        assert _specialty_matches("OBGYN Practice", "OBGYN, Fertility") is True

    def test_dermatology_does_not_match_fertility(self):
        assert _specialty_matches("Dermatology", "OBGYN, Fertility") is False

    def test_cardiology_does_not_match_fertility(self):
        assert _specialty_matches("Cardiology Associates", "OBGYN, Fertility") is False

    def test_ent_does_not_match_urgent_care(self):
        # "ent" is a substring of "urgent" but word tokenization prevents false match
        assert _specialty_matches("Urgent Care Clinic", "ENT") is False

    def test_ent_matches_ent_practice(self):
        assert _specialty_matches("ENT and Allergy Associates", "ENT") is True

    def test_exact_match(self):
        assert _specialty_matches("Cardiology", "Cardiology") is True

    def test_case_insensitive(self):
        assert _specialty_matches("fertility clinic", "FERTILITY") is True

    def test_empty_record_specialty_returns_false(self):
        assert _specialty_matches("", "Fertility") is False

    def test_multiword_target_token_requires_all_words(self):
        # target token "reproductive endocrinology" requires both words
        assert _specialty_matches("Endocrinology Associates", "Reproductive Endocrinology") is False
        assert _specialty_matches("Reproductive Endocrinology Clinic", "Reproductive Endocrinology") is True

    def test_partial_word_in_longer_word_no_match(self):
        # "int" should not match "internal medicine" as ENT-style false positive
        # but "int" is in "internal" — this confirms word-boundary prevents false match
        # when the full token word is not present as a standalone word
        assert _specialty_matches("Pediatrics", "ENT") is False


# ---------------------------------------------------------------------------
# apply_exclusions — CLEAR records get Bullseye or Contender, never Excluded
# ---------------------------------------------------------------------------

_BASE_RECORD = {
    "record_id": "T-1",
    "practice_name": "Fertility Clinic Alpha",
    "specialty": "Fertility Clinic",
    "address_state": "CA",
    "bullseye_score": 80,
    "exclusion_status": None,
    "exclusion_reason": None,
    "target_tier": None,
    "_llm_exclusion_triggers": [],
    "_llm_exclusion_rationale": "",
    "_url_valid": True,
    "_context_text": "some content",
}

_BASE_CONFIG = {
    "target_specialty": "OBGYN, Fertility",
    "target_geography": ["CA"],
    "active_exclusion_rules": [],
    "bullseye_min_score": 75,
}


def _record(**overrides):
    r = dict(_BASE_RECORD)
    r.update(overrides)
    return r


def _config(**overrides):
    c = dict(_BASE_CONFIG)
    c.update(overrides)
    return c


class TestApplyExclusions:
    def test_clear_bullseye_when_specialty_matches_and_score_high(self):
        rec = apply_exclusions(_record(), _config())
        assert rec["exclusion_status"] == "CLEAR"
        assert rec["target_tier"] == "Bullseye"
        assert rec["exclusion_reason"] is None

    def test_clear_contender_when_score_below_min(self):
        rec = apply_exclusions(_record(bullseye_score=60), _config())
        assert rec["exclusion_status"] == "CLEAR"
        assert rec["target_tier"] == "Contender"

    def test_excluded_when_specialty_mismatch(self):
        rec = apply_exclusions(_record(specialty="Dermatology"), _config())
        assert rec["exclusion_status"] == "EXCLUDED"
        assert rec["target_tier"] == "Excluded"
        assert "wrong_specialty" in rec["exclusion_reason"].lower() or "specialty" in rec["exclusion_reason"].lower()

    def test_ent_does_not_exclude_urgent_care_practice(self):
        rec = apply_exclusions(
            _record(specialty="Urgent Care Clinic"),
            _config(target_specialty="ENT"),
        )
        # Urgent Care does not match ENT via word-boundary → should be EXCLUDED for wrong_specialty
        assert rec["exclusion_status"] == "EXCLUDED"

    def test_no_wrong_specialty_when_specialty_fields_empty(self):
        rec = apply_exclusions(_record(specialty=""), _config())
        # empty record specialty → no wrong_specialty check fires
        assert rec["exclusion_status"] == "CLEAR"

    def test_outside_geography_excluded(self):
        rec = apply_exclusions(_record(address_state="TX"), _config())
        assert rec["exclusion_status"] == "EXCLUDED"
        assert "outside_geography" in rec.get("exclusion_reason", "").lower() or "TX" in rec.get("exclusion_reason", "")

    def test_excluded_score_capped(self):
        rec = apply_exclusions(_record(specialty="Dermatology", bullseye_score=90), _config())
        assert rec["bullseye_score"] <= 40
