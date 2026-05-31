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

from ingestion.outscraper_adapter import _normalize_state, infer_specialty
from enrichment.signal_extractor import (
    _validate_and_clean_signals,
    _calculate_scores,
    _apply_reinforcement,
    _build_call_brief,
    _build_empty_signals,
)
from enrichment.constants import empty_call_brief
from enrichment.exclusion_checker import apply_exclusions, _assign_tier
from enrichment.scorer import validate_and_finalize
from pipeline import _finalize_ingest_only, _records_needing_browser_retry, _load_manual_content


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

    def test_tiering_fields_carried_from_icp(self):
        """verification_required and cap_tier flow from the ICP onto each signal."""
        icp = [{
            "signal_id": "S-V", "signal_label": "Cash pay visible",
            "prompt_instruction": "?", "positive_weight": 10,
            "verification_required": True, "cap_tier": "Watchlist",
        }]
        result = _validate_and_clean_signals([], icp)
        assert result[0]["verification_required"] is True
        assert result[0]["cap_tier"] == "Watchlist"

    def test_tiering_fields_default_off_when_absent(self):
        """Signals without the optional fields default to off / empty."""
        result = _validate_and_clean_signals([], SAMPLE_ICP_SIGNALS)
        assert result[0]["verification_required"] is False
        assert result[0]["cap_tier"] == ""


# ---------------------------------------------------------------------------
# Scoring — not_found_weight penalty
# ---------------------------------------------------------------------------

class TestScoring:
    """Fit score = captured share of the achievable positive weight (0–100)."""

    def test_all_desirable_signals_confirmed_scores_full(self):
        icp = [{"signal_id": "S-1", "signal_label": "A", "prompt_instruction": "?",
                "positive_weight": 20},
               {"signal_id": "S-2", "signal_label": "B", "prompt_instruction": "?",
                "positive_weight": 30}]
        signals = [{"signal_id": "S-1", "signal_state": "yes", "confidence": "high"},
                   {"signal_id": "S-2", "signal_state": "yes", "confidence": "high"}]
        scores = _calculate_scores(signals, icp)
        assert scores["fit_signal_score"] == 100

    def test_partial_capture_is_proportional(self):
        # Capture 20 of an achievable 50 -> 40.
        icp = [{"signal_id": "S-1", "signal_label": "A", "prompt_instruction": "?",
                "positive_weight": 20},
               {"signal_id": "S-2", "signal_label": "B", "prompt_instruction": "?",
                "positive_weight": 30}]
        signals = [{"signal_id": "S-1", "signal_state": "yes", "confidence": "high"},
                   {"signal_id": "S-2", "signal_state": "no", "confidence": "high"}]
        scores = _calculate_scores(signals, icp)
        assert scores["fit_signal_score"] == 40

    def test_minor_signals_cannot_outscore_a_heavy_one(self):
        # One heavy signal (40) confirmed beats three minor ones (5 each) confirmed.
        heavy_icp = [{"signal_id": "H", "signal_label": "H", "prompt_instruction": "?",
                      "positive_weight": 40},
                     {"signal_id": "x", "signal_label": "x", "prompt_instruction": "?",
                      "positive_weight": 5}]
        heavy_sig = [{"signal_id": "H", "signal_state": "yes", "confidence": "high"},
                     {"signal_id": "x", "signal_state": "no", "confidence": "high"}]
        minor_icp = [{"signal_id": "a", "signal_label": "a", "prompt_instruction": "?",
                      "positive_weight": 5},
                     {"signal_id": "b", "signal_label": "b", "prompt_instruction": "?",
                      "positive_weight": 5},
                     {"signal_id": "c", "signal_label": "c", "prompt_instruction": "?",
                      "positive_weight": 5},
                     {"signal_id": "H", "signal_label": "H", "prompt_instruction": "?",
                      "positive_weight": 40}]
        minor_sig = [{"signal_id": "a", "signal_state": "yes", "confidence": "high"},
                     {"signal_id": "b", "signal_state": "yes", "confidence": "high"},
                     {"signal_id": "c", "signal_state": "yes", "confidence": "high"},
                     {"signal_id": "H", "signal_state": "no", "confidence": "high"}]
        heavy = _calculate_scores(heavy_sig, heavy_icp)["fit_signal_score"]
        minor = _calculate_scores(minor_sig, minor_icp)["fit_signal_score"]
        assert heavy > minor

    def test_friction_signal_yes_pulls_score_down(self):
        icp = [{"signal_id": "S-1", "signal_label": "A", "prompt_instruction": "?",
                "positive_weight": 40},
               {"signal_id": "S-hosp", "signal_label": "Hospital", "prompt_instruction": "?",
                "positive_weight": -20}]
        confirmed_only = [{"signal_id": "S-1", "signal_state": "yes", "confidence": "high"},
                          {"signal_id": "S-hosp", "signal_state": "no", "confidence": "high"}]
        with_friction = [{"signal_id": "S-1", "signal_state": "yes", "confidence": "high"},
                         {"signal_id": "S-hosp", "signal_state": "yes", "confidence": "high"}]
        clean = _calculate_scores(confirmed_only, icp)["fit_signal_score"]
        dinged = _calculate_scores(with_friction, icp)["fit_signal_score"]
        assert clean == 100
        assert dinged == 50  # (40 - 20) / 40 -> 50

    def test_not_found_weight_penalizes_when_other_signals_carry_weight(self):
        icp = [{"signal_id": "S-1", "signal_label": "Cash pay", "prompt_instruction": "?",
                "positive_weight": 10, "not_found_weight": -10},
               {"signal_id": "S-2", "signal_label": "B", "prompt_instruction": "?",
                "positive_weight": 30}]
        signals = [{"signal_id": "S-1", "signal_state": "not_found", "confidence": "low"},
                   {"signal_id": "S-2", "signal_state": "yes", "confidence": "high"}]
        scores = _calculate_scores(signals, icp)
        # achieved = 30 - 10 = 20 of an achievable 40 -> 50
        assert scores["fit_signal_score"] == 50

    def test_no_weight_penalizes_confirmed_absent_signal(self):
        # Cash pay confirmed "no" with no_weight -15: achieved = 30 - 15 = 15 of 40 -> 38.
        icp = [{"signal_id": "S-cash", "signal_label": "Cash pay", "prompt_instruction": "?",
                "positive_weight": 10, "no_weight": -15},
               {"signal_id": "S-2", "signal_label": "B", "prompt_instruction": "?",
                "positive_weight": 30}]
        signals = [{"signal_id": "S-cash", "signal_state": "no", "confidence": "high"},
                   {"signal_id": "S-2", "signal_state": "yes", "confidence": "high"}]
        scores = _calculate_scores(signals, icp)
        # achieved = 30 - 15 = 15 of an achievable 40 -> 38
        assert scores["fit_signal_score"] == 38

    def test_no_weight_defaults_to_zero_when_unset(self):
        # Without no_weight, a confirmed "no" only loses the missed credit (no penalty).
        icp = [{"signal_id": "S-cash", "signal_label": "Cash pay", "prompt_instruction": "?",
                "positive_weight": 10},
               {"signal_id": "S-2", "signal_label": "B", "prompt_instruction": "?",
                "positive_weight": 30}]
        signals = [{"signal_id": "S-cash", "signal_state": "no", "confidence": "high"},
                   {"signal_id": "S-2", "signal_state": "yes", "confidence": "high"}]
        scores = _calculate_scores(signals, icp)
        # achieved = 30 of an achievable 40 -> 75 (no extra penalty)
        assert scores["fit_signal_score"] == 75


class TestReinforcement:
    """Elective/cosmetic 'yes' should infer (boost) an unconfirmed cash-pay signal."""

    CASH_PAY_ICP = [
        {"signal_id": "S-cash", "signal_label": "Cash pay visible",
         "prompt_instruction": "?", "positive_weight": 30,
         "verification_required": True, "not_found_weight": -10},
        {"signal_id": "S-elective", "signal_label": "Elective procedures",
         "prompt_instruction": "?", "positive_weight": 20,
         "reinforces": "S-cash"},
    ]

    def _signals(self, cash_state, elective_state):
        return [
            {"signal_id": "S-cash", "signal_label": "Cash pay visible",
             "signal_state": cash_state, "confidence": "low",
             "verification_required": True, "state_inferred": False},
            {"signal_id": "S-elective", "signal_label": "Elective procedures",
             "signal_state": elective_state, "confidence": "high",
             "verification_required": False, "state_inferred": False},
        ]

    def test_elective_yes_infers_unconfirmed_cash_pay(self):
        signals = self._signals("not_found", "yes")
        _apply_reinforcement(signals, self.CASH_PAY_ICP)
        cash = next(s for s in signals if s["signal_id"] == "S-cash")
        assert cash["state_inferred"] is True

    def test_no_inference_when_reinforcing_signal_absent(self):
        signals = self._signals("not_found", "not_found")
        _apply_reinforcement(signals, self.CASH_PAY_ICP)
        cash = next(s for s in signals if s["signal_id"] == "S-cash")
        assert cash["state_inferred"] is False

    def test_no_inference_when_cash_pay_already_confirmed(self):
        signals = self._signals("yes", "yes")
        _apply_reinforcement(signals, self.CASH_PAY_ICP)
        cash = next(s for s in signals if s["signal_id"] == "S-cash")
        assert cash["state_inferred"] is False  # already directly confirmed

    def test_inferred_cash_pay_earns_partial_credit(self):
        inferred = self._signals("not_found", "yes")
        _apply_reinforcement(inferred, self.CASH_PAY_ICP)
        penalized = self._signals("not_found", "no")  # elective absent -> penalty
        inferred_fit = _calculate_scores(inferred, self.CASH_PAY_ICP)["fit_signal_score"]
        penalized_fit = _calculate_scores(penalized, self.CASH_PAY_ICP)["fit_signal_score"]
        assert inferred_fit > penalized_fit

    def test_inferred_cash_pay_skips_verification_gate(self):
        rec = _clear_record(score=90)
        rec["signals"] = self._signals("not_found", "yes")
        _apply_reinforcement(rec["signals"], self.CASH_PAY_ICP)
        # state_inferred now set -> verification gate must not fire
        assert _assign_tier(rec, 90, 75) == "Bullseye"

    def test_uninferred_cash_pay_triggers_verification_gate(self):
        rec = _clear_record(score=90)
        rec["signals"] = self._signals("not_found", "not_found")
        _apply_reinforcement(rec["signals"], self.CASH_PAY_ICP)
        assert _assign_tier(rec, 90, 75) == "Needs Verification"


# ---------------------------------------------------------------------------
# Tier assignment — verification + cap_tier
# ---------------------------------------------------------------------------

class TestTierAssignment:

    def _record_with_signals(self, signals):
        rec = _clear_record(score=90)
        rec["signals"] = signals
        return rec

    def test_high_score_no_flags_is_bullseye(self):
        assert _assign_tier(self._record_with_signals([]), 90, 75) == "Bullseye"

    def test_low_score_no_flags_is_watchlist(self):
        assert _assign_tier(self._record_with_signals([]), 50, 75) == "Watchlist"

    def test_unconfirmed_required_signal_caps_bullseye_at_needs_verification(self):
        signals = [{"signal_id": "S-1", "signal_state": "not_found",
                    "verification_required": True, "cap_tier": ""}]
        assert _assign_tier(self._record_with_signals(signals), 90, 75) == "Needs Verification"

    def test_confirmed_required_signal_does_not_cap(self):
        signals = [{"signal_id": "S-1", "signal_state": "yes",
                    "verification_required": True, "cap_tier": ""}]
        assert _assign_tier(self._record_with_signals(signals), 90, 75) == "Bullseye"

    def test_cap_tier_yes_caps_at_watchlist(self):
        signals = [{"signal_id": "S-hosp", "signal_state": "yes",
                    "verification_required": False, "cap_tier": "Watchlist"}]
        assert _assign_tier(self._record_with_signals(signals), 90, 75) == "Watchlist"

    def test_cap_tier_beats_verification(self):
        """A hospital-affiliation Watchlist cap wins over a Needs Verification flag."""
        signals = [
            {"signal_id": "S-hosp", "signal_state": "yes",
             "verification_required": False, "cap_tier": "Watchlist"},
            {"signal_id": "S-cash", "signal_state": "not_found",
             "verification_required": True, "cap_tier": ""},
        ]
        assert _assign_tier(self._record_with_signals(signals), 90, 75) == "Watchlist"

    def test_verification_does_not_lift_watchlist(self):
        """A not_found required signal never raises a low-score Watchlist up a rung."""
        signals = [{"signal_id": "S-1", "signal_state": "not_found",
                    "verification_required": True, "cap_tier": ""}]
        assert _assign_tier(self._record_with_signals(signals), 50, 75) == "Watchlist"

    def test_must_have_confirmed_no_caps_at_watchlist(self):
        """A required_for_bullseye signal confirmed 'no' caps at Watchlist even at top score."""
        signals = [{"signal_id": "S-cash", "signal_state": "no",
                    "required_for_bullseye": True, "cap_tier": ""}]
        assert _assign_tier(self._record_with_signals(signals), 95, 90) == "Watchlist"

    def test_must_have_not_found_caps_at_needs_verification(self):
        """A required_for_bullseye signal that is not_found caps at Needs Verification."""
        signals = [{"signal_id": "S-cash", "signal_state": "not_found",
                    "required_for_bullseye": True, "cap_tier": ""}]
        assert _assign_tier(self._record_with_signals(signals), 95, 90) == "Needs Verification"

    def test_must_have_confirmed_yes_allows_bullseye(self):
        """A required_for_bullseye signal confirmed 'yes' does not cap."""
        signals = [{"signal_id": "S-cash", "signal_state": "yes",
                    "required_for_bullseye": True, "cap_tier": ""}]
        assert _assign_tier(self._record_with_signals(signals), 95, 90) == "Bullseye"

    def test_must_have_inferred_bypasses_gate(self):
        """An inferred required_for_bullseye signal counts as confirmed and allows Bullseye."""
        signals = [{"signal_id": "S-cash", "signal_state": "not_found",
                    "required_for_bullseye": True, "cap_tier": "",
                    "state_inferred": True}]
        assert _assign_tier(self._record_with_signals(signals), 95, 90) == "Bullseye"

    def test_apply_exclusions_sets_needs_verification_tier(self):
        """End to end: a CLEAR record with an unconfirmed required signal is tiered NV."""
        rec = _clear_record(score=90, specialty="OBGYN")
        rec["signals"] = [{"signal_id": "S-1", "signal_state": "not_found",
                           "verification_required": True, "cap_tier": ""}]
        result = apply_exclusions(rec, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"
        assert result["target_tier"] == "Needs Verification"


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
        """Exclusion fires only when there is genuinely no URL in the record."""
        record = _clear_record()
        record["website_url"] = ""      # no URL at all
        record["_url_valid"] = False
        record["_context_text"] = ""
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "EXCLUDED"
        assert "web presence" in (result.get("exclusion_reason") or "").lower()

    def test_url_present_but_validation_failed_not_excluded(self):
        """URL validation failure alone must not trigger no_web_presence.
        A URL string in the record proves the practice has a website."""
        record = _clear_record()
        record["website_url"] = "https://nepenthewellness.com"
        record["_url_valid"] = False    # validator couldn't connect (SSL, timeout, etc.)
        record["_context_text"] = ""   # no crawled content
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"
        triggered = result.get("exclusion_reason") or ""
        assert "web presence" not in triggered.lower()

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
        record["website_url"] = ""
        record["_url_valid"] = False
        record["_context_text"] = ""
        result = apply_exclusions(record, config)
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
        """CLEAR + Excluded tier → repaired to Bullseye (score 95 >= 90)."""
        record = self._base_record()
        record["exclusion_status"] = "CLEAR"
        record["target_tier"] = "Excluded"
        record["bullseye_score"] = 95
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

    def test_call_brief_defaulted_when_missing(self):
        """A record without call_brief gets a fully shaped empty one."""
        record = self._base_record()
        record.pop("call_brief", None)
        result = validate_and_finalize(record)
        assert result["call_brief"] == empty_call_brief()

    def test_call_brief_partial_is_completed(self):
        """A partial call_brief keeps valid fields and fills the rest."""
        record = self._base_record()
        record["call_brief"] = {"opening_line": "Hi there", "top_evidence": "bad-type"}
        result = validate_and_finalize(record)
        brief = result["call_brief"]
        assert brief["opening_line"] == "Hi there"
        assert brief["top_evidence"] == []          # wrong type coerced to list
        assert brief["why_contact"] == ""           # missing string filled
        assert brief["missing_to_verify"] == []
        assert brief["hours_of_operation"] == ""    # missing string filled

    def test_call_brief_hours_of_operation_preserved(self):
        """hours_of_operation is preserved when already a string."""
        record = self._base_record()
        record["call_brief"] = {"hours_of_operation": "Mon-Fri 8am-5pm"}
        result = validate_and_finalize(record)
        assert result["call_brief"]["hours_of_operation"] == "Mon-Fri 8am-5pm"


# ---------------------------------------------------------------------------
# Rep call brief assembly
# ---------------------------------------------------------------------------

class TestCallBrief:

    ICP = [
        {"signal_id": "S-svc", "signal_label": "Service line listed",
         "prompt_instruction": "?", "positive_weight": 30},
        {"signal_id": "S-minor", "signal_label": "Minor service",
         "prompt_instruction": "?", "positive_weight": 10},
        {"signal_id": "S-cash", "signal_label": "Cash pay visible",
         "prompt_instruction": "?", "positive_weight": 25, "verification_required": True},
        {"signal_id": "S-hosp", "signal_label": "Hospital affiliated",
         "prompt_instruction": "?", "positive_weight": -30, "cap_tier": "Watchlist"},
    ]

    def _sig(self, sid, state, label, weight, conf="high", evidence="",
             verification_required=False, cap_tier="", inferred=False):
        return {"signal_id": sid, "signal_label": label, "signal_state": state,
                "confidence": conf, "evidence_text": evidence, "source_url": "",
                "positive_weight": weight, "verification_required": verification_required,
                "cap_tier": cap_tier, "state_inferred": inferred}

    def test_top_evidence_orders_by_weight_and_needs_evidence_text(self):
        signals = [
            self._sig("S-svc", "yes", "Service line listed", 30, evidence="Lists the service."),
            self._sig("S-minor", "yes", "Minor service", 10, evidence="Minor mention."),
            self._sig("S-cash", "yes", "Cash pay visible", 25, verification_required=True),  # no evidence_text
            self._sig("S-hosp", "no", "Hospital affiliated", -30),
        ]
        brief = _build_call_brief(signals, {"fit_signal_score": 80}, {"specialty": "OBGYN"}, {})
        points = [e["point"] for e in brief["top_evidence"]]
        assert points == ["Service line listed", "Minor service"]  # cash dropped (no evidence)

    def test_missing_to_verify_uses_verification_gate(self):
        signals = [
            self._sig("S-cash", "not_found", "Cash pay visible", 25, verification_required=True),
        ]
        brief = _build_call_brief(signals, {"fit_signal_score": 40}, {}, {})
        assert brief["missing_to_verify"] == ["Cash pay visible"]

    def test_inferred_signal_not_flagged_to_verify(self):
        signals = [
            self._sig("S-cash", "not_found", "Cash pay visible", 25,
                      verification_required=True, inferred=True),
        ]
        brief = _build_call_brief(signals, {"fit_signal_score": 70}, {}, {})
        assert brief["missing_to_verify"] == []

    def test_disqualifier_risk_from_friction_and_cap(self):
        signals = [
            self._sig("S-hosp", "yes", "Hospital affiliated", -30, cap_tier="Watchlist"),
        ]
        brief = _build_call_brief(signals, {"fit_signal_score": 20}, {}, {})
        assert len(brief["disqualifier_risk"]) == 1
        assert "Hospital affiliated" in brief["disqualifier_risk"][0]

    def test_generated_prep_lines_passed_through(self):
        # Prep lines require at least one confirmed "yes" signal with evidence —
        # the integrity gate clears them when top_evidence is empty.
        signals = [
            self._sig("S-svc", "yes", "Service line listed", 30, evidence="Lists the service."),
        ]
        brief = _build_call_brief(signals, {"fit_signal_score": 80}, {}, {
            "opening_line": "Hi", "likely_objection": "Busy", "discovery_question": "How?",
        })
        assert brief["opening_line"] == "Hi"
        assert brief["likely_objection"] == "Busy"
        assert brief["discovery_question"] == "How?"

    def test_why_contact_references_top_signals_and_fit(self):
        signals = [self._sig("S-svc", "yes", "Service line listed", 30, evidence="x")]
        brief = _build_call_brief(signals, {"fit_signal_score": 88}, {"specialty": "OBGYN"}, {})
        assert "Service line listed" in brief["why_contact"]
        assert "88" in brief["why_contact"]

    def test_hours_of_operation_passed_through_from_generated(self):
        brief = _build_call_brief([], {"fit_signal_score": 0}, {}, {
            "hours_of_operation": "Mon-Fri 8am-5pm",
        })
        assert brief["hours_of_operation"] == "Mon-Fri 8am-5pm"

    def test_hours_of_operation_defaults_to_empty_string(self):
        brief = _build_call_brief([], {"fit_signal_score": 0}, {}, {})
        assert brief["hours_of_operation"] == ""


# ---------------------------------------------------------------------------
# Hallucination reduction — evidence gate, confidence weighting,
# empty-context gate, source confidence tier cap
# ---------------------------------------------------------------------------

class TestEvidenceGate:
    """'yes' signals without evidence_text or source_url are downgraded to not_found."""

    _ICP = [{"signal_id": "S-1", "signal_label": "A", "prompt_instruction": "?",
             "positive_weight": 20}]

    def _raw(self, state, evidence="found it", url="https://example.com/p"):
        return [{"signal_id": "S-1", "signal_state": state,
                 "evidence_text": evidence, "source_url": url, "confidence": "high"}]

    def test_yes_with_evidence_and_url_passes(self):
        out = _validate_and_clean_signals(self._raw("yes"), self._ICP)
        assert out[0]["signal_state"] == "yes"

    def test_yes_without_evidence_text_downgraded(self):
        out = _validate_and_clean_signals(self._raw("yes", evidence=""), self._ICP)
        assert out[0]["signal_state"] == "not_found"
        assert out[0]["confidence"] == "low"

    def test_yes_without_source_url_downgraded(self):
        out = _validate_and_clean_signals(self._raw("yes", url=""), self._ICP)
        assert out[0]["signal_state"] == "not_found"
        assert out[0]["confidence"] == "low"

    def test_yes_with_whitespace_only_evidence_downgraded(self):
        out = _validate_and_clean_signals(self._raw("yes", evidence="   "), self._ICP)
        assert out[0]["signal_state"] == "not_found"

    def test_not_found_without_evidence_unchanged(self):
        out = _validate_and_clean_signals(self._raw("not_found", evidence="", url=""), self._ICP)
        assert out[0]["signal_state"] == "not_found"

    def test_no_without_evidence_unchanged(self):
        out = _validate_and_clean_signals(self._raw("no", evidence="", url=""), self._ICP)
        assert out[0]["signal_state"] == "no"


class TestConfidenceWeightedScoring:
    """Fit credit scales with evidence confidence — low-confidence 'yes' scores less."""

    _ICP = [{"signal_id": "S-1", "signal_label": "A", "prompt_instruction": "?",
             "positive_weight": 20}]

    def _sig(self, confidence):
        return [{"signal_id": "S-1", "signal_state": "yes", "confidence": confidence}]

    def test_high_confidence_yes_scores_full(self):
        scores = _calculate_scores(self._sig("high"), self._ICP)
        assert scores["fit_signal_score"] == 100

    def test_medium_confidence_yes_scores_less_than_high(self):
        high = _calculate_scores(self._sig("high"), self._ICP)["fit_signal_score"]
        med  = _calculate_scores(self._sig("medium"), self._ICP)["fit_signal_score"]
        assert med < high

    def test_low_confidence_yes_scores_less_than_medium(self):
        med = _calculate_scores(self._sig("medium"), self._ICP)["fit_signal_score"]
        low = _calculate_scores(self._sig("low"), self._ICP)["fit_signal_score"]
        assert low < med

    def test_low_confidence_yes_earns_50_percent_credit(self):
        # weight=20, credit=0.5 → achieved=10 of max=20 → fit=50
        scores = _calculate_scores(self._sig("low"), self._ICP)
        assert scores["fit_signal_score"] == 50

    def test_medium_confidence_yes_earns_75_percent_credit(self):
        # weight=20, credit=0.75 → achieved=15 of max=20 → fit=75
        scores = _calculate_scores(self._sig("medium"), self._ICP)
        assert scores["fit_signal_score"] == 75


class TestEmptySignalsHelper:
    """_build_empty_signals returns correctly shaped all-not_found signals."""

    _ICP = [
        {"signal_id": "S-1", "signal_label": "A", "prompt_instruction": "?",
         "positive_weight": 20, "required_for_bullseye": True},
        {"signal_id": "S-2", "signal_label": "B", "prompt_instruction": "?",
         "positive_weight": 10},
    ]

    def test_returns_one_signal_per_icp_entry(self):
        out = _build_empty_signals(self._ICP)
        assert len(out) == 2

    def test_all_signals_are_not_found(self):
        out = _build_empty_signals(self._ICP)
        assert all(s["signal_state"] == "not_found" for s in out)

    def test_all_signals_are_low_confidence(self):
        out = _build_empty_signals(self._ICP)
        assert all(s["confidence"] == "low" for s in out)

    def test_icp_fields_carried_through(self):
        out = _build_empty_signals(self._ICP)
        s1 = next(s for s in out if s["signal_id"] == "S-1")
        assert s1["positive_weight"] == 20
        assert s1["required_for_bullseye"] is True

    def test_state_inferred_is_false(self):
        out = _build_empty_signals(self._ICP)
        assert all(s["state_inferred"] is False for s in out)


class TestSourceConfidenceTierCap:
    """Records with limited/failed source confidence cannot reach Bullseye."""

    _ICP = [{"signal_id": "S-1", "signal_label": "A", "prompt_instruction": "?",
             "positive_weight": 20}]

    def _record(self, source_confidence):
        rec = _clear_record(score=95)
        rec["source_confidence"] = source_confidence
        rec["signals"] = []
        return rec

    def test_limited_confidence_caps_at_watchlist(self):
        assert _assign_tier(self._record("limited"), 95, 75) == "Watchlist"

    def test_failed_confidence_caps_at_watchlist(self):
        assert _assign_tier(self._record("failed"), 95, 75) == "Watchlist"

    def test_complete_confidence_allows_bullseye(self):
        assert _assign_tier(self._record("complete"), 95, 75) == "Bullseye"

    def test_partial_confidence_allows_bullseye(self):
        assert _assign_tier(self._record("partial"), 95, 75) == "Bullseye"

    def test_missing_source_confidence_allows_bullseye(self):
        # Records without source_confidence set are not capped.
        rec = _clear_record(score=95)
        rec["signals"] = []
        assert _assign_tier(rec, 95, 75) == "Bullseye"


# ---------------------------------------------------------------------------
# Call brief integrity gate, not_found_reason, inferred_from attribution
# ---------------------------------------------------------------------------

class TestCallBriefIntegrity:
    """Opening lines and sales angles are cleared when no confirmed signals exist."""

    _ICP = [{"signal_id": "S-1", "signal_label": "Service listed", "prompt_instruction": "?",
             "positive_weight": 20}]

    def _confirmed_sig(self):
        return {"signal_id": "S-1", "signal_label": "Service listed", "signal_state": "yes",
                "confidence": "high", "evidence_text": "Lists the service.", "source_url": "https://x.com",
                "positive_weight": 20, "verification_required": False, "cap_tier": "",
                "state_inferred": False}

    def test_opening_line_cleared_when_no_confirmed_signals(self):
        brief = _build_call_brief([], {"fit_signal_score": 0}, {}, {"opening_line": "I saw X."})
        assert brief["opening_line"] == ""

    def test_likely_objection_cleared_when_no_confirmed_signals(self):
        brief = _build_call_brief([], {"fit_signal_score": 0}, {}, {"likely_objection": "Already set."})
        assert brief["likely_objection"] == ""

    def test_discovery_question_cleared_when_no_confirmed_signals(self):
        brief = _build_call_brief([], {"fit_signal_score": 0}, {}, {"discovery_question": "How?"})
        assert brief["discovery_question"] == ""

    def test_hours_of_operation_preserved_when_no_confirmed_signals(self):
        # hours_of_operation is factual (office hours), kept regardless of signal state.
        brief = _build_call_brief([], {"fit_signal_score": 0}, {}, {"hours_of_operation": "Mon-Fri 9-5"})
        assert brief["hours_of_operation"] == "Mon-Fri 9-5"

    def test_prep_lines_preserved_when_confirmed_signal_exists(self):
        signals = [self._confirmed_sig()]
        brief = _build_call_brief(signals, {"fit_signal_score": 80}, {}, {
            "opening_line": "I saw your service listed.",
            "likely_objection": "Already set.",
        })
        assert brief["opening_line"] == "I saw your service listed."
        assert brief["likely_objection"] == "Already set."

    def test_why_contact_populated_independently_of_gate(self):
        # why_contact is grounded from validated signals, not LLM — always set.
        brief = _build_call_brief([], {"fit_signal_score": 30}, {"specialty": "OBGYN"}, {})
        assert "OBGYN" in brief["why_contact"]
        assert "30" in brief["why_contact"]


class TestNotFoundReason:
    """not_found_reason distinguishes why a signal could not be confirmed."""

    _ICP = [{"signal_id": "S-1", "signal_label": "A", "prompt_instruction": "?",
             "positive_weight": 20}]

    def test_empty_context_signals_have_no_context_reason(self):
        out = _build_empty_signals(self._ICP)
        assert out[0]["not_found_reason"] == "no_context"

    def test_evidence_gate_downgrade_sets_evidence_gate_reason(self):
        raw = [{"signal_id": "S-1", "signal_state": "yes",
                "evidence_text": "", "source_url": "", "confidence": "high"}]
        out = _validate_and_clean_signals(raw, self._ICP)
        assert out[0]["signal_state"] == "not_found"
        assert out[0]["not_found_reason"] == "evidence_gate"

    def test_evidence_gate_fires_on_missing_url_too(self):
        raw = [{"signal_id": "S-1", "signal_state": "yes",
                "evidence_text": "some evidence", "source_url": "", "confidence": "high"}]
        out = _validate_and_clean_signals(raw, self._ICP)
        assert out[0]["not_found_reason"] == "evidence_gate"

    def test_normal_llm_not_found_has_empty_reason(self):
        raw = [{"signal_id": "S-1", "signal_state": "not_found",
                "evidence_text": "", "source_url": "", "confidence": "low"}]
        out = _validate_and_clean_signals(raw, self._ICP)
        assert out[0]["not_found_reason"] == ""

    def test_confirmed_yes_has_empty_reason(self):
        raw = [{"signal_id": "S-1", "signal_state": "yes",
                "evidence_text": "confirmed text", "source_url": "https://x.com", "confidence": "high"}]
        out = _validate_and_clean_signals(raw, self._ICP)
        assert out[0]["not_found_reason"] == ""

    def test_default_missing_signal_has_empty_reason(self):
        # A signal the LLM omitted entirely gets a default not_found entry.
        out = _validate_and_clean_signals([], self._ICP)
        assert out[0]["not_found_reason"] == ""


class TestInferredFrom:
    """_apply_reinforcement records which signal triggered inference."""

    _ICP = [
        {"signal_id": "S-source", "signal_label": "Elective procedures",
         "prompt_instruction": "?", "positive_weight": 18, "reinforces": "S-target"},
        {"signal_id": "S-target", "signal_label": "Cash pay visible",
         "prompt_instruction": "?", "positive_weight": 30},
    ]

    def _signals(self, source_state, target_state):
        return [
            {"signal_id": "S-source", "signal_label": "Elective procedures",
             "signal_state": source_state, "state_inferred": False, "inferred_from": ""},
            {"signal_id": "S-target", "signal_label": "Cash pay visible",
             "signal_state": target_state, "state_inferred": False, "inferred_from": ""},
        ]

    def test_reinforcement_sets_inferred_from(self):
        signals = self._signals("yes", "not_found")
        _apply_reinforcement(signals, self._ICP)
        target = next(s for s in signals if s["signal_id"] == "S-target")
        assert target["inferred_from"] == "S-source"

    def test_no_reinforcement_leaves_inferred_from_empty(self):
        signals = self._signals("not_found", "not_found")
        _apply_reinforcement(signals, self._ICP)
        target = next(s for s in signals if s["signal_id"] == "S-target")
        assert target["inferred_from"] == ""

    def test_source_signal_keeps_empty_inferred_from(self):
        signals = self._signals("yes", "not_found")
        _apply_reinforcement(signals, self._ICP)
        source = next(s for s in signals if s["signal_id"] == "S-source")
        assert source["inferred_from"] == ""

    def test_empty_signals_helper_sets_empty_inferred_from(self):
        out = _build_empty_signals(self._ICP)
        assert all(s["inferred_from"] == "" for s in out)


class TestIngestOnly:
    """The --ingest-only roster pass: normalize + structural exclusions, no enrichment."""

    def _roster_record(self, state="TX", website="https://example.com", specialty="OBGYN"):
        return {
            "id": "T-ingest",
            "practice_name": "Roster Practice",
            "specialty": specialty,
            "address_state": state,
            "address_city": "Houston",
            "address_zip": "77001",
            "website_url": website,
        }

    def test_clear_record_marked_not_enriched(self):
        out = _finalize_ingest_only([self._roster_record()], BASE_RUN_CONFIG)
        rec = out[0]
        assert rec["enrichment_status"] == "not_enriched"
        assert rec["exclusion_status"] == "CLEAR"
        assert rec["bullseye_score"] == 0
        assert rec["signals"] == []

    def test_structural_exclusion_still_fires(self):
        # Wrong geography should be excluded even in ingest-only mode.
        out = _finalize_ingest_only([self._roster_record(state="CA")], BASE_RUN_CONFIG)
        rec = out[0]
        assert rec["exclusion_status"] == "EXCLUDED"
        assert rec["target_tier"] == "Excluded"
        assert rec["enrichment_status"] == "not_enriched"

    def test_output_schema_is_complete(self):
        # validate_and_finalize must leave a fully-shaped record the UI can render.
        out = _finalize_ingest_only([self._roster_record()], BASE_RUN_CONFIG)
        rec = out[0]
        assert isinstance(rec.get("call_brief"), dict)
        assert isinstance(rec.get("sales_angle"), list)
        assert rec.get("qc_status") == "pending"

    def test_not_enriched_status_survives_validation(self):
        rec = {
            "practice_name": "X", "exclusion_status": "CLEAR",
            "target_tier": "Watchlist", "enrichment_status": "not_enriched",
        }
        out = validate_and_finalize(rec)
        assert out["enrichment_status"] == "not_enriched"


class TestBrowserRetrySelection:
    """_records_needing_browser_retry selects only blocked/thin records with a URL."""

    def test_limited_source_confidence_selected(self):
        recs = [{"website_url": "https://a.com", "source_confidence": "limited",
                 "_context_text": "x" * 5000}]
        assert _records_needing_browser_retry(recs) == recs

    def test_failed_source_confidence_selected(self):
        recs = [{"website_url": "https://a.com", "source_confidence": "failed",
                 "_context_text": "x" * 5000}]
        assert len(_records_needing_browser_retry(recs)) == 1

    def test_thin_context_selected(self):
        recs = [{"website_url": "https://a.com", "source_confidence": "partial",
                 "_context_text": "too short"}]
        assert len(_records_needing_browser_retry(recs)) == 1

    def test_healthy_record_skipped(self):
        recs = [{"website_url": "https://a.com", "source_confidence": "complete",
                 "_context_text": "x" * 5000}]
        assert _records_needing_browser_retry(recs) == []

    def test_no_url_skipped_even_if_blocked(self):
        # A browser cannot help a record that has no URL.
        recs = [{"website_url": "", "source_confidence": "failed", "_context_text": ""}]
        assert _records_needing_browser_retry(recs) == []


class TestChallengeDetection:
    """_looks_like_challenge flags bot/security interstitials, not real content."""

    def _detect(self, html):
        import importlib, sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "extraction"))
        mod = importlib.import_module("playwright_extractor")
        return mod._looks_like_challenge(html)

    def test_cloudflare_just_a_moment_flagged(self):
        assert self._detect("<html><title>Just a moment...</title></html>") is True

    def test_checking_your_browser_flagged(self):
        assert self._detect("<body>Checking your browser before accessing</body>") is True

    def test_verify_human_flagged(self):
        assert self._detect("<div>Please verify you are a human</div>") is True

    def test_real_content_not_flagged(self):
        html = "<html><body>Welcome to our TMS and ketamine clinic in Dallas.</body></html>"
        assert self._detect(html) is False

    def test_empty_html_not_flagged(self):
        assert self._detect("") is False


class TestManualContent:
    """_load_manual_content injects operator-provided page content, no crawl."""

    def _write(self, tmp_path, name, data):
        p = tmp_path / name
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return str(p)

    def test_html_file_extracts_visible_text(self, tmp_path):
        html = ("<html><head><title>T</title><script>var x=1;</script></head>"
                "<body><h1>Dallas Ketamine Clinic</h1>"
                "<p>We offer Spravato and TMS therapy for depression.</p>"
                "</body></html>")
        path = self._write(tmp_path, "page.html", html)
        rec = {"website_url": "https://x.com"}
        _load_manual_content([rec], path)
        assert "Ketamine" in rec["_context_text"]
        assert "var x=1" not in rec["_context_text"]   # script stripped
        assert rec["_pages_crawled"] == ["[Manual content]"]
        assert rec["source_confidence"] == "partial"
        assert rec["_url_valid"] is True

    def test_plain_text_used_verbatim(self, tmp_path):
        text = "TMS therapy and ketamine infusions offered. Cash pay accepted. " * 3
        path = self._write(tmp_path, "notes.txt", text)
        rec = {"website_url": "https://x.com"}
        _load_manual_content([rec], path)
        assert "ketamine infusions" in rec["_context_text"]
        assert rec["source_confidence"] == "partial"

    def test_thin_content_left_short_for_downstream_gate(self, tmp_path):
        # Under MIN_CONTEXT_CHARS: helper does not pad; the empty-context gate
        # in extract_signals will set signals not_found downstream.
        path = self._write(tmp_path, "tiny.txt", "too short")
        rec = {"website_url": "https://x.com"}
        _load_manual_content([rec], path)
        assert rec["_context_text"] == "too short"
        assert len(rec["_context_text"]) < 150


class TestNoContextReason:
    """_no_context_reason explains a zero-data record at the record level."""

    def _reason(self, record, context_text=""):
        from enrichment.signal_extractor import _no_context_reason
        return _no_context_reason(record, context_text)

    def test_no_url(self):
        r = self._reason({"website_url": ""})
        assert "No website URL" in r

    def test_url_error_surfaced(self):
        r = self._reason({"website_url": "https://x.com", "_url_error": "HTTP 403"})
        assert "HTTP 403" in r
        assert "could not be reached" in r

    def test_blocked_no_text(self):
        r = self._reason({"website_url": "https://x.com"}, context_text="")
        assert "no readable text" in r

    def test_thin_text_reports_char_count(self):
        r = self._reason({"website_url": "https://x.com"}, context_text="abc")
        assert "3 characters" in r
