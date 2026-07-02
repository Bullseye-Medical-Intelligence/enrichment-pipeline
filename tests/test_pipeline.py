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

from ingestion.outscraper_adapter import _normalize_state, _parse_full_address, infer_specialty
from ingestion.npi_lookup import (
    _normalize_phone,
    _names_agree,
    _extract_taxonomy_codes,
)
from enrichment.signal_extractor import (
    _validate_and_clean_signals,
    _calculate_scores,
    _apply_reinforcement,
    _build_call_brief,
    _build_empty_signals,
    _parse_providers,
    _parse_primary_contact,
    _format_key_contact,
    _apply_physician_prefix,
    _build_system_prompt,
    DEFAULT_CONTACT_STRATEGY,
)
from enrichment.constants import empty_call_brief
from enrichment.exclusion_checker import apply_exclusions, _assign_tier
from enrichment.scorer import validate_and_finalize
from pipeline import (
    _finalize_ingest_only,
    _records_needing_browser_retry,
    _load_manual_content,
    _load_step4_checkpoint,
)


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

    def test_territory_full_names(self):
        assert _normalize_state("Puerto Rico") == "PR"
        assert _normalize_state("Guam") == "GU"


# ---------------------------------------------------------------------------
# Full-address parsing
# ---------------------------------------------------------------------------

class TestFullAddressParsing:

    def test_city_state_zip(self):
        assert _parse_full_address("Miami, FL 33101") == {
            "address_city": "Miami", "address_state": "FL", "address_zip": "33101"}

    def test_city_state_no_zip_does_not_crash(self):
        # The optional zip group used to be .strip()'d unconditionally, which
        # raised AttributeError on a missing zip and silently dropped the row.
        r = _parse_full_address("Austin, TX")
        assert (r["address_city"], r["address_state"], r["address_zip"]) == ("Austin", "TX", "")

    def test_multi_segment_uses_trailing_city_state(self):
        r = _parse_full_address("123 Oak Ave, Suite 5, Miami, FL 33101")
        assert (r["address_city"], r["address_state"], r["address_zip"]) == ("Miami", "FL", "33101")

    def test_street_city_full_state_no_zip(self):
        r = _parse_full_address("500 Main St, Dallas, Texas")
        assert (r["address_city"], r["address_state"], r["address_zip"]) == ("Dallas", "Texas", "")

    def test_zip_plus_four(self):
        r = _parse_full_address("Dallas, TX 75201-1234")
        assert (r["address_state"], r["address_zip"]) == ("TX", "75201-1234")

    def test_empty_and_unparseable_return_blanks(self):
        blank = {"address_city": "", "address_state": "", "address_zip": ""}
        assert _parse_full_address("") == blank
        assert _parse_full_address("no delimiters here") == blank


# ---------------------------------------------------------------------------
# Manual adapter state normalization
# ---------------------------------------------------------------------------

class TestManualAdapterStateNormalization:

    def test_full_state_name_normalized(self):
        from ingestion.manual_adapter import _map_row
        rec = _map_row({"practice_name": "X", "address_state": "Florida"}, 2)
        assert rec["address_state"] == "FL"

    def test_abbreviation_passthrough(self):
        from ingestion.manual_adapter import _map_row
        rec = _map_row({"practice_name": "X", "address_state": "tx"}, 2)
        assert rec["address_state"] == "TX"


# ---------------------------------------------------------------------------
# Step-4 checkpoint
# ---------------------------------------------------------------------------

class TestStep4Checkpoint:

    def test_load_skips_failed_rows(self, tmp_path):
        """A failed record in the checkpoint (e.g. from an older version that
        persisted failures) is ignored on load, so a resume re-attempts it instead
        of freezing it as completed. complete and needs_review rows are kept."""
        import json as _json
        path = tmp_path / "step4_checkpoint.ndjson"
        path.write_text(
            _json.dumps({"id": "T-1", "enrichment_status": "complete"}) + "\n"
            + _json.dumps({"id": "T-2", "enrichment_status": "failed"}) + "\n"
            + _json.dumps({"id": "T-3", "enrichment_status": "needs_review"}) + "\n",
            encoding="utf-8",
        )
        loaded = _load_step4_checkpoint(str(tmp_path))
        assert set(loaded) == {"T-1", "T-3"}


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
        """verification_required, cap_tier, and floor_tier flow from the ICP onto each signal."""
        icp = [{
            "signal_id": "S-V", "signal_label": "Cash pay visible",
            "prompt_instruction": "?", "positive_weight": 10,
            "verification_required": True, "cap_tier": "Contender",
            "floor_tier": "Contender", "required_for_contender": True,
        }]
        result = _validate_and_clean_signals([], icp)
        assert result[0]["verification_required"] is True
        assert result[0]["cap_tier"] == "Contender"
        assert result[0]["floor_tier"] == "Contender"
        assert result[0]["required_for_contender"] is True

    def test_required_for_contender_carried_on_all_signal_paths(self):
        """required_for_contender must survive every signal-construction path."""
        icp = [{
            "signal_id": "S-Q", "signal_label": "Qualifier",
            "prompt_instruction": "?", "positive_weight": 20,
            "required_for_contender": True,
        }]
        # Path A: empty-signals (thin context, no LLM call)
        assert _build_empty_signals(icp)[0]["required_for_contender"] is True
        # Path B: validated LLM output (confirmed signal)
        confirmed = [{"signal_id": "S-Q", "signal_state": "yes", "confidence": "high",
                      "evidence_text": "Listed", "source_url": "https://x.com/s"}]
        assert _validate_and_clean_signals(confirmed, icp)[0]["required_for_contender"] is True
        # Path C: default not_found insertion (signal omitted by the LLM)
        assert _validate_and_clean_signals([], icp)[0]["required_for_contender"] is True

    def test_floor_tier_carried_on_all_signal_paths(self):
        """floor_tier must survive every signal-construction path, not just cap_tier.

        Regression guard: a confirmed floor_tier signal lifts a thin-score record
        past the Manual Review gate. If the extractor stops copying floor_tier (as
        it once silently did), this guarantee dies and the simulator/pipeline diverge.
        """
        icp = [{
            "signal_id": "S-F", "signal_label": "Niche qualifier",
            "prompt_instruction": "?", "positive_weight": 0,
            "floor_tier": "Contender",
        }]
        # Path A: empty-signals (thin context, no LLM call)
        assert _build_empty_signals(icp)[0]["floor_tier"] == "Contender"
        # Path B: validated LLM output
        confirmed = [{"signal_id": "S-F", "signal_state": "yes", "confidence": "high",
                      "evidence_text": "Listed", "source_url": "https://x.com/s"}]
        validated = _validate_and_clean_signals(confirmed, icp)
        assert validated[0]["floor_tier"] == "Contender"
        # Path C: tier engine honors it on a sub-floor score
        rec = {"bullseye_score": 20, "source_confidence": "complete", "signals": validated}
        assert _assign_tier(rec, 20, 90) == "Contender"

    def test_tiering_fields_default_off_when_absent(self):
        """Signals without the optional fields default to off / empty."""
        result = _validate_and_clean_signals([], SAMPLE_ICP_SIGNALS)
        assert result[0]["verification_required"] is False
        assert result[0]["cap_tier"] == ""
        assert result[0]["floor_tier"] == ""
        assert result[0]["required_for_contender"] is False


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


class TestFemasysCartridgeScoring:
    """Femasys v12 cartridge scored by the unchanged (legacy) engine: every positive
    weight shares the fit denominator, a confirmed negative folds into fit, and tiers
    come from cap_tier / floor_tier / required_for_bullseye. Achieved with config
    only — no engine changes."""

    ICP = [
        {"signal_id": "cash_pay_signal", "signal_label": "Cash pay",
         "prompt_instruction": "?", "positive_weight": 50,
         "required_for_bullseye": True, "floor_tier": "Contender"},
        {"signal_id": "fertility_services", "signal_label": "Fertility",
         "prompt_instruction": "?", "positive_weight": 35,
         "required_for_bullseye": True, "floor_tier": "Contender"},
        {"signal_id": "iui_listed", "signal_label": "IUI",
         "prompt_instruction": "?", "positive_weight": 15},
        {"signal_id": "cycle_monitoring_listed", "signal_label": "Cycle monitoring",
         "prompt_instruction": "?", "positive_weight": 10},
        {"signal_id": "patient_financing_visible", "signal_label": "Financing",
         "prompt_instruction": "?", "positive_weight": 10},
        {"signal_id": "ivf_listed", "signal_label": "IVF",
         "prompt_instruction": "?", "positive_weight": -20, "cap_tier": "Contender"},
        {"signal_id": "rei_on_staff", "signal_label": "REI",
         "prompt_instruction": "?", "positive_weight": -20, "cap_tier": "Contender"},
    ]

    RUN_CONFIG = {
        "target_specialty": "OBGYN",
        "target_geography": [],
        "active_exclusion_rules": ["wrong_specialty", "outside_geography",
                                   "no_web_presence", "hospital_owned",
                                   "health_system_affiliated"],
        "bullseye_min_score": 80,
    }

    def _signals(self, **states):
        """Build output-shaped signals carrying the ICP tier flags.

        Each value is a state string ("yes"/"no"/"not_found") or a
        (state, confidence) tuple; omitted signals default to not_found/low.
        """
        out = []
        for icp_sig in self.ICP:
            val = states.get(icp_sig["signal_id"], ("not_found", "low"))
            state, conf = val if isinstance(val, tuple) else (val, "high")
            out.append({
                "signal_id": icp_sig["signal_id"],
                "signal_label": icp_sig["signal_label"],
                "signal_state": state,
                "confidence": conf,
                "positive_weight": icp_sig.get("positive_weight", 0),
                "required_for_bullseye": icp_sig.get("required_for_bullseye", False),
                "required_for_contender": icp_sig.get("required_for_contender", False),
                "cap_tier": icp_sig.get("cap_tier", ""),
                "floor_tier": icp_sig.get("floor_tier", ""),
                "verification_required": icp_sig.get("verification_required", False),
                "state_inferred": False,
            })
        return out

    def _tier(self, signals, specialty="OBGYN"):
        """Score the signals, run the exclusion/tier pass, return the record."""
        rec = _clear_record(specialty=specialty)
        rec["signals"] = signals
        rec["source_confidence"] = "complete"
        rec.update(_calculate_scores(signals, self.ICP))
        apply_exclusions(rec, self.RUN_CONFIG)
        return rec

    # --- fit math (legacy engine: all positive weights share the denominator) ---

    def test_full_positive_capture_is_100(self):
        scores = _calculate_scores(
            self._signals(cash_pay_signal="yes", fertility_services="yes",
                          iui_listed="yes", cycle_monitoring_listed="yes",
                          patient_financing_visible="yes"), self.ICP)
        assert scores["fit_signal_score"] == 100

    def test_two_must_haves_capture_partial_fit(self):
        # The secondary signals share the denominator, so the two must-haves alone
        # capture 85 of 120 -> fit 71 (a Contender, not a perfect Bullseye).
        scores = _calculate_scores(
            self._signals(cash_pay_signal="yes", fertility_services="yes"), self.ICP)
        assert scores["fit_signal_score"] == 71

    def test_confirmed_negative_pulls_score_down(self):
        clean = _calculate_scores(
            self._signals(cash_pay_signal="yes", fertility_services="yes"), self.ICP)
        dinged = _calculate_scores(
            self._signals(cash_pay_signal="yes", fertility_services="yes",
                          ivf_listed="yes"), self.ICP)
        assert dinged["fit_signal_score"] < clean["fit_signal_score"]
        assert dinged["bullseye_score"] < clean["bullseye_score"]

    # --- verification cases (tier outcomes, bullseye_min 80) ------------------

    def test_case1_cash_fertility_iui_reaches_bullseye(self):
        rec = self._tier(self._signals(
            cash_pay_signal="yes", fertility_services="yes", iui_listed="yes"))
        assert rec["bullseye_score"] >= self.RUN_CONFIG["bullseye_min_score"]
        assert rec["target_tier"] == "Bullseye"

    def test_case2_no_cash_strong_fertility_cannot_reach_bullseye(self):
        rec = self._tier(self._signals(
            fertility_services="yes", iui_listed="yes",
            cycle_monitoring_listed="yes", patient_financing_visible="yes"))
        # Missing the cash-pay must-have keeps it out of Bullseye; fertility floors
        # it at a callable Contender.
        assert rec["target_tier"] != "Bullseye"
        assert rec["target_tier"] == "Contender"

    def test_case3_fertility_plus_ivf_capped_at_contender_thin(self):
        # Confirmed fertility (a primary) floors the record at Contender even though
        # the IVF penalty drags the score under 50; the IVF cap holds the ceiling.
        rec = self._tier(self._signals(fertility_services="yes", ivf_listed="yes"))
        assert rec["target_tier"] == "Contender"

    def test_confirmed_primary_floors_thin_record_to_contender(self):
        # A confirmed primary (here low-confidence cash pay) guarantees at least
        # Contender even on a sub-50 score; without a confirmed primary the record
        # would fall to Manual Review (see the ivf+rei case).
        rec = self._tier(self._signals(cash_pay_signal=("yes", "low")))
        assert rec["bullseye_score"] < 50
        assert rec["target_tier"] == "Contender"

    def test_case3_fertility_plus_ivf_capped_at_contender_high(self):
        rec = self._tier(self._signals(
            cash_pay_signal="yes", fertility_services="yes", iui_listed="yes",
            cycle_monitoring_listed="yes", patient_financing_visible="yes",
            ivf_listed="yes"))
        # A Bullseye-qualifying score, but the IVF cap holds the tier at Contender.
        assert rec["bullseye_score"] >= self.RUN_CONFIG["bullseye_min_score"]
        assert rec["target_tier"] == "Contender"

    def test_case4_ivf_and_rei_zero_fit_is_manual_review(self):
        rec = self._tier(self._signals(ivf_listed="yes", rei_on_staff="yes"))
        # Two negatives crush fit and no primary is confirmed, so the record falls
        # under the 50-pt floor with no floor guarantee -> Manual Review.
        assert rec["bullseye_score"] < 50
        assert rec["target_tier"] == "Manual Review"

    def test_case5_out_of_scope_record_excluded_at_scoring(self):
        rec = self._tier(self._signals(fertility_services="yes"),
                         specialty="Dermatology")
        assert rec["exclusion_status"] == "EXCLUDED"
        assert rec["target_tier"] == "Excluded"


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
        # A confirmed signal so the record has evidence; cash pay stays not_found
        # and uninferred, so the verification gate (not Manual Review) applies.
        rec["signals"] = ([{"signal_id": "S-other", "signal_state": "yes"}]
                          + self._signals("not_found", "not_found"))
        _apply_reinforcement(rec["signals"], self.CASH_PAY_ICP)
        assert _assign_tier(rec, 90, 75) == "Needs Verification"


# ---------------------------------------------------------------------------
# Tier assignment — verification + cap_tier
# ---------------------------------------------------------------------------

class TestTierAssignment:

    def _record_with_signals(self, signals):
        rec = _clear_record(score=90)
        # A plain confirmed signal so the record has evidence; without any "yes"
        # the record is correctly Manual Review. These tests exercise capping on
        # top of a record that was actually evaluated.
        rec["signals"] = [{"signal_id": "S-base", "signal_state": "yes"}] + list(signals)
        return rec

    def test_high_score_no_flags_is_bullseye(self):
        assert _assign_tier(self._record_with_signals([]), 90, 75) == "Bullseye"

    def test_low_score_no_flags_is_contender(self):
        assert _assign_tier(self._record_with_signals([]), 50, 75) == "Contender"

    def test_no_confirmed_evidence_is_manual_review(self):
        """A record with no 'yes' and nothing inferred is Manual Review, not a fit tier."""
        rec = _clear_record(score=90)
        rec["signals"] = [{"signal_id": "S-1", "signal_state": "not_found"},
                          {"signal_id": "S-2", "signal_state": "no"}]
        assert _assign_tier(rec, 90, 75) == "Manual Review"

    def test_empty_signals_is_manual_review(self):
        assert _assign_tier({"signals": [], "enrichment_status": "complete"},
                            90, 75) == "Manual Review"

    def test_not_enriched_roster_skips_manual_review(self):
        """An ingest-only roster row (not_enriched, no signals) is not Manual Review."""
        rec = {"signals": [], "enrichment_status": "not_enriched"}
        assert _assign_tier(rec, 0, 75) == "Contender"

    def test_low_score_with_evidence_is_manual_review(self):
        """A score below 50 is Manual Review even when some evidence exists."""
        rec = _clear_record(score=20)
        rec["signals"] = [{"signal_id": "S-1", "signal_state": "yes"}]
        assert _assign_tier(rec, 20, 75) == "Manual Review"

    def test_score_below_50_with_evidence_is_manual_review(self):
        """A score of 45 (one medium-confidence signal) is not enough for Contender."""
        rec = _clear_record(score=45)
        rec["signals"] = [{"signal_id": "S-1", "signal_state": "yes"}]
        assert _assign_tier(rec, 45, 75) == "Manual Review"

    def test_score_at_threshold_is_not_manual_review(self):
        """A score of exactly 50 is not Manual Review — threshold is exclusive."""
        rec = _clear_record(score=50)
        rec["signals"] = [{"signal_id": "S-1", "signal_state": "yes"}]
        assert _assign_tier(rec, 50, 75) == "Contender"

    def test_unconfirmed_required_signal_caps_bullseye_at_needs_verification(self):
        signals = [{"signal_id": "S-1", "signal_state": "not_found",
                    "verification_required": True, "cap_tier": ""}]
        assert _assign_tier(self._record_with_signals(signals), 90, 75) == "Needs Verification"

    def test_confirmed_required_signal_does_not_cap(self):
        signals = [{"signal_id": "S-1", "signal_state": "yes",
                    "verification_required": True, "cap_tier": ""}]
        assert _assign_tier(self._record_with_signals(signals), 90, 75) == "Bullseye"

    def test_cap_tier_yes_caps_at_contender(self):
        signals = [{"signal_id": "S-hosp", "signal_state": "yes",
                    "verification_required": False, "cap_tier": "Contender"}]
        assert _assign_tier(self._record_with_signals(signals), 90, 75) == "Contender"

    def test_cap_tier_beats_verification(self):
        """A hospital-affiliation Contender cap wins over a Needs Verification flag."""
        signals = [
            {"signal_id": "S-hosp", "signal_state": "yes",
             "verification_required": False, "cap_tier": "Contender"},
            {"signal_id": "S-cash", "signal_state": "not_found",
             "verification_required": True, "cap_tier": ""},
        ]
        assert _assign_tier(self._record_with_signals(signals), 90, 75) == "Contender"

    def test_verification_does_not_lift_contender(self):
        """A not_found required signal never raises a low-score Contender up a rung."""
        signals = [{"signal_id": "S-1", "signal_state": "not_found",
                    "verification_required": True, "cap_tier": ""}]
        assert _assign_tier(self._record_with_signals(signals), 50, 75) == "Contender"

    def test_must_have_confirmed_no_caps_at_contender(self):
        """A required_for_bullseye signal confirmed 'no' caps at Contender even at top score."""
        signals = [{"signal_id": "S-cash", "signal_state": "no",
                    "required_for_bullseye": True, "cap_tier": ""}]
        assert _assign_tier(self._record_with_signals(signals), 95, 90) == "Contender"

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

    def test_all_required_confirmed_below_score_threshold_is_contender(self):
        """All must-haves confirmed but score below bullseye_min → Contender, not Bullseye.

        Bullseye requires BOTH the score threshold and confirmed must-haves; a
        single confirmed must-have must never promote a weak-fit record.
        """
        signals = [
            {"signal_id": "S-tms", "signal_state": "yes",
             "required_for_bullseye": True, "cap_tier": ""},
            {"signal_id": "S-cash", "signal_state": "yes",
             "required_for_bullseye": True, "cap_tier": ""},
        ]
        rec = self._record_with_signals(signals)
        assert _assign_tier(rec, 73, 90) == "Contender"
        assert "below the Bullseye threshold" in rec["tier_cap_reason"]

    def test_partial_required_confirmed_uses_score_threshold(self):
        """Some must-haves not confirmed → score threshold still applies (not all confirmed)."""
        signals = [
            {"signal_id": "S-tms", "signal_state": "yes",
             "required_for_bullseye": True, "cap_tier": ""},
            {"signal_id": "S-cash", "signal_state": "not_found",
             "required_for_bullseye": True, "cap_tier": ""},
        ]
        # score 73 < bullseye_min 90, not all required confirmed → Contender;
        # the NV cap on cash can only pull DOWN from Bullseye, not further below Contender
        assert _assign_tier(self._record_with_signals(signals), 73, 90) == "Contender"

    def test_bullseye_has_empty_cap_reason(self):
        """A clean Bullseye record carries no tier_cap_reason."""
        rec = self._record_with_signals([])
        _assign_tier(rec, 90, 75)
        assert rec["tier_cap_reason"] == ""

    def test_must_have_no_sets_cap_reason(self):
        """A confirmed-absent must-have names itself in tier_cap_reason."""
        signals = [{"signal_id": "S-tms", "signal_label": "TMS services offered",
                    "signal_state": "no", "required_for_bullseye": True, "cap_tier": ""}]
        rec = self._record_with_signals(signals)
        assert _assign_tier(rec, 95, 90) == "Contender"
        assert "TMS services offered" in rec["tier_cap_reason"]
        assert "Contender" in rec["tier_cap_reason"]

    def test_must_have_not_found_sets_cap_reason(self):
        """A not-found must-have explains the Needs Verification cap."""
        signals = [{"signal_id": "S-cash", "signal_label": "Cash pay accepted",
                    "signal_state": "not_found", "required_for_bullseye": True, "cap_tier": ""}]
        rec = self._record_with_signals(signals)
        assert _assign_tier(rec, 95, 90) == "Needs Verification"
        assert "Cash pay accepted" in rec["tier_cap_reason"]

    def test_thin_crawl_sets_cap_reason(self):
        """A limited-confidence crawl explains the Manual Review tier."""
        rec = self._record_with_signals([])
        rec["source_confidence"] = "limited"
        assert _assign_tier(rec, 90, 75) == "Manual Review"
        assert "crawl" in rec["tier_cap_reason"].lower()

    def test_no_evidence_sets_cap_reason(self):
        """A record with no confirmed signal explains its Manual Review tier."""
        rec = _clear_record(score=90)
        rec["signals"] = [{"signal_id": "S-1", "signal_state": "not_found"}]
        assert _assign_tier(rec, 90, 75) == "Manual Review"
        assert "No confirmed signals" in rec["tier_cap_reason"]

    def test_apply_exclusions_sets_needs_verification_tier(self):
        """End to end: a CLEAR record with evidence + an unconfirmed required signal is NV."""
        rec = _clear_record(score=90, specialty="OBGYN")
        rec["signals"] = [{"signal_id": "S-base", "signal_state": "yes"},
                          {"signal_id": "S-1", "signal_state": "not_found",
                           "verification_required": True, "cap_tier": ""}]
        result = apply_exclusions(rec, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"
        assert result["target_tier"] == "Needs Verification"

    # --- required_for_contender (qualifier gate) ---
    # Stricter than required_for_bullseye: when the flagged signal is not confirmed
    # "yes" and not inferred, the record is held in Manual Review regardless of
    # score or any other confirmed signal — removed from the call queue entirely.

    def test_required_for_contender_not_found_is_manual_review(self):
        """A not_found required_for_contender signal forces Manual Review even with
        other 'yes' signals and a high score."""
        signals = [{"signal_id": "S-cash", "signal_label": "Cash pay / self-pay",
                    "signal_state": "not_found", "required_for_contender": True}]
        rec = self._record_with_signals(signals)  # carries a confirmed S-base + high score
        assert _assign_tier(rec, 95, 90) == "Manual Review"
        assert "required to qualify" in rec["tier_cap_reason"]

    def test_required_for_contender_confirmed_no_is_manual_review(self):
        """A confirmed-'no' required_for_contender signal also forces Manual Review."""
        signals = [{"signal_id": "S-cash", "signal_label": "Cash pay / self-pay",
                    "signal_state": "no", "required_for_contender": True}]
        assert _assign_tier(self._record_with_signals(signals), 95, 90) == "Manual Review"

    def test_required_for_contender_inferred_is_not_manual_review(self):
        """An inferred (reinforced) required_for_contender signal suppresses the gate
        and the record tiers normally."""
        icp = [
            {"signal_id": "S-cash", "signal_label": "Cash pay / self-pay",
             "prompt_instruction": "?", "positive_weight": 20,
             "required_for_contender": True},
            {"signal_id": "S-elective", "signal_label": "Elective services",
             "prompt_instruction": "?", "positive_weight": 0,
             "reinforces": "S-cash"},
        ]
        signals = [
            {"signal_id": "S-cash", "signal_state": "not_found",
             "required_for_contender": True, "state_inferred": False},
            {"signal_id": "S-elective", "signal_state": "yes",
             "state_inferred": False},
        ]
        _apply_reinforcement(signals, icp)  # elective 'yes' infers cash pay
        rec = _clear_record(score=90)
        rec["signals"] = signals
        assert _assign_tier(rec, 90, 75) == "Bullseye"

    def test_required_for_contender_confirmed_yes_tiers_normally(self):
        """A confirmed 'yes' required_for_contender signal does not gate at all."""
        signals = [{"signal_id": "S-cash", "signal_state": "yes",
                    "required_for_contender": True}]
        assert _assign_tier(self._record_with_signals(signals), 90, 75) == "Bullseye"

    def test_required_for_contender_absent_flag_is_regression_safe(self):
        """A profile that omits required_for_contender is unaffected (default off)."""
        signals = [{"signal_id": "S-1", "signal_state": "not_found"}]
        # No required_for_contender anywhere → the gate never fires; normal tiering.
        rec = self._record_with_signals(signals)
        assert _assign_tier(rec, 90, 75) == "Bullseye"


# ---------------------------------------------------------------------------
# floor_tier ICP signal flag
# ---------------------------------------------------------------------------

class TestFloorTier:
    """floor_tier guarantees a minimum tier when a qualifying signal is confirmed."""

    def _record(self, signals, score=30, status="complete"):
        return {
            "enrichment_status": status,
            "bullseye_score": score,
            "signals": signals,
        }

    def test_floor_tier_overrides_low_score_manual_review(self):
        """Confirmed signal with floor_tier: Contender prevents Manual Review from low score."""
        signals = [
            {"signal_id": "S-cash", "signal_state": "yes",
             "positive_weight": 20, "floor_tier": "Contender"},
        ]
        result = _assign_tier(self._record(signals, score=30), 30, 75)
        assert result == "Contender"

    def test_floor_tier_does_not_apply_when_signal_not_confirmed(self):
        """floor_tier has no effect when the signal is not_found — Manual Review still fires."""
        signals = [
            {"signal_id": "S-cash", "signal_state": "not_found",
             "positive_weight": 20, "floor_tier": "Contender"},
        ]
        result = _assign_tier(self._record(signals, score=20), 20, 75)
        assert result == "Manual Review"

    def test_floor_tier_does_not_prevent_higher_tier(self):
        """A high score above bullseye_min is not capped down to the floor."""
        signals = [
            {"signal_id": "S-cash", "signal_state": "yes",
             "positive_weight": 20, "floor_tier": "Contender"},
        ]
        result = _assign_tier(self._record(signals, score=90), 90, 75)
        assert result == "Bullseye"

    def test_floor_tier_still_subject_to_cap_tier(self):
        """A cap_tier from another confirmed signal can pull the floor-lifted tier back down."""
        signals = [
            {"signal_id": "S-cash", "signal_state": "yes",
             "positive_weight": 20, "floor_tier": "Contender"},
            {"signal_id": "S-hosp", "signal_state": "yes",
             "positive_weight": -30, "cap_tier": "Needs Verification"},
        ]
        # Score-based start = Contender (30 < 75 bullseye_min), floor = Contender.
        # cap_tier: Needs Verification (rank 2) > Contender (rank 1), so cap doesn't
        # pull down — Contender wins.
        result = _assign_tier(self._record(signals, score=30), 30, 75)
        assert result == "Contender"

    def test_floor_tier_cap_pulls_down_when_lower_than_floor(self):
        """cap_tier: Contender pulls a floor-lifted Bullseye down to Contender."""
        signals = [
            {"signal_id": "S-qual", "signal_state": "yes",
             "positive_weight": 20, "floor_tier": "Needs Verification"},
            {"signal_id": "S-hosp", "signal_state": "yes",
             "positive_weight": -30, "cap_tier": "Contender"},
        ]
        # Floor lifts to NV (rank 2); cap pulls back to Contender (rank 1).
        result = _assign_tier(self._record(signals, score=90), 90, 75)
        assert result == "Contender"


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

    def test_womens_care_in_name_maps_to_obgyn(self):
        # "women's care" (not "women's health") should not slip through as Unknown.
        assert infer_specialty("", "Beyond Women's Care") == "OBGYN"
        assert infer_specialty("", "Valley Women's Care Center") == "OBGYN"

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

    def test_specialty_inflection_psychiatrist_matches_psychiatry(self):
        """'Psychiatrist' matches target 'Psychiatry' via 7-char prefix."""
        config = {**BASE_RUN_CONFIG, "target_specialty": "Psychiatry, primary care"}
        record = _clear_record(specialty="Psychiatrist")
        result = apply_exclusions(record, config)
        assert result["exclusion_status"] == "CLEAR"

    def test_specialty_inflection_cardiologist_matches_cardiology(self):
        """'Cardiologist' matches target 'Cardiology' via prefix."""
        config = {**BASE_RUN_CONFIG, "target_specialty": "Cardiology"}
        record = _clear_record(specialty="Cardiologist")
        result = apply_exclusions(record, config)
        assert result["exclusion_status"] == "CLEAR"

    def test_specialty_unrelated_name_still_excluded(self):
        """'Mental Health Clinic' does not match 'Psychiatry' — requires operator config."""
        config = {**BASE_RUN_CONFIG, "target_specialty": "Psychiatry"}
        record = _clear_record(specialty="Mental Health Clinic")
        result = apply_exclusions(record, config)
        assert result["exclusion_status"] == "EXCLUDED"


class TestZipLookup:
    """Offline ZIP -> (city, state) resolution from the bundled dataset."""

    def _lookup(self, z):
        from ingestion import zip_lookup
        return zip_lookup.infer_city_state(z)

    def test_known_zip_resolves(self):
        assert self._lookup("75201") == ("Dallas", "TX")

    def test_zip_plus_four_resolves(self):
        assert self._lookup("75201-1234") == ("Dallas", "TX")

    def test_unknown_and_blank_zip_return_empty(self):
        assert self._lookup("00000") == ("", "")
        assert self._lookup("") == ("", "")
        assert self._lookup("abc") == ("", "")

    def test_ingest_fills_city_state_from_zip(self, tmp_path):
        """A row with only a ZIP gets city/state filled from the offline lookup."""
        from ingestion.outscraper_adapter import load_outscraper_csv
        csv_path = tmp_path / "in.csv"
        csv_path.write_text(
            "name,postal_code,site\nDallas Clinic,75201,https://x.com\n",
            encoding="utf-8",
        )
        records = load_outscraper_csv(str(csv_path))
        assert records[0]["address_city"] == "Dallas"
        assert records[0]["address_state"] == "TX"

    def test_google_place_id_preserved_through_ingest(self, tmp_path):
        """place_id column from Outscraper flows into google_place_id on the record."""
        from ingestion.outscraper_adapter import load_outscraper_csv
        csv_path = tmp_path / "in.csv"
        csv_path.write_text(
            "name,postal_code,site,place_id\n"
            "Atlanta Clinic,30301,https://x.com,ChIJtest123\n",
            encoding="utf-8",
        )
        records = load_outscraper_csv(str(csv_path))
        assert records[0]["google_place_id"] == "ChIJtest123"

    def test_google_place_id_defaults_empty_string_when_absent(self, tmp_path):
        """When place_id column is not in the CSV, google_place_id defaults to ''."""
        from ingestion.outscraper_adapter import load_outscraper_csv
        csv_path = tmp_path / "in.csv"
        csv_path.write_text(
            "name,postal_code,site\nAtlanta Clinic,30301,https://x.com\n",
            encoding="utf-8",
        )
        records = load_outscraper_csv(str(csv_path))
        assert records[0].get("google_place_id") == ""


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


class TestExcludeIfYes:
    """A signal flagged exclude_if_yes is an immediate disqualifier when 'yes'."""

    def _record_with_signal(self, state):
        record = _clear_record(score=95)
        record["signals"] = [{
            "signal_id": "S-TELE",
            "signal_label": "Telehealth-only practice",
            "signal_state": state,
            "exclude_if_yes": True,
        }]
        return record

    def test_yes_excludes_record(self):
        record = self._record_with_signal("yes")
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "EXCLUDED"
        assert result["target_tier"] == "Excluded"
        assert "Telehealth-only practice" in (result["exclusion_reason"] or "")

    def test_not_found_does_not_exclude(self):
        record = self._record_with_signal("not_found")
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"
        assert result["target_tier"] != "Excluded"

    def test_no_does_not_exclude(self):
        record = self._record_with_signal("no")
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"

    def test_flag_absent_yes_does_not_exclude(self):
        record = _clear_record(score=95)
        record["signals"] = [{
            "signal_id": "S-PLAIN",
            "signal_label": "Some positive signal",
            "signal_state": "yes",
        }]
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"


class TestConfidenceBand:
    """confidence_band is derived from confidence_score, never recomputed."""

    def test_band_boundaries(self):
        from enrichment.constants import (
            confidence_band_for_score,
            HIGH_CONFIDENCE_THRESHOLD,
            LOW_CONFIDENCE_THRESHOLD,
        )
        assert confidence_band_for_score(HIGH_CONFIDENCE_THRESHOLD) == "High"
        assert confidence_band_for_score(90) == "High"
        assert confidence_band_for_score(LOW_CONFIDENCE_THRESHOLD) == "Moderate"
        assert confidence_band_for_score(64) == "Moderate"
        assert confidence_band_for_score(LOW_CONFIDENCE_THRESHOLD - 1) == "Low"
        assert confidence_band_for_score(40) == "Low"   # pure low-confidence yes
        assert confidence_band_for_score(30) == "Low"   # no-signal default

    def test_finalize_sets_band_from_score(self):
        record = _clear_record(score=90)
        record["confidence_score"] = 90
        record["signals"] = []
        result = validate_and_finalize(record)
        assert result["confidence_band"] == "High"
        # numeric score is preserved in the internal record
        assert result["confidence_score"] == 90

    def test_finalize_low_score_band_low(self):
        record = _clear_record(score=20)
        record["confidence_score"] = 30
        record["signals"] = []
        result = validate_and_finalize(record)
        assert result["confidence_band"] == "Low"


# ---------------------------------------------------------------------------
# CLEAR record tier invariant
# ---------------------------------------------------------------------------

class TestClearRecordTierInvariant:

    _YES = [{"signal_id": "S-base", "signal_state": "yes"}]

    def test_clear_high_score_is_bullseye(self):
        record = _clear_record(score=80)
        record["signals"] = list(self._YES)
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"
        assert result["target_tier"] == "Bullseye"

    def test_clear_low_score_is_contender_not_excluded(self):
        """CLEAR records with score between threshold and bullseye_min are Contender, never Excluded."""
        record = _clear_record(score=55)
        record["signals"] = list(self._YES)
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"
        assert result["target_tier"] == "Contender"
        assert result["target_tier"] != "Excluded"

    def test_clear_zero_evidence_is_manual_review(self):
        """A CLEAR record with no confirmed signals is Manual Review (still not Excluded)."""
        record = _clear_record(score=0)
        record["signals"] = []
        result = apply_exclusions(record, BASE_RUN_CONFIG)
        assert result["exclusion_status"] == "CLEAR"
        assert result["target_tier"] == "Manual Review"


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

    def test_clear_excluded_tier_repaired_to_contender(self):
        """CLEAR + Excluded tier → repaired to Contender (score 40 < 75)."""
        record = self._base_record()
        record["exclusion_status"] = "CLEAR"
        record["target_tier"] = "Excluded"
        record["bullseye_score"] = 40
        result = validate_and_finalize(record)
        assert result["target_tier"] == "Contender"
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
        """EXCLUDED + Contender tier → repaired to Excluded."""
        record = self._base_record()
        record["exclusion_status"] = "EXCLUDED"
        record["target_tier"] = "Contender"
        record["exclusion_reason"] = "hospital_owned"
        result = validate_and_finalize(record)
        assert result["target_tier"] == "Excluded"
        assert "Invariant violation" in result["internal_notes"]

    def test_valid_consistent_record_unchanged(self):
        """Valid, consistent records are not modified."""
        record = self._base_record()
        record["exclusion_status"] = "CLEAR"
        record["target_tier"] = "Contender"
        result = validate_and_finalize(record)
        assert result["target_tier"] == "Contender"
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

    def test_clear_manual_review_high_score_not_promoted(self):
        """Fail-closed path: when the exclusion check raises, the record is routed
        to Manual Review with exclusion_status CLEAR. Manual Review is a valid tier
        consistent with CLEAR, so validation must preserve it and never promote it
        to Bullseye by bare score (the old fail-open bug left the tier unset, so the
        scorer inferred Bullseye from the score and bypassed every exclusion gate)."""
        record = self._base_record()
        record["exclusion_status"] = "CLEAR"
        record["target_tier"] = "Manual Review"
        record["bullseye_score"] = 95
        result = validate_and_finalize(record)
        assert result["target_tier"] == "Manual Review"

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
         "prompt_instruction": "?", "positive_weight": -30, "cap_tier": "Contender"},
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
            self._sig("S-hosp", "yes", "Hospital affiliated", -30, cap_tier="Contender"),
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
    """Blocked (limited/failed) sites go to Manual Review; partial/complete allow normal tier."""

    _ICP = [{"signal_id": "S-1", "signal_label": "A", "prompt_instruction": "?",
             "positive_weight": 20}]

    def _record(self, source_confidence):
        rec = _clear_record(score=95)
        rec["source_confidence"] = source_confidence
        # A confirmed signal so the record has evidence — isolates the
        # source-confidence gate from the no-evidence Manual Review rule.
        rec["signals"] = [{"signal_id": "S-1", "signal_state": "yes"}]
        return rec

    def test_limited_confidence_becomes_manual_review(self):
        assert _assign_tier(self._record("limited"), 95, 75) == "Manual Review"

    def test_failed_confidence_becomes_manual_review(self):
        assert _assign_tier(self._record("failed"), 95, 75) == "Manual Review"

    def test_complete_confidence_allows_bullseye(self):
        assert _assign_tier(self._record("complete"), 95, 75) == "Bullseye"

    def test_partial_confidence_allows_bullseye(self):
        assert _assign_tier(self._record("partial"), 95, 75) == "Bullseye"

    def test_missing_source_confidence_allows_bullseye(self):
        # Records without source_confidence set are not gated.
        rec = _clear_record(score=95)
        rec["signals"] = [{"signal_id": "S-1", "signal_state": "yes"}]
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
        out = _finalize_ingest_only([self._roster_record()])
        rec = out[0]
        assert rec["enrichment_status"] == "not_enriched"
        assert rec["exclusion_status"] == "CLEAR"
        assert rec["bullseye_score"] == 0
        assert rec["signals"] == []

    def test_no_exclusions_at_import(self):
        # Wrong geography/specialty must NOT exclude at import — exclusions are
        # deferred to enrichment time so the operator sees the full roster.
        out = _finalize_ingest_only([self._roster_record(state="CA")])
        rec = out[0]
        assert rec["exclusion_status"] == "CLEAR"
        assert rec["target_tier"] != "Excluded"
        assert rec["enrichment_status"] == "not_enriched"

    def test_wrong_specialty_not_excluded_at_import(self):
        out = _finalize_ingest_only([self._roster_record(specialty="Cardiology")])
        rec = out[0]
        assert rec["exclusion_status"] == "CLEAR"
        assert rec["enrichment_status"] == "not_enriched"

    def test_output_schema_is_complete(self):
        # validate_and_finalize must leave a fully-shaped record the UI can render.
        out = _finalize_ingest_only([self._roster_record()])
        rec = out[0]
        assert isinstance(rec.get("call_brief"), dict)
        assert isinstance(rec.get("sales_angle"), list)
        assert rec.get("qc_status") == "pending"

    def test_not_enriched_status_survives_validation(self):
        rec = {
            "practice_name": "X", "exclusion_status": "CLEAR",
            "target_tier": "Contender", "enrichment_status": "not_enriched",
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

    def _web_mod(self):
        import importlib, sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "extraction"))
        return importlib.import_module("web_extractor")

    def test_requests_path_shares_detector(self):
        """web_extractor exposes the same challenge detector the browser path uses."""
        mod = self._web_mod()
        assert mod.looks_like_challenge("<title>Just a moment...</title>") is True
        assert mod.looks_like_challenge("<body>Real clinic content here.</body>") is False

    def test_requests_crawl_treats_challenge_as_blocked(self, monkeypatch):
        """A challenge page (HTTP 200 with body) is a blocked crawl, not content."""
        mod = self._web_mod()
        challenge_html = "<html><title>Just a moment...</title><body>Checking your browser</body></html>"
        monkeypatch.setattr(
            mod, "_fetch_html",
            lambda url, timeout=15, retries=3: (challenge_html, url, ""),
        )
        result = mod.extract_practice_text("https://blocked.example.com")
        assert result.success is False
        assert result.context_text == ""
        assert "challenge" in result.error.lower()


class TestBrowserChallengeKnobs:
    """Env-driven knobs for the patient browser challenge handling."""

    def _mod(self):
        import importlib, sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "extraction"))
        return importlib.import_module("playwright_extractor")

    def test_headful_requested_reads_env(self, monkeypatch):
        mod = self._mod()
        monkeypatch.delenv("PIPELINE_BROWSER_HEADFUL", raising=False)
        assert mod._headful_requested() is False
        monkeypatch.setenv("PIPELINE_BROWSER_HEADFUL", "1")
        assert mod._headful_requested() is True
        monkeypatch.setenv("PIPELINE_BROWSER_HEADFUL", "true")
        assert mod._headful_requested() is True

    def test_challenge_budget_default_and_override(self, monkeypatch):
        mod = self._mod()
        monkeypatch.delenv("PIPELINE_BROWSER_CHALLENGE_WAIT_MS", raising=False)
        assert mod._challenge_wait_budget_ms() == 25000
        monkeypatch.setenv("PIPELINE_BROWSER_CHALLENGE_WAIT_MS", "40000")
        assert mod._challenge_wait_budget_ms() == 40000
        # Clamps to a sane floor and ignores garbage.
        monkeypatch.setenv("PIPELINE_BROWSER_CHALLENGE_WAIT_MS", "100")
        assert mod._challenge_wait_budget_ms() == 5000
        monkeypatch.setenv("PIPELINE_BROWSER_CHALLENGE_WAIT_MS", "abc")
        assert mod._challenge_wait_budget_ms() == 25000


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
        _load_manual_content([rec], [path])
        assert "Ketamine" in rec["_context_text"]
        assert "var x=1" not in rec["_context_text"]   # script stripped
        assert rec["_pages_crawled"] == ["[Manual content] page.html"]
        assert rec["source_confidence"] == "partial"
        assert rec["_url_valid"] is True

    def test_plain_text_used_verbatim(self, tmp_path):
        text = "TMS therapy and ketamine infusions offered. Cash pay accepted. " * 3
        path = self._write(tmp_path, "notes.txt", text)
        rec = {"website_url": "https://x.com"}
        _load_manual_content([rec], [path])
        assert "ketamine infusions" in rec["_context_text"]
        assert rec["source_confidence"] == "partial"

    def test_thin_content_left_short_for_downstream_gate(self, tmp_path):
        # Under MIN_CONTEXT_CHARS: helper does not pad; the empty-context gate
        # in extract_signals will set signals not_found downstream.
        path = self._write(tmp_path, "tiny.txt", "too short")
        rec = {"website_url": "https://x.com"}
        _load_manual_content([rec], [path])
        assert rec["_context_text"] == "too short"
        assert len(rec["_context_text"]) < 150

    def test_multiple_pages_joined_with_separator(self, tmp_path):
        p1 = self._write(tmp_path, "home.html",
                         "<html><body><h1>Dallas Clinic</h1>"
                         "<p>Ketamine infusions offered.</p></body></html>")
        p2 = self._write(tmp_path, "about.txt",
                         "Solo practice run by Dr. Smith for fifteen years.")
        rec = {"website_url": "https://x.com"}
        _load_manual_content([rec], [p1, p2])
        # Both pages present, joined with the crawler's separator.
        assert "Ketamine infusions" in rec["_context_text"]
        assert "Solo practice" in rec["_context_text"]
        assert "\n\n---\n\n" in rec["_context_text"]
        assert rec["_pages_crawled"] == ["[Manual content] home.html",
                                         "[Manual content] about.txt"]
        assert rec["source_confidence"] == "partial"


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


class TestProviderExtraction:
    """Provider name extraction and key_contact formatting."""

    def test_valid_providers_parsed(self):
        raw = [{"name": "Dr. Jane Smith", "title": "MD, OBGYN"}, {"name": "Sarah Lee", "title": "NP"}]
        providers, names = _parse_providers(raw)
        assert len(providers) == 2
        assert providers[0] == {"name": "Dr. Jane Smith", "title": "MD, OBGYN"}
        assert names == ["Dr. Jane Smith, MD, OBGYN", "Sarah Lee, NP"]

    def test_missing_title_omits_comma(self):
        raw = [{"name": "Dr. Marcus Patel"}]
        providers, names = _parse_providers(raw)
        assert names == ["Dr. Marcus Patel"]

    def test_empty_name_skipped(self):
        raw = [{"name": "", "title": "MD"}, {"name": "Dr. Chen", "title": "DO"}]
        providers, names = _parse_providers(raw)
        assert len(providers) == 1
        assert providers[0]["name"] == "Dr. Chen"

    def test_non_list_returns_empty(self):
        providers, names = _parse_providers(None)
        assert providers == []
        assert names == []

    def test_caps_at_eight_entries(self):
        raw = [{"name": f"Dr. Provider {i}", "title": "MD"} for i in range(12)]
        providers, _ = _parse_providers(raw)
        assert len(providers) == 8

    def test_primary_contact_valid_pick(self):
        providers = [{"name": "Dr. Jane Smith", "title": "MD"}, {"name": "Sarah Lee", "title": "NP"}]
        raw = {"name": "Dr. Jane Smith", "title": "MD", "reason": "performs in-office procedures"}
        result = _parse_primary_contact(raw, providers)
        assert result["name"] == "Dr. Jane Smith"
        assert result["reason"] == "performs in-office procedures"

    def test_primary_contact_falls_back_to_first_provider_on_bad_pick(self):
        providers = [{"name": "Dr. Chen", "title": "DO"}, {"name": "Sarah Lee", "title": "NP"}]
        raw = {"name": "Dr. Nobody", "title": "MD", "reason": "not in list"}
        result = _parse_primary_contact(raw, providers)
        assert result["name"] == "Dr. Chen"
        assert result["reason"] == ""

    def test_primary_contact_none_when_no_providers(self):
        assert _parse_primary_contact(None, []) is None
        assert _parse_primary_contact({"name": "Dr. X"}, []) is None

    def test_primary_contact_null_raw_falls_back_to_first(self):
        providers = [{"name": "Dr. Patel", "title": "MD"}]
        result = _parse_primary_contact(None, providers)
        assert result["name"] == "Dr. Patel"

    def test_key_contact_with_reason(self):
        primary = {"name": "Dr. Jane Smith", "title": "MD", "reason": "lead OBGYN"}
        assert _format_key_contact(primary) == "Ask for Dr. Jane Smith — lead OBGYN"

    def test_key_contact_without_reason(self):
        primary = {"name": "Dr. Patel", "title": "DO", "reason": ""}
        assert _format_key_contact(primary) == "Ask for Dr. Patel"

    def test_key_contact_none(self):
        assert _format_key_contact(None) == ""

    def test_key_contact_md_without_dr_prefix_gets_dr(self):
        """LLM returns name without 'Dr.' but title says MD — must prepend Dr."""
        primary = {"name": "Theodore Fellenbaum", "title": "MD", "reason": "sole physician and practice owner"}
        result = _format_key_contact(primary)
        assert result == "Ask for Dr. Theodore Fellenbaum — sole physician and practice owner"

    def test_key_contact_do_without_dr_prefix_gets_dr(self):
        primary = {"name": "Maria Santos", "title": "DO", "reason": "lead physician"}
        assert _format_key_contact(primary) == "Ask for Dr. Maria Santos — lead physician"

    def test_key_contact_already_has_dr_not_doubled(self):
        primary = {"name": "Dr. Jane Smith", "title": "MD", "reason": "lead OBGYN"}
        assert _format_key_contact(primary) == "Ask for Dr. Jane Smith — lead OBGYN"

    def test_key_contact_np_does_not_get_dr(self):
        primary = {"name": "Sarah Lee", "title": "NP", "reason": "handles scheduling"}
        assert _format_key_contact(primary) == "Ask for Sarah Lee — handles scheduling"

    def test_key_contact_no_title_no_dr(self):
        primary = {"name": "Alex Johnson", "title": "", "reason": "office manager"}
        assert _format_key_contact(primary) == "Ask for Alex Johnson — office manager"

    def test_apply_physician_prefix_md(self):
        assert _apply_physician_prefix("Theodore Fellenbaum", "MD") == "Dr. Theodore Fellenbaum"

    def test_apply_physician_prefix_do(self):
        assert _apply_physician_prefix("Maria Santos", "DO") == "Dr. Maria Santos"

    def test_apply_physician_prefix_mbbs(self):
        assert _apply_physician_prefix("Priya Kapoor", "MBBS") == "Dr. Priya Kapoor"

    def test_apply_physician_prefix_already_dr(self):
        assert _apply_physician_prefix("Dr. Jane Smith", "MD") == "Dr. Jane Smith"

    def test_apply_physician_prefix_non_physician_title(self):
        assert _apply_physician_prefix("Sarah Lee", "NP") == "Sarah Lee"

    def test_apply_physician_prefix_empty_name(self):
        assert _apply_physician_prefix("", "MD") == ""


class TestContactStrategyPrompt:
    """ICP-configured contact strategy injection into the system prompt."""

    _SIGNALS = [{"signal_id": "S-01", "signal_label": "Test signal",
                 "prompt_instruction": "Is it there?", "positive_weight": 10}]

    def test_default_strategy_when_unset(self):
        prompt = _build_system_prompt(self._SIGNALS)
        assert DEFAULT_CONTACT_STRATEGY in prompt
        assert "{contact_strategy}" not in prompt

    def test_custom_strategy_replaces_default(self):
        strategy = "Prefer the treatment coordinator or lead hygienist when named."
        prompt = _build_system_prompt(self._SIGNALS, contact_strategy=strategy)
        assert strategy in prompt
        assert DEFAULT_CONTACT_STRATEGY not in prompt

    def test_whitespace_strategy_falls_back_to_default(self):
        prompt = _build_system_prompt(self._SIGNALS, contact_strategy="   ")
        assert DEFAULT_CONTACT_STRATEGY in prompt


# ---------------------------------------------------------------------------
# NPI Lookup — pure-function tests (no HTTP)
# ---------------------------------------------------------------------------

class TestNormalizePhone:
    def test_standard_10_digit(self):
        assert _normalize_phone("(214) 555-1234") == "2145551234"

    def test_strips_country_code(self):
        assert _normalize_phone("+1-214-555-1234") == "2145551234"

    def test_dashes_and_dots(self):
        assert _normalize_phone("214.555.1234") == "2145551234"

    def test_too_short_returns_empty(self):
        assert _normalize_phone("555-1234") == ""

    def test_empty_returns_empty(self):
        assert _normalize_phone("") == ""

    def test_none_returns_empty(self):
        assert _normalize_phone(None) == ""


class TestNamesAgree:
    def test_exact_match(self):
        assert _names_agree("Dallas Women's Health", "Dallas Women's Health")

    def test_common_tokens_agree(self):
        assert _names_agree("Dallas OBGYN Associates", "Dallas OBGYN Clinic")

    def test_partial_name_overlap_agrees(self):
        assert _names_agree("Dallas OB/GYN Associates", "Dallas OB/GYN Clinic") is True

    def test_completely_different_names_disagree(self):
        assert _names_agree("Northside Family Medicine", "Dallas Women's Clinic") is False

    def test_empty_name_disagrees(self):
        assert _names_agree("", "Dallas Women's Health") is False
        assert _names_agree("Dallas Women's Health", "") is False

    def test_noise_only_names_disagree(self):
        # Both names reduce to empty token sets after noise stripping
        assert _names_agree("The Health Group LLC", "The Care Center PC") is False


class TestExtractTaxonomyCodes:
    def test_extracts_single_code(self):
        result = {"taxonomies": [{"code": "207VX0000X", "desc": "Obstetrics & Gynecology"}]}
        assert _extract_taxonomy_codes(result) == ["207VX0000X"]

    def test_extracts_multiple_codes(self):
        result = {
            "taxonomies": [
                {"code": "207VX0000X"},
                {"code": "207VE0102X"},
            ]
        }
        codes = _extract_taxonomy_codes(result)
        assert "207VX0000X" in codes
        assert "207VE0102X" in codes

    def test_empty_taxonomies_returns_empty(self):
        assert _extract_taxonomy_codes({"taxonomies": []}) == []

    def test_no_taxonomies_key_returns_empty(self):
        assert _extract_taxonomy_codes({}) == []

    def test_taxonomy_code_detected(self):
        result = {"taxonomies": [{"code": "207VE0102X"}]}
        codes = _extract_taxonomy_codes(result)
        assert "207VE0102X" in codes


# ---------------------------------------------------------------------------
# Taxonomy Structural Exclusion Gate
# ---------------------------------------------------------------------------

class TestTaxonomyStructuralGate:
    """Verify that _npi_taxonomy_exclusions drives the structural exclusion gate
    via taxonomy_exclusion_rules config — generic, not REI-specific."""

    def _make_run_config(self, active_rules=None):
        return {
            "target_specialty": "OBGYN",
            "target_geography": ["TX"],
            "active_exclusion_rules": active_rules or [],
        }

    def _make_record(self, taxonomy_exclusions=None):
        return {
            "id": "T-TEST",
            "practice_name": "Test OB/GYN",
            "specialty": "OBGYN",
            "address_state": "TX",
            "address_zip": "75001",
            "_npi_taxonomy_exclusions": taxonomy_exclusions or [],
        }

    def test_taxonomy_exclusion_fires_when_rule_active(self):
        from enrichment.exclusion_checker import check_structural_exclusions
        record = self._make_record(taxonomy_exclusions=["rei_on_staff"])
        run_config = self._make_run_config(active_rules=["rei_on_staff"])
        triggered, rationale = check_structural_exclusions(record, run_config)
        assert "rei_on_staff" in triggered
        assert any("taxonomy" in r.lower() for r in rationale)

    def test_taxonomy_exclusion_suppressed_when_rule_not_active(self):
        from enrichment.exclusion_checker import check_structural_exclusions
        record = self._make_record(taxonomy_exclusions=["rei_on_staff"])
        run_config = self._make_run_config(active_rules=[])  # rei_on_staff not in active rules
        triggered, _ = check_structural_exclusions(record, run_config)
        assert "rei_on_staff" not in triggered

    def test_empty_taxonomy_exclusions_does_not_fire(self):
        from enrichment.exclusion_checker import check_structural_exclusions
        record = self._make_record(taxonomy_exclusions=[])
        run_config = self._make_run_config(active_rules=["rei_on_staff"])
        triggered, _ = check_structural_exclusions(record, run_config)
        assert "rei_on_staff" not in triggered

    def test_missing_taxonomy_exclusions_field_does_not_fire(self):
        from enrichment.exclusion_checker import check_structural_exclusions
        record = self._make_record()
        del record["_npi_taxonomy_exclusions"]
        run_config = self._make_run_config(active_rules=["rei_on_staff"])
        triggered, _ = check_structural_exclusions(record, run_config)
        assert "rei_on_staff" not in triggered

    def test_taxonomy_exclusion_end_to_end_excluded(self):
        """End-to-end: _npi_taxonomy_exclusions matching active rule → EXCLUDED."""
        record = {
            "id": "T-REI",
            "practice_name": "REI Center",
            "specialty": "OBGYN",
            "address_state": "TX",
            "address_zip": "75001",
            "website_url": "https://example.com",
            "_npi_taxonomy_exclusions": ["rei_on_staff"],
            "signals": [],
            "bullseye_score": 85,
            "fit_signal_score": 85,
            "confidence_score": 80,
            "enrichment_status": "complete",
            "source_confidence": "complete",
        }
        run_config = self._make_run_config(active_rules=["rei_on_staff"])
        result = apply_exclusions(record, run_config)
        assert result["exclusion_status"] == "EXCLUDED"
        assert result["target_tier"] == "Excluded"
        assert result.get("exclusion_reason")


# ---------------------------------------------------------------------------
# LLM Parse Failure Routes to needs_review
# ---------------------------------------------------------------------------

class TestParseFailureStatus:
    """A structurally invalid Claude response (missing required keys) is a
    parse failure and must land needs_review, not failed (PIPELINE.md §error
    handling). RuntimeError (API failure) must still land failed."""

    _ICP = [{"signal_id": "S-01", "signal_label": "test",
             "prompt_instruction": "x", "positive_weight": 10}]

    def _run_with_response(self, raw):
        from unittest.mock import patch
        from enrichment.signal_extractor import extract_signals
        record = {"id": "T-1", "practice_name": "Test", "specialty": "OBGYN"}
        with patch("enrichment.signal_extractor._get_client", return_value=object()), \
             patch("enrichment.signal_extractor._call_claude",
                   return_value=(raw, {"input_tokens": 0, "output_tokens": 0})):
            return extract_signals(
                record=record,
                icp_signals=self._ICP,
                context_text="x" * 500,
                run_id="RUN-TEST",
            )

    def test_missing_signals_key_is_needs_review(self):
        result = self._run_with_response('{"sales_angle": []}')
        assert result["enrichment_status"] == "needs_review"
        assert "parse" in result["internal_notes"].lower()

    def test_missing_sales_angle_key_is_needs_review(self):
        result = self._run_with_response('{"signals": []}')
        assert result["enrichment_status"] == "needs_review"

    def test_api_failure_is_failed(self):
        from unittest.mock import patch
        from enrichment.signal_extractor import extract_signals
        record = {"id": "T-2", "practice_name": "Test", "specialty": "OBGYN"}
        with patch("enrichment.signal_extractor._get_client", return_value=object()), \
             patch("enrichment.signal_extractor._call_claude",
                   side_effect=RuntimeError("API down")):
            result = extract_signals(
                record=record,
                icp_signals=self._ICP,
                context_text="x" * 500,
                run_id="RUN-TEST",
            )
        assert result["enrichment_status"] == "failed"


# ---------------------------------------------------------------------------
# LLM Token Usage Capture (cost-per-run)
# ---------------------------------------------------------------------------

class TestTokenUsageCapture:
    """extract_signals records per-call token usage; the run log carries totals."""

    _ICP = [{"signal_id": "S-01", "signal_label": "test",
             "prompt_instruction": "x", "positive_weight": 10}]

    def test_extract_signals_records_usage(self):
        from unittest.mock import patch
        from enrichment.signal_extractor import extract_signals
        record = {"id": "T-1", "practice_name": "Test", "specialty": "OBGYN"}
        raw = '{"signals": [], "sales_angle": []}'
        with patch("enrichment.signal_extractor._get_client", return_value=object()), \
             patch("enrichment.signal_extractor._call_claude",
                   return_value=(raw, {"input_tokens": 1200, "output_tokens": 340})):
            result = extract_signals(
                record=record, icp_signals=self._ICP,
                context_text="x" * 500, run_id="RUN-TEST",
            )
        assert result["_llm_usage"] == {"input_tokens": 1200, "output_tokens": 340}

    def test_run_log_carries_usage_totals(self, tmp_path):
        import json as _json
        from output.log_writer import write_run_log
        path = write_run_log(
            run_id="RUN-TEST", records=[], errors=[], warnings=[],
            input_file="in.csv", input_source_type="manual", records_input=0,
            output_dir=str(tmp_path),
            llm_usage={"llm_input_tokens": 5000, "llm_output_tokens": 900,
                       "llm_call_count": 4},
        )
        log = _json.loads(open(path, encoding="utf-8").read())
        assert log["llm_input_tokens"] == 5000
        assert log["llm_output_tokens"] == 900
        assert log["llm_call_count"] == 4

    def test_run_log_omits_usage_when_not_captured(self, tmp_path):
        import json as _json
        from output.log_writer import write_run_log
        path = write_run_log(
            run_id="RUN-TEST", records=[], errors=[], warnings=[],
            input_file="in.csv", input_source_type="manual", records_input=0,
            output_dir=str(tmp_path),
        )
        log = _json.loads(open(path, encoding="utf-8").read())
        assert "llm_input_tokens" not in log
        assert "llm_call_count" not in log


class TestSimulator:
    """simulate_icp.py — exclude_if_yes and reinforcement parity with real pipeline."""

    _BASE_SIGNALS = [
        {"signal_id": "S-1", "signal_label": "Clear aligners", "positive_weight": 35, "required_for_bullseye": True},
        {"signal_id": "S-2", "signal_label": "Open scanner", "positive_weight": 28},
        {"signal_id": "S-3", "signal_label": "Financing", "positive_weight": 22},
        {"signal_id": "S-4", "signal_label": "Treats adults", "positive_weight": 15},
        {"signal_id": "S-X", "signal_label": "Exclusion trigger", "positive_weight": 0, "exclude_if_yes": True},
    ]

    def _run(self, states, signals=None, bullseye_min=90):
        from simulate_icp import simulate
        return simulate(signals or self._BASE_SIGNALS, states, bullseye_min)

    def test_exclude_if_yes_returns_excluded_tier(self):
        states = {
            "S-1": {"state": "yes", "confidence": "high"},
            "S-2": {"state": "yes", "confidence": "high"},
            "S-3": {"state": "yes", "confidence": "high"},
            "S-4": {"state": "yes", "confidence": "high"},
            "S-X": {"state": "yes", "confidence": "high"},
        }
        result = self._run(states)
        assert result["tier"] == "Excluded"
        assert "Exclusion trigger" in result["tier_cap_reason"]

    def test_exclude_if_yes_not_fired_when_state_not_yes(self):
        states = {
            "S-1": {"state": "yes", "confidence": "high"},
            "S-2": {"state": "yes", "confidence": "high"},
            "S-3": {"state": "yes", "confidence": "high"},
            "S-4": {"state": "yes", "confidence": "high"},
            "S-X": {"state": "not_found", "confidence": "high"},
        }
        result = self._run(states)
        assert result["tier"] == "Bullseye"

    def test_reinforcement_grants_inference_credit(self):
        """S-R reinforces S-2; when S-R is yes and S-2 is not_found, S-2 earns partial credit."""
        signals = [
            {"signal_id": "S-1", "signal_label": "Aligners", "positive_weight": 35, "required_for_bullseye": True},
            {"signal_id": "S-2", "signal_label": "Open scanner", "positive_weight": 28},
            {"signal_id": "S-R", "signal_label": "Competing brands", "positive_weight": 0, "reinforces": "S-2"},
        ]
        # With reinforcement: S-R yes → S-2 inferred → partial credit on S-2
        result_with = self._run(
            {"S-1": {"state": "yes", "confidence": "high"}, "S-R": {"state": "yes", "confidence": "high"}},
            signals=signals, bullseye_min=50,
        )
        # Without reinforcement: S-R not_found → S-2 not_found → no credit
        result_without = self._run(
            {"S-1": {"state": "yes", "confidence": "high"}, "S-R": {"state": "not_found", "confidence": "high"}},
            signals=signals, bullseye_min=50,
        )
        assert result_with["fit_signal_score"] > result_without["fit_signal_score"]

    def test_reinforcement_skips_verification_gate(self):
        """A verification_required signal that is state_inferred should not cap the tier."""
        signals = [
            {"signal_id": "S-1", "signal_label": "Aligners", "positive_weight": 35, "required_for_bullseye": True},
            {"signal_id": "S-2", "signal_label": "Scanner", "positive_weight": 28, "verification_required": True},
            {"signal_id": "S-R", "signal_label": "Competing brands", "positive_weight": 0, "reinforces": "S-2"},
        ]
        # S-R yes → S-2 inferred → verification gate skipped → Bullseye reachable
        result = self._run(
            {"S-1": {"state": "yes", "confidence": "high"}, "S-R": {"state": "yes", "confidence": "high"}},
            signals=signals, bullseye_min=50,
        )
        assert result["tier"] == "Bullseye"

    def test_inhibited_by_suppresses_exclusion_when_inhibitor_yes(self):
        """exclude_if_yes is suppressed when the inhibited_by signal is also yes."""
        signals = [
            {"signal_id": "S-1", "signal_label": "Aligners", "positive_weight": 35, "required_for_bullseye": True},
            {"signal_id": "S-2", "signal_label": "Scanner", "positive_weight": 28},
            # S-EX is the exclusion signal; S-INH is its inhibitor
            {"signal_id": "S-EX", "signal_label": "High tier exclusive", "positive_weight": 0,
             "exclude_if_yes": True, "inhibited_by": "S-INH"},
            {"signal_id": "S-INH", "signal_label": "Competing brands present", "positive_weight": 0,
             "cap_tier": "Contender"},
        ]
        # Both S-EX and S-INH yes → inhibitor fires → exclusion suppressed → Contender via cap_tier
        result = self._run(
            {
                "S-1": {"state": "yes", "confidence": "high"},
                "S-2": {"state": "yes", "confidence": "high"},
                "S-EX": {"state": "yes", "confidence": "high"},
                "S-INH": {"state": "yes", "confidence": "high"},
            },
            signals=signals, bullseye_min=50,
        )
        assert result["tier"] == "Contender"

    def test_inhibited_by_does_not_suppress_when_inhibitor_not_yes(self):
        """exclude_if_yes fires normally when the inhibited_by signal is not yes."""
        signals = [
            {"signal_id": "S-1", "signal_label": "Aligners", "positive_weight": 35, "required_for_bullseye": True},
            {"signal_id": "S-2", "signal_label": "Scanner", "positive_weight": 28},
            {"signal_id": "S-EX", "signal_label": "High tier exclusive", "positive_weight": 0,
             "exclude_if_yes": True, "inhibited_by": "S-INH"},
            {"signal_id": "S-INH", "signal_label": "Competing brands present", "positive_weight": 0,
             "cap_tier": "Contender"},
        ]
        # S-EX yes, S-INH not_found → exclusion fires
        result = self._run(
            {
                "S-1": {"state": "yes", "confidence": "high"},
                "S-2": {"state": "yes", "confidence": "high"},
                "S-EX": {"state": "yes", "confidence": "high"},
                "S-INH": {"state": "not_found", "confidence": "high"},
            },
            signals=signals, bullseye_min=50,
        )
        assert result["tier"] == "Excluded"
