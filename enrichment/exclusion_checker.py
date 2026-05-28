"""
exclusion_checker.py
Applies exclusion rules to enriched records.
Hard exclusions always fire. Configurable exclusions fire only when listed in run_config.
"""

import re

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

# Tier ladder for CLEAR records (worst to best). A signal can cap the tier at a
# lower rung; an unconfirmed required signal caps a would-be Bullseye at
# "Needs Verification". "Excluded" only ever comes from an exclusion rule.
TIER_RANK = {"Excluded": 0, "Watchlist": 1, "Needs Verification": 2, "Bullseye": 3}
_RANK_TO_TIER = {rank: tier for tier, rank in TIER_RANK.items()}


def _assign_tier(record: dict, score: int, bullseye_min: int) -> str:
    """Assign a CLEAR record's tier from its score, signal caps, and verification flags.

    Starts from the score-based tier, lets any "yes" signal with a cap_tier pull
    the ceiling down, then caps a would-be Bullseye at "Needs Verification" when a
    verification_required signal is unconfirmed (not_found).
    """
    rank = TIER_RANK["Bullseye"] if score >= bullseye_min else TIER_RANK["Watchlist"]
    signals = record.get("signals") or []

    for sig in signals:
        cap = sig.get("cap_tier")
        if cap and cap in TIER_RANK and sig.get("signal_state") == "yes":
            rank = min(rank, TIER_RANK[cap])

    needs_verification = any(
        sig.get("verification_required") and sig.get("signal_state") == "not_found"
        for sig in signals
    )
    if needs_verification:
        rank = min(rank, TIER_RANK["Needs Verification"])

    return _RANK_TO_TIER[rank]


def _specialty_words(text: str) -> set[str]:
    return set(re.findall(r'[a-z0-9]+', text.lower()))


def _specialty_matches(record_specialty: str, target_specialty: str) -> bool:
    """
    Return True if record_specialty matches any comma-separated token in target_specialty.

    Uses word-boundary tokenization so short tokens like "ENT" cannot accidentally
    match mid-word in strings like "urgent care" (where "ent" is a substring of "urgent").
    A target token matches when all its words appear as whole words in the record specialty.
    """
    rec_words = _specialty_words(record_specialty)
    if not rec_words:
        return False
    for token in target_specialty.split(","):
        tok_words = _specialty_words(token.strip())
        if tok_words and tok_words.issubset(rec_words):
            return True
    return False


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

    CLEAR records are assigned target_tier "Bullseye" or "Watchlist" only.
    target_tier = "Excluded" is set if and only if exclusion_status = "EXCLUDED".

    Args:
        record: Enriched record dict.
        run_config: Loaded run_config.json dict.

    Returns:
        Updated record.
    """
    active_rules = set(run_config.get("active_exclusion_rules", []))
    target_geography = run_config.get("target_geography", [])
    target_specialty = (run_config.get("target_specialty") or "").strip()
    bullseye_min = run_config.get("bullseye_min_score", 75)

    # Collect all triggered exclusions
    triggered = []
    rationale_parts = []

    # LLM-detected triggers from signal extraction step
    llm_triggers = set(record.get("_llm_exclusion_triggers") or [])
    llm_rationale = (record.get("_llm_exclusion_rationale") or "").strip()

    # --- Hard exclusions ---

    # wrong_specialty is deterministic — no LLM agreement required.
    # Fire only when the record specialty is known and does not match the target.
    # "Unknown" means detection failed, not a confirmed mismatch, so it is not a
    # hard exclusion on its own — let scoring and signals decide instead.
    record_specialty = (record.get("specialty") or "").strip()
    if target_specialty and record_specialty and record_specialty.lower() != "unknown":
        if not _specialty_matches(record_specialty, target_specialty):
            triggered.append("wrong_specialty")
            rationale_parts.append(
                f"Practice specialty '{record_specialty}' does not match "
                f"target specialty '{target_specialty}'."
            )

    # Geography check (pipeline-level)
    if target_geography and _check_geography(record, target_geography):
        triggered.append("outside_geography")
        state = record.get("address_state", "unknown")
        rationale_parts.append(
            f"Practice is in {state}, outside target geography "
            f"({', '.join(target_geography)})."
        )

    # LLM-detected hard exclusions (excluding wrong_specialty, which we handle above)
    hard_from_llm = llm_triggers & HARD_EXCLUSION_RULES - {"wrong_specialty", "outside_geography"}
    for trigger in sorted(hard_from_llm):
        if trigger not in triggered:
            triggered.append(trigger)

    if llm_rationale and hard_from_llm:
        rationale_parts.append(llm_rationale)

    # --- Configurable exclusions (only if active in run_config) ---
    configurable_from_llm = llm_triggers & CONFIGURABLE_EXCLUSION_RULES & active_rules
    for trigger in sorted(configurable_from_llm):
        if trigger not in triggered:
            triggered.append(trigger)

    if llm_rationale and configurable_from_llm and llm_rationale not in rationale_parts:
        rationale_parts.append(llm_rationale)

    # No-web-presence: deterministic pipeline-level check (configurable)
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
    score = record.get("bullseye_score", 0)

    if triggered:
        record["exclusion_status"] = "EXCLUDED"
        record["exclusion_reason"] = " | ".join(rationale_parts) if rationale_parts else (
            f"Exclusion rules triggered: {', '.join(triggered)}"
        )
        record["target_tier"] = "Excluded"

        # Cap score for excluded records
        if score > EXCLUDED_SCORE_CAP:
            record["bullseye_score"] = EXCLUDED_SCORE_CAP

        print(f"    [X] EXCLUDED: {', '.join(triggered)}")

    else:
        # CLEAR records are tiered by score, signal caps, and verification flags.
        # Never "Excluded" — that tier requires exclusion_status == "EXCLUDED".
        record["exclusion_status"] = "CLEAR"
        record["exclusion_reason"] = None
        record["target_tier"] = _assign_tier(record, score, bullseye_min)

    return record
