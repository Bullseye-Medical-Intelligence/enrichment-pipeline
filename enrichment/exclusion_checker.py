"""
exclusion_checker.py
Applies exclusion rules to enriched records.
Hard exclusions always fire. Configurable exclusions fire only when listed in run_config.
"""

# ---------------------------------------------------------------------------
# Exclusion rule definitions
# ---------------------------------------------------------------------------

# Hard exclusions — always applied regardless of run_config
HARD_EXCLUSION_RULES = {
    "hospital_owned",
    "health_system_affiliated",
    "wrong_specialty",
    "outside_geography",
    "practice_closed",
    "academic_medical_center",
}

# Configurable exclusions — only applied when listed in active_exclusion_rules
CONFIGURABLE_EXCLUSION_RULES = {
    "rei_on_staff",
    "no_web_presence",
    "competitor_conflict",
    "no_relevant_service_line",
}

ALL_KNOWN_EXCLUSION_RULES = HARD_EXCLUSION_RULES | CONFIGURABLE_EXCLUSION_RULES

# Max bullseye_score for excluded records
EXCLUDED_SCORE_CAP = 40


def _check_geography(record: dict, target_geography: list[str]) -> bool:
    """
    Return True if the record's state is outside the target geography.
    Empty target_geography list means no geography restriction.
    """
    if not target_geography:
        return False
    state = (record.get("address_state") or "").strip().upper()
    geo_upper = [g.strip().upper() for g in target_geography]
    return state not in geo_upper


def apply_exclusions(record: dict, run_config: dict) -> dict:
    """
    Apply all active exclusion rules to a record.
    Sets exclusion_status, exclusion_reason, and target_tier.
    Caps bullseye_score for excluded records.

    Args:
        record: Enriched record dict.
        run_config: Loaded run_config.json dict.

    Returns:
        Updated record.
    """
    active_rules = set(run_config.get("active_exclusion_rules", []))
    target_geography = run_config.get("target_geography", [])
    target_specialty = (run_config.get("target_specialty") or "").strip()

    # Collect all triggered exclusions
    triggered = []
    rationale_parts = []

    # LLM-detected triggers from signal extraction step
    llm_triggers = set(record.get("_llm_exclusion_triggers") or [])
    llm_rationale = (record.get("_llm_exclusion_rationale") or "").strip()

    # --- Hard exclusions ---

    # Geography check (pipeline-level, overrides LLM)
    if "outside_geography" in (HARD_EXCLUSION_RULES & (active_rules | HARD_EXCLUSION_RULES)):
        if target_geography and _check_geography(record, target_geography):
            triggered.append("outside_geography")
            state = record.get("address_state", "unknown")
            rationale_parts.append(
                f"Practice is in {state}, outside target geography "
                f"({', '.join(target_geography)})."
            )

    # Specialty check
    if target_specialty and "wrong_specialty" in HARD_EXCLUSION_RULES:
        record_specialty = (record.get("specialty") or "").strip()
        if record_specialty and record_specialty.lower() != target_specialty.lower():
            # Check if LLM also flagged it or if specialty clearly doesn't match
            if "wrong_specialty" in llm_triggers:
                triggered.append("wrong_specialty")
                rationale_parts.append(
                    f"Practice specialty '{record_specialty}' does not match "
                    f"target '{target_specialty}'."
                )

    # LLM-detected hard exclusions
    for trigger in llm_triggers:
        if trigger in HARD_EXCLUSION_RULES and trigger not in triggered:
            triggered.append(trigger)

    if llm_rationale and llm_triggers & HARD_EXCLUSION_RULES:
        rationale_parts.append(llm_rationale)

    # --- Configurable exclusions (only if active in run_config) ---
    for trigger in llm_triggers:
        if (trigger in CONFIGURABLE_EXCLUSION_RULES
                and trigger in active_rules
                and trigger not in triggered):
            triggered.append(trigger)

    # If we have configurable triggers and LLM provided rationale, include it
    if llm_rationale and llm_triggers & CONFIGURABLE_EXCLUSION_RULES & active_rules:
        if llm_rationale not in rationale_parts:
            rationale_parts.append(llm_rationale)

    # No-web-presence configurable exclusion (pipeline-detectable)
    if ("no_web_presence" in active_rules
            and "no_web_presence" not in triggered):
        url_valid = record.get("_url_valid", True)
        context_text = (record.get("_context_text") or "").strip()
        if not url_valid and not context_text:
            triggered.append("no_web_presence")
            rationale_parts.append(
                "No valid website URL and no public web presence detected."
            )

    # --- Apply results ---
    if triggered:
        record["exclusion_status"] = "EXCLUDED"
        record["exclusion_reason"] = " | ".join(rationale_parts) if rationale_parts else (
            f"Exclusion rules triggered: {', '.join(triggered)}"
        )
        record["target_tier"] = "Excluded"

        # Cap score for excluded records
        if record.get("bullseye_score", 0) > EXCLUDED_SCORE_CAP:
            record["bullseye_score"] = EXCLUDED_SCORE_CAP

        print(f"    ⊘ EXCLUDED: {', '.join(triggered)}")
    else:
        record["exclusion_status"] = "CLEAR"
        record["exclusion_reason"] = None

        # Set target_tier based on score if not already set
        bullseye_min = run_config.get("bullseye_min_score", 75)
        score = record.get("bullseye_score", 0)
        if score >= bullseye_min:
            record["target_tier"] = "Bullseye"
        elif score >= 50:
            record["target_tier"] = "Watchlist"
        else:
            record["target_tier"] = "Excluded"

    return record
