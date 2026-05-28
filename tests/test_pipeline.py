"""
Regression tests for the Bullseye enrichment pipeline.
All tests are deterministic — no API calls, no HTTP requests.

Coverage:
  - State normalization (outscraper_adapter)
  - Signal normalization — missing signals, phantom signals, ordering (signal_extractor)
  - Specialty exclusion — deterministic, no LLM required (exclusion_checker)
  - Geography exclusion (exclusion_checker)
  - No-web-presence exclusion (exclusion_checker)
  - CLEAR record tier invariant — never "Excluded" (exclusion_checker + scorer)
  - EXCLUDED record tier invariant — always "Excluded" (scorer)
  - Invariant repair in validate_and_finalize for contradictory pre-existing values
  - Excluded score cap (scorer)
"""

import sys
import os

# Ensure project root is on the path regardless of where pytest is invoked
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from ingestion.outscraper_adapter import _normalize_state, infer_specialty
from enrichment.signal_extractor import _validate_and_clean_signals
from enrichment.exclusion_checker import apply_exclusions
from enrichment.scorer import validate_and_finalize


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_ICP_SIGNALS = [
    {
        "signal_id": "S-ICP-001",
        "signal_label": "IUD insertion listed",
        "prompt_instruction": "Does this practice list IUD insertion?",
        "positive_weight": 15,
    },
    {
        "signal_id": "S-ICP-002",
        "signal_label": "REI on staff",
        "prompt_instruction": "Is there an REI on staff?",
        "positive_weight": -20,
        "note": "Negative signal",
    },
    {
        "signal_id": "S-ICP-003",
        "signal_label": "Independent private practice",
        "prompt_instruction": "Is this an independent private practice?",
        "positive_weight": 20,
    },
]

BASE_RUN_CONFIG = {
    "target_specialty": "OBGYN",
    "target_geography": ["TX", "FL", "GA"],
    "active_exclusion_rules": [
        "hospital_owned",
        "health_system_affiliated",
        "rei_on_staff",
        "wrong_specialty",
        "outside_geography",
        "no_web_presence",
    ],
    "bullseye_min_score": 75,
}

def _clear_record(score=80, state="TX", specialty="OBGYN"):
    """Minimal CLEAR record for use in tests."""
    return {
        "id": "T-test",
        "practice_name": "Test Practice",
        "specialty": specialty,
        "address_state": state,
        "address_city": "Houston",
        "address_zip": "77001",
        "website_url": "https://example.com",
        "bullseye_score": score,
        "fit_signal_score": score,
        "confidence_score": score,
        "_url_valid": True,
        "_context_text": "Some website text",
        "_llm_exclusion_triggers": [],
        "_llm_exclusion_rationale": "",
    }


# ---------------------------------------------------------------------------
# State normalization
# ---------------------------------------------------------------------------

class TestStateNormalization:

    def test_full_name_to_abbreviation(self):
        assert _normalize_state("Texas") == "TX"

    def test_full_name_case_insensitive(self):
        assert _normalize_state("texas") == "TX"
        assert _normalize_state("TEXAS") == "TX"

    def test_already_abbreviated_passthrough(self):
        assert _normalize_state("TX") == "TX"
        assert _normalize_state("FL") == "FL"
        assert _normalize_state("GA") == "GA"

    def test_all_target_states(self):
        assert _normalize_state("Florida") == "FL"
        assert _normalize_state("Georgia") == "GA"

    def test_other_states(self):
        assert _normalize_state("New York") == "NY"
        assert _normalize_state("California") == "CA"
        assert _normalize_state("West Virginia") == "WV"

    def test_empty_string_returns_empty(self):
        assert _normalize_state("") == ""

    def test_unknown_value_uppercased(self):
        # Unknown values are returned uppercased (best-effort)
        result = _normalize_state("Narnia")
        assert result == "NARNIA"


# ---------------------------------------------------------------------------
# Signal normalization
# ---------------------------------------------------------------------------

class TestSignalNormalization:

    def test_output_has_exactly_len_icp_entries(self):
        """Output always has one entry per ICP signal."""
        raw = [
            {"signal_id": "S-ICP-001", "signal_label": "IUD insertion listed",
             "signal_state": "yes", "confidence": "high",
             "evidence_text": "Listed.", "source_url": "https://x.com"},
        ]
        result = _validate_and_clean_signals(raw, SAMPLE_ICP_SIGNALS)
        assert len(result) == len(SAMPLE_ICP_SIGNALS)

    def test_missing_signals_get_not_found_default(self):
        """ICP signals not in LLM response are inserted as not_found."""
        raw = []  # LLM returned nothing
        result = _validate_and_clean_signals(raw, SAMPLE_ICP_SIGNALS)
        assert len(result) == 3
        for sig in result:
            assert sig["signal_state"] == "not_found"
            assert sig["confidence"] == "low"
            assert sig["evidence_text"] == ""
            assert sig["source_url"] == ""
            assert sig["analyst_note"] == ""

    def test_phantom_signal_ids_are_discarded(self):
        """Signals with IDs not in ICP checklist are dropped."""
        raw = [
            {"signal_id": "S-PHANTOM-999", "signal_label": "Made up",
             "signal_state": "yes", "confidence": "high",
             "evidence_text": "Ignore me.", "source_url": ""},
        ]
        result = _validate_and_clean_signals(raw, SAMPLE_ICP_SIGNALS)
        ids = [s["signal_id"] for s in result]
        assert "S-PHANTOM-999" not in ids
        assert len(result) == len(SAMPLE_ICP_SIGNALS)

    def test_output_order_matches_icp_order(self):
        """Output is in icp_signals order, not LLM response order."""
        raw = [
            {"signal_id": "S-ICP-003", "signal_label": "Independent",
             "signal_state": "yes", "confidence": "high",
             "evidence_text": "Independent.", "source_url": ""},
            {"signal_id": "S-ICP-001", "signal_label": "IUD",
             "signal_state": "no", "confidence": "high",
             "evidence_text": "Not listed.", "source_url": ""},
        ]
        result = _validate_and_clean_signals(raw, SAMPLE_ICP_SIGNALS)
        assert result[0]["signal_id"] == "S-ICP-001"
        assert result[1]["signal_id"] == "S-ICP-002"  # missing → not_found
        assert result[2]["signal_id"] == "S-ICP-003"

    def test_invalid_signal_state_coerced_to_not_found(self):
        """Bad signal_state values are coerced to not_found."""
        for bad_state in [True, False, 1, None, "maybe", ""]:
            raw = [{"signal_id": "S-ICP-001", "signal_label": "IUD",
                    "signal_state": bad_state, "confidence": "high",
                    "evidence_text": "", "source_url": ""}]
            result = _validate_and_clean_signals(raw, SAMPLE_ICP_SIGNALS)
            assert result[0]["signal_state"] == "not_found", \
                f"Expected not_found for signal_state={bad_state!r}"

    def test_invalid_confidence_coerced_to_low(self):
        """Bad confidence values are coerced to low."""
        raw = [{"signal_id": "S-ICP-001", "signal_label": "IUD",
                "signal_state": "yes", "confidence": "extreme",
                "evidence_text": "x", "source_url": ""}]
        result = _validate_and_clean_signals(raw, SAMPLE_ICP_SIGNALS)
        assert result[0]["confidence"] == "low"

    def test_signal_label_taken_from_icp_not_llm(self):
        """signal_label in output comes from ICP checklist, not LLM."""
        raw = [{"signal_id": "S-ICP-001", "signal_label": "LLM made this up",
                "signal_state": "yes", "confidence": "high",
                "evidence_text": "e", "source_url": ""}]
        result = _validate_and_clean_signals(raw, SAMPLE_ICP_SIGNALS)
        assert result[0]["signal_label"] == "IUD insertion listed"

    def test_positive_weight_carried_from_icp_on_matched_signal(self):
        """positive_weight is copied from the ICP definition onto matched signals."""
        raw = [{"signal_id": "S-ICP-002", "signal_label": "REI on staff",
                "signal_state": "yes", "confidence": "high",
                "evidence_text": "e", "source_url": ""}]
        result = _validate_and_clean_signals(raw, SAMPLE_ICP_SIGNALS)
        by_id = {s["signal_id"]: s for s in result}
        assert by_id["S-ICP-002"]["positive_weight"] == -20

    def test_positive_weight_carried_on_not_found_signal(self):
        """not_found defaults still carry the ICP positive_weight (for UI coloring)."""
        result = _validate_and_clean_signals([], SAMPLE_ICP_SIGNALS)
        by_id = {s["signal_id"]: s for s in result}
        assert by_id["S-ICP-001"]["positive_weight"] == 15
        assert by_id["S-ICP-002"]["positive_weight"] == -20


# ---------------------------------------------------------------------------
# Specialty inference (type column, with practice-name fallback)
# ---------------------------------------------------------------------------

class TestSpecialtyInference:

    def test_type_keyword_maps_to_canonical(self):
        assert infer_specialty("Obstetrician-gynecologist") == "OBGYN"

    def test_empty_type_falls_back_to_name(self):
        assert infer_specialty("", "Atlanta Obstetrics & Gynecology Associates") == "OBGYN"

    def test_name_fallback_only_used_when_type_unmatched(self):
        # type is empty, name carries the signal
        assert infer_specialty("", "Lakeside Urology Center") == "Urology"

    def test_unrecognized_type_kept_as_titlecased_label(self):
        assert infer_specialty("Wellness Center", "") == "Wellness Center"

    def test_unknown_when_neither_matches(self):
        assert infer_specialty("", "Main Street Medical") == "Unknown"

    def test_empty_inputs_return_unknown(self):
        assert infer_specialty("", "") == "Unknown"


# ---------------------------------------------------------------------------
# Specialty exclusion — deterministic
# ---------------------------------------------------------------------------

class TestSpecialtyExclusion:

    def test_wrong_specialty_fires_without_llm_trigger(self):
        """wrong_specialty fires purely from record data, no LLM trigger needed."""
        record = _clear_record(specialty="Cardiology")
        record["_llm_exclusion_triggers"] = []  # LLM says nothing
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "EXCLUDED"
        assert "wrong_specialty" in result["exclusion_reason"].lower() or \
               "specialty" in result["exclusion_reason"].lower()

    def test_matching_specialty_not_excluded(self):
        """Correct specialty does not trigger wrong_specialty."""
        record = _clear_record(specialty="OBGYN")
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"

    def test_specialty_check_is_case_insensitive(self):
        """Specialty comparison ignores case."""
        record = _clear_record(specialty="obgyn")
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"

    def test_empty_record_specialty_skips_specialty_check(self):
        """If record has no specialty set, wrong_specialty does not fire."""
        record = _clear_record(specialty="")
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        # Should not exclude on specialty alone (no specialty to compare)
        triggered = result.get("exclusion_reason") or ""
        assert "wrong_specialty" not in triggered

    def test_unknown_specialty_does_not_fire_wrong_specialty(self):
        """'Unknown' means detection failed, not a confirmed mismatch — no exclusion."""
        record = _clear_record(specialty="Unknown")
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert "wrong_specialty" not in (result.get("exclusion_reason") or "")

    def test_empty_target_specialty_skips_check(self):
        """If run_config has no target_specialty, check is skipped."""
        config = {**BASE_RUN_CONFIG, "target_specialty": ""}
        record = _clear_record(specialty="Cardiology")
        result = apply_exclusions(record, config)
        triggered = result.get("exclusion_reason") or ""
        assert "wrong_specialty" not in triggered


# ---------------------------------------------------------------------------
# Geography exclusion
# ---------------------------------------------------------------------------

class TestGeographyExclusion:

    def test_out_of_state_excluded(self):
        record = _clear_record(state="NY")
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "EXCLUDED"
        assert "outside_geography" in (result.get("exclusion_reason") or "").lower() or \
               "geography" in (result.get("exclusion_reason") or "").lower()

    def test_in_state_not_excluded(self):
        for state in ("TX", "FL", "GA"):
            record = _clear_record(state=state)
            result = apply_exclusions(record, BASE_RUN_CONFIG)
            assert result["exclusion_status"] == "CLEAR", \
                f"State {state} should not be geography-excluded"

    def test_empty_geography_config_skips_check(self):
        """Empty target_geography means no geography restriction."""
        config = {**BASE_RUN_CONFIG, "target_geography": []}
        record = _clear_record(state="NY")
        result = apply_exclusions(record, config)
        # Geography exclusion should not fire
        triggered = result.get("exclusion_reason") or ""
        assert "geography" not in triggered.lower()


# ---------------------------------------------------------------------------
# No-web-presence exclusion
# ---------------------------------------------------------------------------

class TestNoWebPresenceExclusion:

    def test_no_url_and_no_text_excluded(self):
        record = _clear_record()
        record["_url_valid"] = False
        record["_context_text"] = ""
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "EXCLUDED"
        assert "web presence" in (result.get("exclusion_reason") or "").lower()

    def test_valid_url_and_text_not_excluded(self):
        record = _clear_record()
        record["_url_valid"] = True
        record["_context_text"] = "Some website text here"
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"

    def test_not_active_in_config_does_not_fire(self):
        """no_web_presence only fires when in active_exclusion_rules."""
        config = {
            **BASE_RUN_CONFIG,
            "active_exclusion_rules": [r for r in BASE_RUN_CONFIG["active_exclusion_rules"]
                                        if r != "no_web_presence"],
        }
        record = _clear_record()
        record["_url_valid"] = False
        record["_context_text"] = ""
        result = apply_exclusions(record, config)
        # Should not fire no_web_presence
        triggered = result.get("exclusion_reason") or ""
        assert "web presence" not in triggered.lower()


# ---------------------------------------------------------------------------
# CLEAR record tier invariant
# ---------------------------------------------------------------------------

class TestClearRecordTierInvariant:

    def test_clear_high_score_is_bullseye(self):
        record = _clear_record(score=80)
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"
        assert result["target_tier"] == "Bullseye"

    def test_clear_low_score_is_watchlist_not_excluded(self):
        """CLEAR records with score < 75 should be Watchlist, never Excluded."""
        record = _clear_record(score=40)
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"
        assert result["target_tier"] == "Watchlist"
        assert result["target_tier"] != "Excluded"

    def test_clear_zero_score_is_watchlist(self):
        record = _clear_record(score=0)
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"
        assert result["target_tier"] == "Watchlist"


# ---------------------------------------------------------------------------
# Invariant repair in validate_and_finalize
# ---------------------------------------------------------------------------

class TestValidateAndFinalizeInvariant:

    def _base_record(self):
        return {
            "id": "T-test",
            "practice_name": "Test",
            "specialty": "OBGYN",
            "address_state": "TX",
            "address_city": "Dallas",
            "address_zip": "75201",
            "phone": "",
            "website_url": "",
            "metro_region_tag": "",
            "state_mandate_status": "",
            "bullseye_score": 40,
            "fit_signal_score": 40,
            "confidence_score": 40,
            "fit_confidence_status": "LOW FIT / LOW EVIDENCE",
            "signals": [],
            "sales_angle": [],
            "source_confidence": "partial",
            "enrichment_status": "complete",
            "qc_status": "pending",
            "internal_notes": "",
            "date_enriched": "2026-05-27",
            "enrichment_run_id": "RUN-001",
            "llm_model_used": "claude-sonnet-4-6",
            "llm_prompt_version": "signal_extraction_v1",
            "source_pipeline_version": "v1.0",
            "raw_input_source": "test.csv",
            "analyst_override_classification": None,
            "override_reason": None,
            "client_facing_rationale": None,
            "exclusion_reason": None,
        }

    def test_clear_excluded_tier_repaired_to_watchlist(self):
        """CLEAR + Excluded tier → repaired to Watchlist (score 40 < 75)."""
        record = self._base_record()
        record["exclusion_status"] = "CLEAR"
        record["target_tier"] = "Excluded"
        record["bullseye_score"] = 40
        result = validate_and_finalize(record)
        assert result["target_tier"] == "Watchlist"
        assert "Invariant violation" in result["internal_notes"]

    def test_clear_excluded_tier_repaired_to_bullseye(self):
        """CLEAR + Excluded tier → repaired to Bullseye (score 80 >= 75)."""
        record = self._base_record()
        record["exclusion_status"] = "CLEAR"
        record["target_tier"] = "Excluded"
        record["bullseye_score"] = 80
        result = validate_and_finalize(record)
        assert result["target_tier"] == "Bullseye"

    def test_excluded_non_excluded_tier_repaired(self):
        """EXCLUDED + Watchlist tier → repaired to Excluded."""
        record = self._base_record()
        record["exclusion_status"] = "EXCLUDED"
        record["target_tier"] = "Watchlist"
        record["exclusion_reason"] = "hospital_owned"
        result = validate_and_finalize(record)
        assert result["target_tier"] == "Excluded"
        assert "Invariant violation" in result["internal_notes"]

    def test_valid_consistent_record_unchanged(self):
        """Valid, consistent records are not modified."""
        record = self._base_record()
        record["exclusion_status"] = "CLEAR"
        record["target_tier"] = "Watchlist"
        result = validate_and_finalize(record)
        assert result["target_tier"] == "Watchlist"
        assert result["exclusion_status"] == "CLEAR"
        assert "Invariant violation" not in result.get("internal_notes", "")

    def test_excluded_record_score_capped_at_40(self):
        """EXCLUDED records have bullseye_score capped at 40."""
        record = self._base_record()
        record["exclusion_status"] = "EXCLUDED"
        record["target_tier"] = "Excluded"
        record["exclusion_reason"] = "hospital_owned"
        record["bullseye_score"] = 85
        result = validate_and_finalize(record)
        assert result["bullseye_score"] <= 40
