"""
scorer.py
Scoring validation — final pass to ensure all scores are in range,
all signal_state values are valid, and all required fields are populated.
Called after exclusion check (Step 7).
"""

VALID_SIGNAL_STATES = {"yes", "no", "not_found"}
VALID_EXCLUSION_STATUSES = {"CLEAR", "EXCLUDED"}
VALID_TARGET_TIERS = {"Bullseye", "Watchlist", "Excluded"}
VALID_SOURCE_CONFIDENCES = {"complete", "partial", "limited", "failed"}
VALID_ENRICHMENT_STATUSES = {"complete", "partial", "failed", "needs_review"}
VALID_QC_STATUSES = {"pending"}  # Pipeline always outputs "pending"

EXCLUDED_SCORE_CAP = 40
MIN_SCORE = 0
MAX_SCORE = 100


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
    tier = record.get("target_tier")
    if tier not in VALID_TARGET_TIERS:
        # Infer from score
        score = record["bullseye_score"]
        if record["exclusion_status"] == "EXCLUDED":
            record["target_tier"] = "Excluded"
        elif score >= 75:
            record["target_tier"] = "Bullseye"
        elif score >= 50:
            record["target_tier"] = "Watchlist"
        else:
            record["target_tier"] = "Excluded"
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

    # fit_confidence_status: default if missing
    if not record.get("fit_confidence_status"):
        record["fit_confidence_status"] = "LOW FIT / LOW EVIDENCE"

    # date_enriched: should be set by signal_extractor, but just in case
    from datetime import date
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
