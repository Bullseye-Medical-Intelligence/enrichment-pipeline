"""
scorer.py
Scoring validation — final pass to ensure all scores are in range,
all signal_state values are valid, and all required fields are populated.
Called after exclusion check (Step 7).

Enforces the invariant that target_tier == "Excluded" iff
exclusion_status == "EXCLUDED", repairing any contradiction.
"""

from datetime import date

from enrichment.constants import (
    CALL_BRIEF_LIST_FIELDS,
    CALL_BRIEF_STRING_FIELDS,
    DEFAULT_BULLSEYE_MIN_SCORE,
    EXCLUDED_SCORE_CAP,
    MAX_SCORE,
    MIN_SCORE,
    confidence_band_for_score,
    empty_call_brief,
)

DEFAULT_FIT_CONFIDENCE_STATUS = "LOW FIT / LOW EVIDENCE"

VALID_SIGNAL_STATES = {"yes", "no", "not_found"}
VALID_EXCLUSION_STATUSES = {"CLEAR", "EXCLUDED"}
VALID_TARGET_TIERS = {"Bullseye", "Needs Verification", "Contender", "Manual Review", "Excluded"}
# Legacy tier label -> current label. "Watchlist" was renamed to "Contender".
LEGACY_TIER_ALIAS = {"Watchlist": "Contender"}
VALID_SOURCE_CONFIDENCES = {"complete", "partial", "limited", "failed"}
VALID_ENRICHMENT_STATUSES = {"complete", "partial", "failed", "needs_review", "not_enriched"}
VALID_QC_STATUSES = {"pending"}  # Pipeline always outputs "pending"


def validate_and_finalize(record: dict) -> dict:
    """
    Final validation and cleanup pass for a record.
    Enforces all schema rules from PIPELINE.md.

    - Clamps scores to 0–100
    - Validates signal_state values
    - Ensures required fields have valid values
    - Caps excluded records at score 40
    - Sets defaults for missing optional fields

    Args:
        record: Enriched, exclusion-checked record dict.

    Returns:
        Validated and finalized record dict.
    """
    errors = []

    # --- Score validation ---
    for score_field in ("bullseye_score", "fit_signal_score", "confidence_score"):
        val = record.get(score_field)
        if val is None:
            record[score_field] = 0
            errors.append(f"Missing {score_field}, defaulted to 0")
        elif not isinstance(val, (int, float)):
            try:
                record[score_field] = int(val)
            except (TypeError, ValueError):
                record[score_field] = 0
                errors.append(f"Invalid {score_field} value '{val}', defaulted to 0")
        # Clamp
        record[score_field] = max(MIN_SCORE, min(MAX_SCORE, int(record[score_field])))

    # Excluded records: cap bullseye_score at 40
    if record.get("exclusion_status") == "EXCLUDED":
        if record["bullseye_score"] > EXCLUDED_SCORE_CAP:
            record["bullseye_score"] = EXCLUDED_SCORE_CAP

    # --- Signal state validation ---
    signals = record.get("signals") or []
    for sig in signals:
        state = sig.get("signal_state")
        if state not in VALID_SIGNAL_STATES:
            sig["signal_state"] = "not_found"
            errors.append(
                f"Signal {sig.get('signal_id', '?')}: invalid state '{state}', "
                f"set to 'not_found'"
            )
        # Ensure analyst_note is empty string, not null
        if sig.get("analyst_note") is None:
            sig["analyst_note"] = ""
        # Ensure exclude_if_yes is always present as a bool
        sig["exclude_if_yes"] = bool(sig.get("exclude_if_yes", False))

    # --- Exclusion status ---
    exc_status = record.get("exclusion_status")
    if exc_status not in VALID_EXCLUSION_STATUSES:
        record["exclusion_status"] = "CLEAR"
        errors.append(f"Invalid exclusion_status '{exc_status}', defaulted to CLEAR")

    # exclusion_reason: null when CLEAR
    if record["exclusion_status"] == "CLEAR":
        record["exclusion_reason"] = None
    elif not record.get("exclusion_reason"):
        record["exclusion_reason"] = "Excluded by pipeline rules"

    # --- Target tier ---
    # Normalize any legacy label (e.g. "Watchlist" from a frozen snapshot) first.
    tier = LEGACY_TIER_ALIAS.get(record.get("target_tier"), record.get("target_tier"))
    record["target_tier"] = tier
    exc_status = record["exclusion_status"]  # already validated above

    # Enforce invariant: target_tier == "Excluded" iff exclusion_status == "EXCLUDED".
    # Both values may individually be valid enum members while contradicting each other,
    # so this check runs unconditionally — not just when tier is outside the enum.
    if exc_status == "CLEAR" and tier == "Excluded":
        score = record["bullseye_score"]
        record["target_tier"] = "Bullseye" if score >= DEFAULT_BULLSEYE_MIN_SCORE else "Contender"
        errors.append(
            f"Invariant violation: exclusion_status=CLEAR but target_tier=Excluded; "
            f"repaired to {record['target_tier']}"
        )
    elif exc_status == "EXCLUDED" and tier != "Excluded":
        record["target_tier"] = "Excluded"
        errors.append(
            f"Invariant violation: exclusion_status=EXCLUDED but target_tier={tier!r}; "
            f"repaired to Excluded"
        )
    elif tier not in VALID_TARGET_TIERS:
        # Unknown tier value — infer from score and status
        score = record["bullseye_score"]
        if exc_status == "EXCLUDED":
            record["target_tier"] = "Excluded"
        elif score >= DEFAULT_BULLSEYE_MIN_SCORE:
            record["target_tier"] = "Bullseye"
        else:
            record["target_tier"] = "Contender"
        errors.append(f"Invalid target_tier '{tier}', inferred from score")

    # --- Source confidence ---
    sc = record.get("source_confidence")
    if sc not in VALID_SOURCE_CONFIDENCES:
        record["source_confidence"] = "partial"

    # --- Enrichment status ---
    es = record.get("enrichment_status")
    if es not in VALID_ENRICHMENT_STATUSES:
        record["enrichment_status"] = "partial"

    # --- QC status: always "pending" from pipeline ---
    record["qc_status"] = "pending"

    # --- Null/empty field rules ---
    # npi_optional: null is fine
    if "npi_optional" not in record:
        record["npi_optional"] = None

    # internal_notes: empty string, not null
    if record.get("internal_notes") is None:
        record["internal_notes"] = ""

    # tier_cap_reason: operator-facing explanation of why the tier landed below
    # Bullseye (set by _assign_tier). Always present as a string; "" for Bullseye,
    # excluded, or not-yet-enriched records.
    if not isinstance(record.get("tier_cap_reason"), str):
        record["tier_cap_reason"] = ""

    # Pipeline output fields always null
    record["analyst_override_classification"] = None
    record["override_reason"] = None
    record["client_facing_rationale"] = None

    # --- Required string fields: default to empty string if missing ---
    for field in ("practice_name", "specialty", "phone", "address_city",
                   "address_state", "address_zip", "metro_region_tag",
                   "state_mandate_status", "website_url"):
        if field not in record or record[field] is None:
            record[field] = ""

    # provider_names: empty list if missing
    if not isinstance(record.get("provider_names"), list):
        record["provider_names"] = []

    # sales_angle: empty list if missing
    if not isinstance(record.get("sales_angle"), list):
        record["sales_angle"] = []

    # call_brief: always present and fully shaped (string fields and list fields).
    brief = record.get("call_brief")
    if not isinstance(brief, dict):
        brief = empty_call_brief()
    else:
        defaults = empty_call_brief()
        for field in CALL_BRIEF_STRING_FIELDS:
            if not isinstance(brief.get(field), str):
                brief[field] = defaults[field]
        for field in CALL_BRIEF_LIST_FIELDS:
            if not isinstance(brief.get(field), list):
                brief[field] = defaults[field]
    record["call_brief"] = brief

    # fit_confidence_status: default if missing
    if not record.get("fit_confidence_status"):
        record["fit_confidence_status"] = DEFAULT_FIT_CONFIDENCE_STATUS

    # confidence_band: always present, derived from the (already clamped)
    # numeric confidence_score. Client-facing display shows this, never the number.
    record["confidence_band"] = confidence_band_for_score(record.get("confidence_score", 0))

    # date_enriched: should be set by signal_extractor, but just in case
    if not record.get("date_enriched"):
        record["date_enriched"] = date.today().isoformat()

    if errors:
        existing_notes = record.get("internal_notes") or ""
        note = f"[Validation warnings: {'; '.join(errors)}]"
        record["internal_notes"] = f"{existing_notes} {note}".strip()

    return record


def strip_internal_fields(record: dict) -> dict:
    """
    Remove all internal pipeline tracking fields (prefixed with '_')
    before writing to output. These are not part of the output schema.

    Args:
        record: Finalized record dict.

    Returns:
        Clean record dict with only output schema fields.
    """
    return {k: v for k, v in record.items() if not k.startswith("_")}
