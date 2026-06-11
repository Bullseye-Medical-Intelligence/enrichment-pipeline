"""
exclusion_checker.py
Applies exclusion rules to enriched records.
Hard exclusions always fire. Configurable exclusions fire only when listed in run_config.
"""

import re

from enrichment.constants import DEFAULT_BULLSEYE_MIN_SCORE, EXCLUDED_SCORE_CAP, LOW_SCORE_MANUAL_REVIEW_THRESHOLD

# ---------------------------------------------------------------------------
# Exclusion rule definitions
# ---------------------------------------------------------------------------

# Hard exclusions — always applied regardless of run_config
HARD_EXCLUSION_RULES = {
    "wrong_specialty",
    "outside_geography",
    "practice_closed",
    "academic_medical_center",
}

# Configurable exclusions — only applied when listed in active_exclusion_rules.
# Hospital affiliation rules are listed here so operators can downgrade them from
# auto-exclude to a tier cap (e.g. cap_tier: "Needs Verification" on the ICP signal)
# when they prefer to review affiliated practices rather than discard them.
CONFIGURABLE_EXCLUSION_RULES = {
    "hospital_owned",
    "health_system_affiliated",
    "no_web_presence",
    "competitor_conflict",
    "no_relevant_service_line",
}

ALL_KNOWN_EXCLUSION_RULES = HARD_EXCLUSION_RULES | CONFIGURABLE_EXCLUSION_RULES

# Tier ladder for CLEAR records (worst to best). A signal can cap the tier at a
# lower rung; an unconfirmed required signal caps a would-be Bullseye at
# "Needs Verification". "Excluded" only ever comes from an exclusion rule.
TIER_RANK = {"Excluded": 0, "Contender": 1, "Needs Verification": 2, "Bullseye": 3}
_RANK_TO_TIER = {rank: tier for tier, rank in TIER_RANK.items()}

# Legacy tier label -> current label. "Watchlist" was renamed to "Contender";
# frozen run snapshots and older signal cap_tier values may still carry it, so
# normalize defensively rather than silently dropping a cap.
_LEGACY_TIER_ALIAS = {"Watchlist": "Contender"}


def _canonical_tier(value: str) -> str:
    """Return the current tier label for a possibly-legacy tier string."""
    return _LEGACY_TIER_ALIAS.get(value, value)


def _signal_name(sig: dict) -> str:
    """Human-readable name for a signal in operator-facing cap explanations."""
    return sig.get("signal_label") or sig.get("signal_id") or "a required signal"


def _assign_tier(record: dict, score: int, bullseye_min: int) -> str:
    """Assign a CLEAR record's tier from its score, signal caps, and verification flags.

    Starts from the score-based tier, lets any "yes" signal with a cap_tier pull
    the ceiling down, then applies the must-have gate (required_for_bullseye) and
    the softer verification gate (verification_required).

    A record with zero confirmed evidence (no "yes", nothing inferred) is not a
    fit verdict at all — it gets "Manual Review", a CLEAR non-call status, so a
    blocked/empty crawl never reads as a Contender.

    A confirmed "yes" signal with floor_tier guarantees a minimum tier — it
    overrides the low-score Manual Review gate for that signal's own evidence.

    Side effect: writes `record["tier_cap_reason"]`, an operator-facing string
    explaining why the tier landed below Bullseye (which must-have signal failed,
    a cap_tier signal, thin crawl, etc.), or "" when the record is Bullseye or the
    tier was set purely by score. Tier and score now answer different questions —
    this field makes the gap legible without the operator reverse-engineering it.
    """
    signals = record.get("signals") or []
    record["tier_cap_reason"] = ""

    # Not-yet-enriched roster rows (ingest-only) have no signals by definition and
    # are not a Manual Review finding — skip the evidence gate for them.
    enriched = record.get("enrichment_status") != "not_enriched"
    floor_rank = -1
    if enriched:
        has_evidence = any(
            s.get("signal_state") == "yes" or s.get("state_inferred") for s in signals
        )
        # A confirmed signal with floor_tier guarantees at least that tier rank,
        # bypassing the low-score Manual Review gate for primary qualifying signals
        # (e.g. cash pay confirmed → always at least Contender).
        floor_rank = max(
            (TIER_RANK[_canonical_tier(s["floor_tier"])]
             for s in signals
             if s.get("floor_tier")
             and s.get("signal_state") == "yes"
             and _canonical_tier(s.get("floor_tier", "")) in TIER_RANK),
            default=-1,
        )
        if (not has_evidence or score < LOW_SCORE_MANUAL_REVIEW_THRESHOLD) and floor_rank < 0:
            record["tier_cap_reason"] = (
                "No confirmed signals on the site — manual review required before calling."
                if not has_evidence
                else f"Score {score} is below the {LOW_SCORE_MANUAL_REVIEW_THRESHOLD}-point "
                     "evidence floor — too thin to call without review."
            )
            return "Manual Review"

    # Bullseye requires BOTH the score threshold and the must-have gate below.
    # Confirming the must-have signals alone never promotes a low-scoring record:
    # with a single-must-have ICP, "lists the service" would otherwise make every
    # marginal practice a Bullseye regardless of how weak the rest of its fit is.
    rank = TIER_RANK["Bullseye"] if score >= bullseye_min else TIER_RANK["Contender"]
    # Apply floor guarantee: lift rank up to the floor minimum.
    if floor_rank > rank:
        rank = floor_rank

    # Collect every constraint that pulls the ceiling below Bullseye as (rank, reason).
    # The final tier is the lowest; the reasons explain it to the operator.
    caps: list[tuple[int, str]] = []

    for sig in signals:
        cap = _canonical_tier(sig.get("cap_tier"))
        if cap and cap in TIER_RANK and sig.get("signal_state") == "yes":
            caps.append((TIER_RANK[cap], f"{_signal_name(sig)} confirmed → capped at {cap}."))

    # Source confidence gate: a record whose website could not be reliably
    # crawled cannot be scored — send it to Manual Review so the operator
    # can trigger a browser re-crawl rather than auto-promoting to Contender.
    if enriched and record.get("source_confidence") in ("limited", "failed"):
        record["tier_cap_reason"] = (
            "Website could not be reliably crawled — re-crawl with browser or "
            "paste page content before calling."
        )
        return "Manual Review"

    # Must-have gate: a required_for_bullseye signal must be confirmed present
    # (or inferred) for Bullseye. Confirmed absent ("no") caps at Contender; an
    # unverified ("not_found") caps at Needs Verification. Inferred presence
    # (state_inferred) counts as confirmed and does not cap.
    for sig in signals:
        if not sig.get("required_for_bullseye") or sig.get("state_inferred"):
            continue
        state = sig.get("signal_state")
        if state == "no":
            caps.append((TIER_RANK["Contender"],
                         f"Must-have '{_signal_name(sig)}' confirmed absent → capped at Contender."))
        elif state == "not_found":
            caps.append((TIER_RANK["Needs Verification"],
                         f"Must-have '{_signal_name(sig)}' not found on the site → needs verification."))

    # A verification_required signal that is not confirmed forces verification —
    # unless its presence was inferred from a reinforcing signal (state_inferred),
    # in which case it counts as confirmed-by-inference and the gate does not fire.
    for sig in signals:
        if (sig.get("verification_required")
                and sig.get("signal_state") == "not_found"
                and not sig.get("state_inferred")):
            caps.append((TIER_RANK["Needs Verification"],
                         f"'{_signal_name(sig)}' needs verification before Bullseye."))

    final_rank = rank
    for cap_rank, _reason in caps:
        final_rank = min(final_rank, cap_rank)

    # Explain the gap to Bullseye: list every constraint that kept it down, most
    # severe first, including a score shortfall — the operator should never have
    # to reverse-engineer why a record landed below Bullseye.
    if final_rank < TIER_RANK["Bullseye"]:
        reasons = [reason for _cap_rank, reason in sorted(caps, key=lambda c: c[0])]
        if score < bullseye_min:
            reasons.append("Score is below the Bullseye threshold.")
        if reasons:
            record["tier_cap_reason"] = " ".join(reasons)

    return _RANK_TO_TIER[final_rank]


_SPECIALTY_PREFIX_LEN = 7


def _specialty_words(text: str) -> set[str]:
    return set(re.findall(r'[a-z0-9]+', text.lower()))


def _word_prefix_match(target_word: str, rec_words: set[str]) -> bool:
    """Return True if target_word exactly matches or shares a 7+ char prefix with any record word.

    Handles medical inflection variants: 'psychiatry'/'psychiatrist',
    'cardiology'/'cardiologist', 'neurology'/'neurologist', etc.
    Short words (<7 chars) require exact match only, preventing false prefix matches
    on common short tokens like 'care', 'pain', 'lab'.
    """
    if target_word in rec_words:
        return True
    if len(target_word) < _SPECIALTY_PREFIX_LEN:
        return False
    prefix = target_word[:_SPECIALTY_PREFIX_LEN]
    return any(
        len(rw) >= _SPECIALTY_PREFIX_LEN and rw[:_SPECIALTY_PREFIX_LEN] == prefix
        for rw in rec_words
    )


def _specialty_matches(record_specialty: str, target_specialty: str) -> bool:
    """Return True if record_specialty matches any comma-separated token in target_specialty.

    Uses word-boundary tokenization so short tokens like "ENT" cannot accidentally
    match mid-word in strings like "urgent care". A target token matches when every
    one of its words appears in the record specialty, either as an exact word or as a
    7+ character prefix match (covering inflection variants like psychiatry/psychiatrist).
    """
    rec_words = _specialty_words(record_specialty)
    if not rec_words:
        return False
    for token in target_specialty.split(","):
        tok_words = _specialty_words(token.strip())
        if tok_words and all(_word_prefix_match(tw, rec_words) for tw in tok_words):
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


def check_structural_exclusions(record: dict, run_config: dict) -> tuple[list[str], list[str]]:
    """Return (triggered, rationale_parts) for deterministic, signal-independent exclusions.

    Covers wrong_specialty, outside_geography, and any NPI taxonomy rules
    configured in run_config["taxonomy_exclusion_rules"]. All three are decided
    from data available before the crawl, enabling the structural pre-filter to
    route confirmed exclusions without spending crawl or LLM budget on them.
    apply_exclusions also calls this so the logic lives in one place.
    """
    target_geography = run_config.get("target_geography", [])
    target_specialty = (run_config.get("target_specialty") or "").strip()
    active_rules = set(run_config.get("active_exclusion_rules", []))
    triggered: list[str] = []
    rationale_parts: list[str] = []

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

    if target_geography and _check_geography(record, target_geography):
        triggered.append("outside_geography")
        state = record.get("address_state", "unknown")
        rationale_parts.append(
            f"Practice is in {state}, outside target geography "
            f"({', '.join(target_geography)})."
        )

    # NPI taxonomy structural gate: fire any rule whose taxonomy code was matched
    # by NPI enrichment and is listed in active_exclusion_rules. The mapping from
    # taxonomy code to rule name lives in run_config["taxonomy_exclusion_rules"],
    # not in the engine — each client cartridge defines which codes matter to them.
    for rule_name in record.get("_npi_taxonomy_exclusions", []):
        if rule_name in active_rules:
            triggered.append(rule_name)
            rationale_parts.append(
                f"Excluded via NPI taxonomy structural gate "
                f"(rule: {rule_name!r}) — excluded before crawl."
            )

    return triggered, rationale_parts


def apply_exclusions(record: dict, run_config: dict) -> dict:
    """
    Apply all active exclusion rules to a record.
    Sets exclusion_status, exclusion_reason, and target_tier.
    Caps bullseye_score for excluded records.

    CLEAR records are assigned target_tier "Bullseye", "Needs Verification", or
    "Contender" only — never "Excluded".
    target_tier = "Excluded" is set if and only if exclusion_status = "EXCLUDED".

    Args:
        record: Enriched record dict.
        run_config: Loaded run_config.json dict.

    Returns:
        Updated record.
    """
    # Customer suppression (Step 1c) takes precedence over all other rules.
    # The record was already matched against the client's existing-customer list
    # before enrichment; nothing downstream should re-classify it.
    if record.get("_customer_suppressed"):
        score = record.get("bullseye_score", 0)
        record["exclusion_status"] = "EXCLUDED"
        record["exclusion_reason"] = (
            record.get("_suppression_reason") or "Existing customer"
        )
        record["target_tier"] = "Excluded"
        if score > EXCLUDED_SCORE_CAP:
            record["bullseye_score"] = EXCLUDED_SCORE_CAP
        print("    [X] EXCLUDED: existing customer (suppression list)")
        return record

    active_rules = set(run_config.get("active_exclusion_rules", []))
    bullseye_min = run_config.get("bullseye_min_score", DEFAULT_BULLSEYE_MIN_SCORE)

    # LLM-detected triggers from signal extraction step
    llm_triggers = set(record.get("_llm_exclusion_triggers") or [])
    llm_rationale = (record.get("_llm_exclusion_rationale") or "").strip()

    # --- Hard exclusions ---

    # Deterministic structural exclusions (wrong_specialty, outside_geography).
    # Shared with the pipeline's pre-enrichment pre-filter so the logic lives once.
    triggered, rationale_parts = check_structural_exclusions(record, run_config)

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
        has_url = bool((record.get("website_url") or "").strip())
        context_text = (record.get("_context_text") or "").strip()
        if not has_url and not context_text:
            triggered.append("no_web_presence")
            rationale_parts.append(
                "No valid website URL and no public web presence detected."
            )

    # Signal-driven hard exclusion: any confirmed "yes" signal flagged
    # exclude_if_yes in the ICP profile is an immediate disqualifier (e.g.
    # telehealth-only). Generic — the engine never names the concept itself.
    # inhibited_by: when the named signal_id is also "yes", this exclusion is
    # suppressed — used for mutually-exclusive pairs where the companion
    # signal's "yes" logically invalidates this one.
    signal_states = {s.get("signal_id"): s.get("signal_state") for s in record.get("signals", [])}
    for sig in record.get("signals", []):
        if sig.get("exclude_if_yes") and sig.get("signal_state") == "yes":
            inhibitor = sig.get("inhibited_by")
            if inhibitor and signal_states.get(inhibitor) == "yes":
                continue
            rule = sig.get("signal_id") or "signal_exclusion"
            if rule not in triggered:
                triggered.append(rule)
                label = sig.get("signal_label") or rule
                rationale_parts.append(f"{label} confirmed present (immediate exclusion).")

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
