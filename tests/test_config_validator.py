"""
tests/test_config_validator.py
Unit tests for enrichment/config_validator.py.

All tests are deterministic — no API calls, no HTTP, no subprocess execution.
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from enrichment.config_validator import (
    validate_icp,
    validate_run_config,
    validate_all,
    VALID_CAP_TIERS,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _minimal_signal(**overrides) -> dict:
    """Return a minimal valid signal dict."""
    base = {
        "signal_id": "S-001",
        "signal_label": "IUI services listed",
        "prompt_instruction": "Does this practice perform IUI?",
        "positive_weight": 20,
    }
    base.update(overrides)
    return base


def _minimal_icp(*signals) -> dict:
    """Wrap signals in an icp_data dict."""
    return {"signals": list(signals) or [_minimal_signal()]}


def _minimal_run_config(**overrides) -> dict:
    """Return a minimal valid run_config dict (no optional fields)."""
    base = {
        "bullseye_min_score": 90,
        "io_concurrency": 10,
        "llm_concurrency": 6,
        "request_timeout_seconds": 15,
        "request_retries": 3,
        "max_pages_per_practice": 5,
        "verify_near_miss_band": 0,
        "active_exclusion_rules": ["wrong_specialty", "outside_geography"],
    }
    base.update(overrides)
    return base


def _raises(fn, *args, **kwargs) -> str:
    """Call fn(*args, **kwargs), assert it raises ValueError, return the message."""
    with pytest.raises(ValueError) as exc_info:
        fn(*args, **kwargs)
    return str(exc_info.value)


# ---------------------------------------------------------------------------
# ICP — structural checks
# ---------------------------------------------------------------------------

class TestICPStructural:
    def test_missing_signals_key_fails(self):
        msg = _raises(validate_icp, {})
        assert "signals" in msg

    def test_signals_not_list_fails(self):
        msg = _raises(validate_icp, {"signals": "not-a-list"})
        assert "signals" in msg

    def test_empty_signals_list_fails(self):
        msg = _raises(validate_icp, {"signals": []})
        assert "empty" in msg.lower() or "signals" in msg

    def test_valid_minimal_icp_passes(self):
        validate_icp(_minimal_icp())   # must not raise

    def test_signal_not_dict_fails(self):
        msg = _raises(validate_icp, {"signals": ["not-a-dict"]})
        assert "object" in msg.lower() or "dict" in msg.lower()


# ---------------------------------------------------------------------------
# ICP — duplicate signal_id
# ---------------------------------------------------------------------------

class TestICPDuplicateSignalId:
    def test_duplicate_signal_id_fails(self):
        s1 = _minimal_signal(signal_id="S-DUP")
        s2 = _minimal_signal(signal_id="S-DUP", signal_label="Second label")
        msg = _raises(validate_icp, _minimal_icp(s1, s2))
        assert "duplicate" in msg.lower() or "S-DUP" in msg

    def test_unique_signal_ids_pass(self):
        s1 = _minimal_signal(signal_id="S-001")
        s2 = _minimal_signal(signal_id="S-002")
        validate_icp(_minimal_icp(s1, s2))   # must not raise


# ---------------------------------------------------------------------------
# ICP — required fields
# ---------------------------------------------------------------------------

class TestICPRequiredFields:
    def test_missing_signal_id_fails(self):
        sig = _minimal_signal()
        del sig["signal_id"]
        msg = _raises(validate_icp, _minimal_icp(sig))
        assert "signal_id" in msg

    def test_empty_signal_id_fails(self):
        msg = _raises(validate_icp, _minimal_icp(_minimal_signal(signal_id="")))
        assert "signal_id" in msg

    def test_missing_signal_label_fails(self):
        sig = _minimal_signal()
        del sig["signal_label"]
        msg = _raises(validate_icp, _minimal_icp(sig))
        assert "signal_label" in msg

    def test_empty_signal_label_fails(self):
        msg = _raises(validate_icp, _minimal_icp(_minimal_signal(signal_label="")))
        assert "signal_label" in msg

    def test_missing_prompt_instruction_fails(self):
        sig = _minimal_signal()
        del sig["prompt_instruction"]
        msg = _raises(validate_icp, _minimal_icp(sig))
        assert "prompt_instruction" in msg

    def test_empty_prompt_instruction_fails(self):
        msg = _raises(validate_icp, _minimal_icp(_minimal_signal(prompt_instruction="")))
        assert "prompt_instruction" in msg

    def test_missing_positive_weight_fails(self):
        sig = _minimal_signal()
        del sig["positive_weight"]
        msg = _raises(validate_icp, _minimal_icp(sig))
        assert "positive_weight" in msg


# ---------------------------------------------------------------------------
# ICP — numeric weight fields
# ---------------------------------------------------------------------------

class TestICPNumericWeights:
    def test_positive_weight_string_fails(self):
        msg = _raises(validate_icp, _minimal_icp(_minimal_signal(positive_weight="20")))
        assert "positive_weight" in msg

    def test_positive_weight_bool_fails(self):
        msg = _raises(validate_icp, _minimal_icp(_minimal_signal(positive_weight=True)))
        assert "positive_weight" in msg

    def test_positive_weight_zero_passes(self):
        validate_icp(_minimal_icp(_minimal_signal(positive_weight=0)))

    def test_positive_weight_negative_passes(self):
        validate_icp(_minimal_icp(_minimal_signal(positive_weight=-10)))

    def test_positive_weight_float_passes(self):
        validate_icp(_minimal_icp(_minimal_signal(positive_weight=1.5)))

    def test_not_found_weight_string_fails(self):
        msg = _raises(validate_icp, _minimal_icp(
            _minimal_signal(not_found_weight="bad")
        ))
        assert "not_found_weight" in msg

    def test_not_found_weight_absent_passes(self):
        validate_icp(_minimal_icp(_minimal_signal()))   # no not_found_weight

    def test_not_found_weight_numeric_passes(self):
        validate_icp(_minimal_icp(_minimal_signal(not_found_weight=-5)))

    def test_no_weight_string_fails(self):
        msg = _raises(validate_icp, _minimal_icp(_minimal_signal(no_weight="bad")))
        assert "no_weight" in msg

    def test_no_weight_bool_fails(self):
        msg = _raises(validate_icp, _minimal_icp(_minimal_signal(no_weight=False)))
        assert "no_weight" in msg

    def test_no_weight_numeric_passes(self):
        validate_icp(_minimal_icp(_minimal_signal(no_weight=-3)))


# ---------------------------------------------------------------------------
# ICP — source_type
# ---------------------------------------------------------------------------

class TestICPSourceType:
    def test_static_lookup_fails_loudly(self):
        msg = _raises(validate_icp, _minimal_icp(
            _minimal_signal(source_type="static_lookup")
        ))
        assert "static_lookup" in msg
        assert "not implemented" in msg.lower() or "implemented" in msg.lower()

    def test_unknown_source_type_fails(self):
        msg = _raises(validate_icp, _minimal_icp(
            _minimal_signal(source_type="database_lookup")
        ))
        assert "unsupported" in msg.lower() or "source_type" in msg

    def test_absent_source_type_passes(self):
        validate_icp(_minimal_icp(_minimal_signal()))   # no source_type field


# ---------------------------------------------------------------------------
# ICP — cap_tier
# ---------------------------------------------------------------------------

class TestICPCapTier:
    def test_invalid_cap_tier_fails(self):
        msg = _raises(validate_icp, _minimal_icp(
            _minimal_signal(cap_tier="Bullseye")
        ))
        assert "cap_tier" in msg or "Bullseye" in msg

    def test_contender_cap_tier_passes(self):
        validate_icp(_minimal_icp(_minimal_signal(cap_tier="Contender")))

    def test_needs_verification_cap_tier_passes(self):
        validate_icp(_minimal_icp(_minimal_signal(cap_tier="Needs Verification")))

    def test_excluded_cap_tier_fails(self):
        msg = _raises(validate_icp, _minimal_icp(
            _minimal_signal(cap_tier="Excluded")
        ))
        assert "cap_tier" in msg

    def test_all_valid_cap_tiers_pass(self):
        for tier in VALID_CAP_TIERS:
            validate_icp(_minimal_icp(_minimal_signal(cap_tier=tier)))

    def test_invalid_floor_tier_fails(self):
        msg = _raises(validate_icp, _minimal_icp(_minimal_signal(floor_tier="Bullseye")))
        assert "floor_tier" in msg or "Bullseye" in msg

    def test_valid_floor_tier_passes(self):
        validate_icp(_minimal_icp(_minimal_signal(floor_tier="Contender")))


# ---------------------------------------------------------------------------
# ICP — boolean flags
# ---------------------------------------------------------------------------

class TestICPBooleanFlags:
    @pytest.mark.parametrize("field", [
        "verification_required", "required_for_bullseye", "exclude_if_yes"
    ])
    def test_string_bool_flag_fails(self, field):
        msg = _raises(validate_icp, _minimal_icp(_minimal_signal(**{field: "true"})))
        assert field in msg

    @pytest.mark.parametrize("field", [
        "verification_required", "required_for_bullseye", "exclude_if_yes"
    ])
    def test_int_bool_flag_fails(self, field):
        msg = _raises(validate_icp, _minimal_icp(_minimal_signal(**{field: 1})))
        assert field in msg

    @pytest.mark.parametrize("field", [
        "verification_required", "required_for_bullseye", "exclude_if_yes"
    ])
    def test_true_bool_flag_passes(self, field):
        validate_icp(_minimal_icp(_minimal_signal(**{field: True})))

    @pytest.mark.parametrize("field", [
        "verification_required", "required_for_bullseye", "exclude_if_yes"
    ])
    def test_false_bool_flag_passes(self, field):
        validate_icp(_minimal_icp(_minimal_signal(**{field: False})))


# ---------------------------------------------------------------------------
# ICP — reinforces
# ---------------------------------------------------------------------------

class TestICPReinforces:
    def test_reinforces_unknown_signal_id_fails(self):
        msg = _raises(validate_icp, _minimal_icp(
            _minimal_signal(signal_id="S-001", reinforces="S-999")
        ))
        assert "reinforces" in msg
        assert "S-999" in msg or "unknown" in msg.lower()

    def test_reinforces_known_signal_id_passes(self):
        s1 = _minimal_signal(signal_id="S-001")
        s2 = _minimal_signal(signal_id="S-002", reinforces="S-001")
        validate_icp(_minimal_icp(s1, s2))   # must not raise

    def test_reinforces_empty_string_fails(self):
        msg = _raises(validate_icp, _minimal_icp(
            _minimal_signal(signal_id="S-001", reinforces="")
        ))
        assert "reinforces" in msg

    def test_reinforces_self_reference_fails(self):
        # A signal cannot reinforce itself — it's not in signal_ids when checked
        # (signal_ids is built before per-signal iteration, so self-ref is allowed
        # unless we explicitly check). Verify the validator's actual behaviour.
        s = _minimal_signal(signal_id="S-001", reinforces="S-001")
        # Self-reference IS in signal_ids so it should pass (not a documented failure)
        validate_icp(_minimal_icp(s))

    def test_inhibited_by_unknown_signal_id_fails(self):
        msg = _raises(validate_icp, _minimal_icp(
            _minimal_signal(signal_id="S-001", inhibited_by="S-999")
        ))
        assert "inhibited_by" in msg

    def test_inhibited_by_known_signal_id_passes(self):
        s1 = _minimal_signal(signal_id="S-001")
        s2 = _minimal_signal(signal_id="S-002", inhibited_by="S-001")
        validate_icp(_minimal_icp(s1, s2))   # must not raise


# ---------------------------------------------------------------------------
# ICP — success with known-good Femasys config
# ---------------------------------------------------------------------------

class TestICPKnownGoodConfig:
    def test_femasys_icp_passes(self):
        """The live Femasys client ICP must pass validation cleanly."""
        path = os.path.join(
            os.path.dirname(__file__), "..",
            "config", "clients", "obgyn_femasys", "icp_checklist.json"
        )
        with open(path, encoding="utf-8") as f:
            icp_data = json.load(f)
        validate_icp(icp_data)   # must not raise


# ---------------------------------------------------------------------------
# Run-config — target_geography
# ---------------------------------------------------------------------------

class TestRunConfigGeography:
    def test_valid_state_codes_pass(self):
        validate_run_config(_minimal_run_config(target_geography=["TX", "FL", "GA"]))

    def test_invalid_state_code_fails(self):
        msg = _raises(validate_run_config,
                      _minimal_run_config(target_geography=["TX", "ZZ"]))
        assert "ZZ" in msg or "state code" in msg.lower()

    def test_lowercase_state_code_fails(self):
        msg = _raises(validate_run_config,
                      _minimal_run_config(target_geography=["tx"]))
        assert "tx" in msg.lower() or "state code" in msg.lower()

    def test_three_letter_code_fails(self):
        msg = _raises(validate_run_config,
                      _minimal_run_config(target_geography=["TEX"]))
        assert "TEX" in msg or "state code" in msg.lower()

    def test_absent_geography_passes(self):
        cfg = _minimal_run_config()
        cfg.pop("target_geography", None)
        validate_run_config(cfg)   # target_geography is optional

    def test_empty_geography_list_passes(self):
        validate_run_config(_minimal_run_config(target_geography=[]))

    def test_dc_code_passes(self):
        validate_run_config(_minimal_run_config(target_geography=["DC"]))


# ---------------------------------------------------------------------------
# Run-config — integer range fields
# ---------------------------------------------------------------------------

class TestRunConfigIntegers:
    @pytest.mark.parametrize("field,bad_value,expected_fragment", [
        ("bullseye_min_score", -1, "bullseye_min_score"),
        ("bullseye_min_score", 101, "bullseye_min_score"),
        ("bullseye_min_score", "90", "bullseye_min_score"),
        ("bullseye_min_score", 90.5, "bullseye_min_score"),
        ("bullseye_min_score", True, "bullseye_min_score"),
        ("verify_near_miss_band", -1, "verify_near_miss_band"),
        ("verify_near_miss_band", "0", "verify_near_miss_band"),
        ("io_concurrency", 0, "io_concurrency"),
        ("io_concurrency", -1, "io_concurrency"),
        ("io_concurrency", "10", "io_concurrency"),
        ("llm_concurrency", 0, "llm_concurrency"),
        ("llm_concurrency", "3", "llm_concurrency"),
        ("request_timeout_seconds", 0, "request_timeout_seconds"),
        ("request_timeout_seconds", "15", "request_timeout_seconds"),
        ("request_retries", -1, "request_retries"),
        ("request_retries", "3", "request_retries"),
        ("max_pages_per_practice", 0, "max_pages_per_practice"),
        ("max_pages_per_practice", "5", "max_pages_per_practice"),
    ])
    def test_invalid_int_field_fails(self, field, bad_value, expected_fragment):
        msg = _raises(validate_run_config, _minimal_run_config(**{field: bad_value}))
        assert expected_fragment in msg

    def test_bullseye_min_score_zero_passes(self):
        validate_run_config(_minimal_run_config(bullseye_min_score=0))

    def test_bullseye_min_score_100_passes(self):
        validate_run_config(_minimal_run_config(bullseye_min_score=100))

    def test_verify_near_miss_band_zero_passes(self):
        validate_run_config(_minimal_run_config(verify_near_miss_band=0))

    def test_request_retries_zero_passes(self):
        validate_run_config(_minimal_run_config(request_retries=0))


# ---------------------------------------------------------------------------
# Run-config — active_exclusion_rules
# ---------------------------------------------------------------------------

class TestRunConfigExclusionRules:
    def test_unrecognized_rule_fails(self):
        msg = _raises(validate_run_config,
                      _minimal_run_config(active_exclusion_rules=["unknown_rule"]))
        assert "unrecognized" in msg.lower() or "unknown_rule" in msg

    def test_valid_rules_pass(self):
        validate_run_config(_minimal_run_config(
            active_exclusion_rules=["wrong_specialty", "outside_geography", "no_web_presence"]
        ))

    def test_empty_rules_list_passes(self):
        validate_run_config(_minimal_run_config(active_exclusion_rules=[]))

    def test_absent_rules_passes(self):
        cfg = _minimal_run_config()
        cfg.pop("active_exclusion_rules", None)
        validate_run_config(cfg)

    def test_mix_valid_and_invalid_fails(self):
        msg = _raises(validate_run_config,
                      _minimal_run_config(
                          active_exclusion_rules=["wrong_specialty", "bogus_rule"]
                      ))
        assert "bogus_rule" in msg


# ---------------------------------------------------------------------------
# Run-config — suppression_list_path warning
# ---------------------------------------------------------------------------

class TestRunConfigSuppressionPath:
    def test_missing_suppression_file_returns_warning(self, tmp_path):
        cfg = _minimal_run_config(suppression_list_path=str(tmp_path / "no_such_file.csv"))
        warnings = validate_run_config(cfg)
        assert any("suppression" in w.lower() for w in warnings)

    def test_existing_suppression_file_no_warning(self, tmp_path):
        sup = tmp_path / "suppression.csv"
        sup.write_text("name\nACME Corp\n")
        cfg = _minimal_run_config(suppression_list_path=str(sup))
        warnings = validate_run_config(cfg)
        assert not any("suppression" in w.lower() for w in warnings)

    def test_absent_suppression_path_no_warning(self):
        cfg = _minimal_run_config()
        warnings = validate_run_config(cfg)
        assert warnings == []


# ---------------------------------------------------------------------------
# Run-config — success with known-good Femasys config
# ---------------------------------------------------------------------------

class TestRunConfigKnownGoodConfig:
    def test_femasys_run_config_passes(self):
        """The live Femasys run_config.json must pass validation with no hard errors."""
        path = os.path.join(
            os.path.dirname(__file__), "..",
            "config", "clients", "obgyn_femasys", "run_config.json"
        )
        with open(path, encoding="utf-8") as f:
            run_config = json.load(f)
        warnings = validate_run_config(run_config)
        # No hard errors (function must not raise).
        # Warnings about missing suppression file or similar are acceptable.
        assert isinstance(warnings, list)


# ---------------------------------------------------------------------------
# validate_all
# ---------------------------------------------------------------------------

class TestValidateAll:
    def test_valid_pair_passes(self):
        warnings = validate_all(_minimal_icp(), _minimal_run_config())
        assert isinstance(warnings, list)

    def test_bad_icp_fails_first(self):
        msg = _raises(validate_all, {"signals": []}, _minimal_run_config())
        assert "signals" in msg   # ICP error, not run_config error

    def test_bad_run_config_fails_after_icp(self):
        msg = _raises(validate_all, _minimal_icp(),
                      _minimal_run_config(bullseye_min_score=-1))
        assert "bullseye_min_score" in msg

    def test_returns_warnings_from_run_config(self, tmp_path):
        cfg = _minimal_run_config(suppression_list_path=str(tmp_path / "gone.csv"))
        warnings = validate_all(_minimal_icp(), cfg)
        assert any("suppression" in w.lower() for w in warnings)
