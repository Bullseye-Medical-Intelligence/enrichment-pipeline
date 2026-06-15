"""
config_validator.py
Pre-run validation for run_config.json and icp_checklist.json.

All checks are deterministic — no network calls, no LLM calls, no side effects.
Both functions raise ValueError with a clear human-readable message on the first
failure they detect.  validate_run_config() additionally returns a list of
non-fatal warning strings (e.g. a suppression file path that does not exist yet).

Usage from the CLI (pipeline.py):
    from enrichment.config_validator import validate_icp, validate_run_config
    validate_icp(icp_data)
    warnings = validate_run_config(run_config)
    for w in warnings:
        print(f"  [WARN] {w}")
"""

import logging
from pathlib import Path

from enrichment.exclusion_checker import ALL_KNOWN_EXCLUSION_RULES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid values for a signal's optional cap_tier field.
VALID_CAP_TIERS: frozenset[str] = frozenset({"Contender", "Needs Verification"})

# US state + DC codes accepted in target_geography.
US_STATE_CODES: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
})

# CSV source_type values accepted at the pipeline level.
VALID_SOURCE_TYPES: frozenset[str] = frozenset({"outscraper", "manual"})


# ---------------------------------------------------------------------------
# ICP validation
# ---------------------------------------------------------------------------

def validate_icp(icp_data: dict, *, source_label: str = "ICP config") -> None:
    """
    Validate the structure and field values of an icp_checklist.json dict.

    Raises ValueError with a descriptive message on the first detected problem.
    Runs in O(n) with no external I/O.

    Checks (in order):
      - Root has a 'signals' list that is non-empty
      - No duplicate signal_id values
      - Each signal has signal_id, signal_label, prompt_instruction, positive_weight
      - positive_weight is numeric (not bool)
      - not_found_weight, no_weight (if present) are numeric
      - source_type (if present) is not set to 'static_lookup' or any other
        unsupported value
      - cap_tier (if present) is 'Contender' or 'Needs Verification'
      - verification_required, required_for_bullseye, exclude_if_yes (if present)
        are boolean
      - reinforces (if present) references an existing signal_id in this profile
    """
    signals = icp_data.get("signals")
    if not isinstance(signals, list):
        raise ValueError(f"{source_label}: 'signals' must be a list.")
    if not signals:
        raise ValueError(f"{source_label}: 'signals' list must not be empty.")

    # Collect all signal_ids first so we can validate cross-references.
    seen_ids: dict[str, int] = {}  # signal_id -> 1-based position
    for i, sig in enumerate(signals, start=1):
        if not isinstance(sig, dict):
            raise ValueError(f"{source_label}: signal #{i} must be an object, got {type(sig).__name__}.")
        sid = sig.get("signal_id")
        if not sid or not isinstance(sid, str):
            raise ValueError(f"{source_label}: signal #{i} is missing 'signal_id'.")
        if sid in seen_ids:
            raise ValueError(
                f"{source_label}: duplicate signal_id '{sid}' "
                f"(first at #{seen_ids[sid]}, repeated at #{i})."
            )
        seen_ids[sid] = i

    all_ids = frozenset(seen_ids)

    for i, sig in enumerate(signals, start=1):
        sid = sig.get("signal_id", f"#{i}")

        # Required string fields
        if not sig.get("signal_label") or not isinstance(sig["signal_label"], str):
            raise ValueError(
                f"{source_label}: signal '{sid}' is missing 'signal_label' (required non-empty string)."
            )
        if not sig.get("prompt_instruction") or not isinstance(sig["prompt_instruction"], str):
            raise ValueError(
                f"{source_label}: signal '{sid}' is missing 'prompt_instruction' "
                f"(required non-empty string)."
            )

        # positive_weight — required, numeric, not bool
        if "positive_weight" not in sig:
            raise ValueError(
                f"{source_label}: signal '{sid}' is missing 'positive_weight'."
            )
        _assert_numeric(sig["positive_weight"], source_label, sid, "positive_weight")

        # Optional numeric weights
        for opt_field in ("not_found_weight", "no_weight"):
            if opt_field in sig:
                _assert_numeric(sig[opt_field], source_label, sid, opt_field)

        # source_type — no values are currently supported on signals
        if "source_type" in sig:
            st = sig["source_type"]
            if st == "static_lookup":
                raise ValueError(
                    f"{source_label}: signal '{sid}' has source_type='static_lookup', "
                    f"which is not implemented. Remove source_type from the signal "
                    f"before running."
                )
            raise ValueError(
                f"{source_label}: signal '{sid}' has unsupported source_type='{st}'. "
                f"No signal source_types are currently implemented; remove the field."
            )

        # cap_tier
        if "cap_tier" in sig:
            ct = sig["cap_tier"]
            if ct not in VALID_CAP_TIERS:
                raise ValueError(
                    f"{source_label}: signal '{sid}' cap_tier='{ct}' is invalid. "
                    f"Must be one of: {sorted(VALID_CAP_TIERS)}."
                )

        # Boolean flags
        for bool_field in ("verification_required", "required_for_bullseye", "exclude_if_yes"):
            if bool_field in sig and not isinstance(sig[bool_field], bool):
                raise ValueError(
                    f"{source_label}: signal '{sid}' '{bool_field}' must be true or false, "
                    f"got {sig[bool_field]!r}."
                )

        # reinforces — must reference another signal_id in this profile
        if "reinforces" in sig:
            ref = sig["reinforces"]
            if not isinstance(ref, str) or not ref:
                raise ValueError(
                    f"{source_label}: signal '{sid}' 'reinforces' must be a non-empty "
                    f"signal_id string."
                )
            if ref not in all_ids:
                raise ValueError(
                    f"{source_label}: signal '{sid}' 'reinforces' references unknown "
                    f"signal_id '{ref}'."
                )


def _assert_numeric(value: object, source_label: str, sid: str, field: str) -> None:
    """Raise ValueError if *value* is not a non-bool numeric."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(
            f"{source_label}: signal '{sid}' '{field}' must be a number, got {value!r}."
        )


# ---------------------------------------------------------------------------
# Run-config validation
# ---------------------------------------------------------------------------

def validate_run_config(
    run_config: dict,
    *,
    source_label: str = "run_config",
) -> list[str]:
    """
    Validate run_config.json field values.

    Raises ValueError with a descriptive message on any hard failure.
    Returns a list of non-fatal warning strings (e.g. missing optional file).

    Hard failures:
      - target_geography entries not 2-letter uppercase US state codes
      - bullseye_min_score not in [0, 100]
      - verify_near_miss_band not >= 0
      - io_concurrency not positive int
      - llm_concurrency not positive int
      - request_timeout_seconds not positive int
      - request_retries not >= 0 int
      - max_pages_per_practice not positive int
      - active_exclusion_rules contains unrecognized rule names

    Warnings (non-fatal, returned in list):
      - suppression_list_path is set but the file does not exist
    """
    warnings: list[str] = []

    # target_geography
    geo = run_config.get("target_geography")
    if geo is not None:
        if not isinstance(geo, list):
            raise ValueError(f"{source_label}: 'target_geography' must be a list.")
        bad = [g for g in geo if not isinstance(g, str) or g not in US_STATE_CODES]
        if bad:
            raise ValueError(
                f"{source_label}: 'target_geography' contains invalid state code(s): "
                f"{sorted(bad)}. Each entry must be a 2-letter uppercase US state code."
            )

    # Numeric range checks
    _assert_int_range(run_config, "bullseye_min_score", minimum=0, maximum=100,
                      source_label=source_label, optional=True)
    _assert_int_range(run_config, "verify_near_miss_band", minimum=0,
                      source_label=source_label, optional=True)
    _assert_int_range(run_config, "io_concurrency", minimum=1,
                      source_label=source_label, optional=True)
    _assert_int_range(run_config, "llm_concurrency", minimum=1,
                      source_label=source_label, optional=True)
    _assert_int_range(run_config, "request_timeout_seconds", minimum=1,
                      source_label=source_label, optional=True)
    _assert_int_range(run_config, "request_retries", minimum=0,
                      source_label=source_label, optional=True)
    _assert_int_range(run_config, "max_pages_per_practice", minimum=1,
                      source_label=source_label, optional=True)

    # active_exclusion_rules content
    rules = run_config.get("active_exclusion_rules")
    if rules is not None:
        if not isinstance(rules, list):
            raise ValueError(
                f"{source_label}: 'active_exclusion_rules' must be a list."
            )
        # taxonomy_exclusion_rules defines client-specific rule names (e.g. rei_on_staff)
        # that map NPI taxonomy codes to named rules. Those names are valid in
        # active_exclusion_rules even though they're not in ALL_KNOWN_EXCLUSION_RULES.
        taxonomy_rule_names = {
            t.get("rule_name", "")
            for t in run_config.get("taxonomy_exclusion_rules", [])
            if isinstance(t, dict)
        }
        allowed_rules = ALL_KNOWN_EXCLUSION_RULES | taxonomy_rule_names
        unknown = sorted(set(rules) - allowed_rules)
        if unknown:
            raise ValueError(
                f"{source_label}: 'active_exclusion_rules' contains unrecognized "
                f"rule name(s): {unknown}. "
                f"Known rules: {sorted(ALL_KNOWN_EXCLUSION_RULES)}."
            )

    # suppression_list_path — warn if set but file absent
    supp = run_config.get("suppression_list_path")
    if supp:
        if not Path(supp).exists():
            warnings.append(
                f"suppression_list_path is set to '{supp}' but the file does not exist. "
                f"Suppression will be skipped for this run."
            )

    return warnings


def _assert_int_range(
    data: dict,
    field: str,
    minimum: int,
    maximum: int | None = None,
    *,
    source_label: str,
    optional: bool = False,
) -> None:
    """
    Raise ValueError if *field* is present but not an int within [minimum, maximum].
    When optional=True the field may be absent; when False it is required.
    """
    if field not in data:
        if not optional:
            raise ValueError(f"{source_label}: '{field}' is required.")
        return
    value = data[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            f"{source_label}: '{field}' must be an integer, got {value!r}."
        )
    if value < minimum:
        bound = f">= {minimum}" if maximum is None else f"{minimum}-{maximum}"
        raise ValueError(f"{source_label}: '{field}' must be {bound}, got {value}.")
    if maximum is not None and value > maximum:
        raise ValueError(
            f"{source_label}: '{field}' must be {minimum}-{maximum}, got {value}."
        )


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------

def validate_all(
    icp_data: dict,
    run_config: dict,
    *,
    icp_label: str = "ICP config",
    config_label: str = "run_config",
) -> list[str]:
    """
    Run both ICP and run_config validation in sequence.

    Raises ValueError on the first hard failure encountered.
    Returns combined list of non-fatal warning strings.
    """
    validate_icp(icp_data, source_label=icp_label)
    return validate_run_config(run_config, source_label=config_label)
