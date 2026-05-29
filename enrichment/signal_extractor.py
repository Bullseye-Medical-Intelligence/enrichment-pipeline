"""
signal_extractor.py
Calls the Claude API for signal extraction, scoring, and sales angle generation.
This is the primary LLM enrichment step — every record passes through here.

_validate_and_clean_signals() normalizes the LLM response to exactly match the
configured ICP signal set:
- Unknown signal_ids (not in icp_signals) are discarded.
- Missing signals (in icp_signals but not in LLM response) are inserted with
  signal_state="not_found", confidence="low", empty evidence/source fields.
- Output list always has exactly len(icp_signals) entries in icp_signals order.
"""

import json
import os
import time
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from enrichment.constants import (
    BASE_FIT_SCORE,
    CONFIDENCE_SCORE_MAP,
    CONFIDENCE_WEIGHT,
    FIT_WEIGHT,
    HIGH_CONFIDENCE_THRESHOLD,
    HIGH_FIT_THRESHOLD,
    INFERENCE_CREDIT,
    MAX_SCORE,
    MIN_SCORE,
    NO_SIGNAL_CONFIDENCE,
    empty_call_brief,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROMPT_VERSION = "signal_extraction_v2"
PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "signal_extraction_v2.txt"

# Number of confirmed signals surfaced in the call brief's evidence list.
TOP_EVIDENCE_COUNT = 3

VALID_SIGNAL_STATES = {"yes", "no", "not_found"}
VALID_CONFIDENCES = {"high", "medium", "low"}

# Per-call LLM timeout (seconds). Prevents a stalled socket from hanging a run.
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("LLM_REQUEST_TIMEOUT_SECONDS", "60"))


# ---------------------------------------------------------------------------
# Client initialization
# ---------------------------------------------------------------------------

def _get_client() -> anthropic.Anthropic:
    """Return an initialized Anthropic client."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set in environment")
    return anthropic.Anthropic(api_key=api_key)


def _get_model() -> str:
    """Return the Claude model ID from environment."""
    return os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _load_prompt_template() -> str:
    """Load the signal extraction prompt template from file."""
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _build_signal_checklist(signals: list[dict]) -> str:
    """Format ICP signal definitions for insertion into the prompt."""
    lines = []
    for s in signals:
        note = f" [{s['note']}]" if s.get("note") else ""
        lines.append(
            f"- signal_id: {s['signal_id']}\n"
            f"  signal_label: {s['signal_label']}\n"
            f"  instruction: {s['prompt_instruction']}{note}"
        )
    return "\n\n".join(lines)


def _build_prompt(record: dict, context_text: str, icp_signals: list[dict]) -> str:
    """
    Build the full signal extraction prompt for a record.
    Uses explicit str.replace() instead of .format() so that JSON examples
    in the prompt template (which contain { and }) are left untouched.
    """
    template = _load_prompt_template()
    checklist_text = _build_signal_checklist(icp_signals)

    replacements = {
        "{practice_name}": record.get("practice_name", "Unknown"),
        "{specialty}": record.get("specialty", "Unknown"),
        "{address_city}": record.get("address_city", ""),
        "{address_state}": record.get("address_state", ""),
        "{address_zip}": record.get("address_zip", ""),
        "{website_url}": record.get("website_url", ""),
        "{context_text}": context_text or "(No website text available — limited public presence)",
        "{signal_checklist}": checklist_text,
    }
    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, str(value))
    return result


# ---------------------------------------------------------------------------
# LLM call with retry
# ---------------------------------------------------------------------------

def _call_claude(prompt: str, client: anthropic.Anthropic, model: str,
                  retries: int = 3) -> str:
    """
    Call Claude with retry logic.
    Returns the raw text response or raises on all retries exhausted.
    """
    last_error = None

    for attempt in range(retries + 1):
        if attempt > 0:
            wait = 2 ** attempt
            print(f"    Retry {attempt}/{retries} after {wait}s...")
            time.sleep(wait)

        try:
            message = client.messages.create(
                model=model,
                max_tokens=4096,
                timeout=REQUEST_TIMEOUT_SECONDS,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
            )
            return message.content[0].text

        except anthropic.RateLimitError as e:
            last_error = f"Rate limit: {e}"
            time.sleep(10)  # Extra wait on rate limit
        except anthropic.APIStatusError as e:
            last_error = f"API error {e.status_code}: {e.message}"
            if e.status_code < 500:
                break  # Don't retry 4xx
        except Exception as e:
            last_error = f"Unexpected error: {str(e)[:100]}"

    raise RuntimeError(f"Claude API failed after {retries} retries: {last_error}")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> dict:
    """
    Parse Claude's JSON response into a structured dict.
    Raises ValueError if JSON is malformed or missing required keys.
    """
    # Strip markdown code blocks if present
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    parsed = json.loads(text)

    # Validate structure
    if "signals" not in parsed:
        raise ValueError("Response missing 'signals' key")
    if "sales_angle" not in parsed:
        raise ValueError("Response missing 'sales_angle' key")

    return parsed


def _validate_and_clean_signals(raw_signals: list[dict],
                                  icp_signals: list[dict]) -> list[dict]:
    """
    Validate and normalize LLM signal output against the configured ICP signal set.

    Steps:
    1. Parse and validate each LLM-returned signal (state, confidence values).
    2. Discard any signal whose signal_id is not in icp_signals (phantom/invented).
    3. For every signal_id in icp_signals: use the LLM result if present, otherwise
       insert a default not_found entry.
    4. Return exactly len(icp_signals) entries in icp_signals order.
    """
    # Build lookup of configured signals by ID
    icp_by_id = {s["signal_id"]: s for s in icp_signals}

    # Parse and validate raw LLM output, keyed by signal_id.
    # Discard entries with unknown IDs.
    validated_map: dict[str, dict] = {}
    for sig in raw_signals:
        signal_id = sig.get("signal_id", "")
        if signal_id not in icp_by_id:
            continue  # Discard unknown/invented signal IDs

        # Coerce to str before .lower() — LLM may return JSON true/false (bool)
        raw_state = sig.get("signal_state")
        state = str(raw_state).lower().strip() if raw_state is not None else "not_found"
        if state not in VALID_SIGNAL_STATES:
            state = "not_found"

        conf = (sig.get("confidence") or "low").lower().strip()
        if conf not in VALID_CONFIDENCES:
            conf = "low"

        validated_map[signal_id] = {
            "signal_id": signal_id,
            "signal_label": icp_by_id[signal_id]["signal_label"],
            "signal_state": state,
            "evidence_text": (sig.get("evidence_text") or "").strip(),
            "source_url": (sig.get("source_url") or "").strip(),
            "source_type": "practice_website",
            "confidence": conf,
            "positive_weight": icp_by_id[signal_id].get("positive_weight", 0),
            "verification_required": bool(icp_by_id[signal_id].get("verification_required", False)),
            "cap_tier": icp_by_id[signal_id].get("cap_tier", ""),
            "state_inferred": False,
            "analyst_note": "",
        }

    # Build final list in icp_signals order, inserting defaults for any omitted signal
    normalized = []
    for icp_sig in icp_signals:
        sid = icp_sig["signal_id"]
        if sid in validated_map:
            normalized.append(validated_map[sid])
        else:
            normalized.append({
                "signal_id": sid,
                "signal_label": icp_sig["signal_label"],
                "signal_state": "not_found",
                "evidence_text": "",
                "source_url": "",
                "source_type": "practice_website",
                "confidence": "low",
                "positive_weight": icp_sig.get("positive_weight", 0),
                "verification_required": bool(icp_sig.get("verification_required", False)),
                "cap_tier": icp_sig.get("cap_tier", ""),
                "state_inferred": False,
                "analyst_note": "",
            })

    return normalized


def _apply_reinforcement(signals: list[dict], icp_signals: list[dict]) -> None:
    """Mark a not_found signal as inferred when a reinforcing signal is confirmed.

    An ICP signal may declare `reinforces: "<other_signal_id>"`. When that
    reinforcing signal resolves to "yes" while its target is "not_found", the
    target's presence is inferred indirectly — for example, listed elective or
    cosmetic procedures imply cash pay even when the words "cash pay" never
    appear on the site. Sets state_inferred=True on the target so scoring grants
    partial credit and tiering skips the verification gate. Mutates in place.
    """
    by_id = {s["signal_id"]: s for s in signals}
    for icp_sig in icp_signals:
        target_id = icp_sig.get("reinforces")
        if not target_id:
            continue
        source = by_id.get(icp_sig["signal_id"])
        target = by_id.get(target_id)
        if (source and target
                and source["signal_state"] == "yes"
                and target["signal_state"] == "not_found"):
            target["state_inferred"] = True


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _calculate_scores(signals: list[dict], icp_signals: list[dict]) -> dict:
    """
    Calculate fit_signal_score, confidence_score, and bullseye_score as a
    commercial-fit confidence reading dictated by the ICP.

    fit_signal_score is the share of the *achievable* positive weight a practice
    actually captures, expressed 0–100. Matching every desirable signal lands
    near 100; matching only minor ones earns proportionally less, so a long tail
    of low-value signals can never out-score the few that matter. Logic:

    - max_positive = sum of all positive (desirable) signal weights — the ideal.
    - A confirmed ("yes") desirable signal adds its full weight to achieved.
    - An inferred desirable signal (state_inferred, set by reinforcement) adds
      INFERENCE_CREDIT of its weight — partial credit for indirect evidence.
    - An unconfirmed ("not_found") desirable signal applies its not_found_weight
      penalty (usually 0 or negative).
    - A confirmed friction signal (negative weight, "yes") subtracts its weight.
    - confidence_score is the mean confidence across confirmed/inferred signals.
    - bullseye_score is the weighted blend of fit and confidence.
    """
    signal_map = {s["signal_id"]: s for s in signals}

    max_positive = 0.0
    achieved = 0.0
    confidence_values = []

    for icp_signal in icp_signals:
        sid = icp_signal["signal_id"]
        weight = icp_signal.get("positive_weight", 0)
        not_found_weight = icp_signal.get("not_found_weight", 0)
        matched = signal_map.get(sid)
        state = matched["signal_state"] if matched else "not_found"
        inferred = bool(matched and matched.get("state_inferred"))

        if weight >= 0:
            max_positive += weight
            if state == "yes":
                achieved += weight
                confidence_values.append(
                    CONFIDENCE_SCORE_MAP.get(matched["confidence"], CONFIDENCE_SCORE_MAP["low"])
                )
            elif state == "not_found":
                if inferred:
                    achieved += weight * INFERENCE_CREDIT
                    confidence_values.append(CONFIDENCE_SCORE_MAP["medium"])
                else:
                    achieved += not_found_weight  # penalty (usually <= 0)
            # "no": confirmed absent — no credit
        else:
            # Friction signal: only a confirmed "yes" applies the negative weight.
            if state == "yes":
                achieved += weight  # weight is negative -> subtracts
                confidence_values.append(
                    CONFIDENCE_SCORE_MAP.get(matched["confidence"], CONFIDENCE_SCORE_MAP["low"])
                )

    # Fit = captured share of the ideal profile, scaled to 0–100.
    if max_positive > 0:
        fit_signal_score = round((achieved / max_positive) * 100)
    else:
        fit_signal_score = BASE_FIT_SCORE  # no positive signals defined -> neutral
    fit_signal_score = max(MIN_SCORE, min(MAX_SCORE, fit_signal_score))

    # Confidence score: average of confidence values for confirmed/inferred signals
    if confidence_values:
        confidence_score = round(sum(confidence_values) / len(confidence_values))
    else:
        confidence_score = NO_SIGNAL_CONFIDENCE

    # Bullseye score: weighted blend of fit and confidence
    bullseye_score = round(FIT_WEIGHT * fit_signal_score + CONFIDENCE_WEIGHT * confidence_score)
    bullseye_score = max(MIN_SCORE, min(MAX_SCORE, bullseye_score))

    return {
        "bullseye_score": bullseye_score,
        "fit_signal_score": fit_signal_score,
        "confidence_score": confidence_score,
    }


def _determine_fit_confidence_status(bullseye_score: int,
                                      confidence_score: int) -> str:
    """
    Determine fit_confidence_status from scores.
    Returns one of the four canonical quadrant labels.
    """
    high_fit = bullseye_score >= HIGH_FIT_THRESHOLD
    high_confidence = confidence_score >= HIGH_CONFIDENCE_THRESHOLD

    if high_fit and high_confidence:
        return "HIGH FIT / HIGH EVIDENCE"
    elif high_fit and not high_confidence:
        return "HIGH FIT / LOW EVIDENCE"
    elif not high_fit and high_confidence:
        return "LOW FIT / HIGH EVIDENCE"
    else:
        return "LOW FIT / LOW EVIDENCE"


# ---------------------------------------------------------------------------
# Rep call brief
# ---------------------------------------------------------------------------

def _build_call_brief(signals: list[dict], scores: dict, record: dict,
                       generated: dict) -> dict:
    """
    Assemble a rep call brief from already-validated signals and scores.

    Grounded fields (top_evidence, missing_to_verify, disqualifier_risk,
    why_contact) are derived from the signals; the three prep lines
    (opening_line, likely_objection, discovery_question) come from the LLM and
    are passed in via `generated`. No new scoring or LLM call happens here.
    """
    confirmed_positive = [
        s for s in signals
        if s.get("signal_state") == "yes" and s.get("positive_weight", 0) > 0
    ]
    confirmed_positive.sort(key=lambda s: s.get("positive_weight", 0), reverse=True)

    # top_evidence: highest-weight confirmed signals that carry evidence text.
    top_evidence = [
        {
            "point": s.get("signal_label", ""),
            "evidence": s.get("evidence_text", ""),
            "source_url": s.get("source_url", ""),
        }
        for s in confirmed_positive if s.get("evidence_text")
    ][:TOP_EVIDENCE_COUNT]

    # missing_to_verify: unconfirmed required signals not covered by inference
    # (mirrors the verification gate in exclusion_checker._assign_tier).
    missing_to_verify = [
        s.get("signal_label", "")
        for s in signals
        if s.get("verification_required")
        and s.get("signal_state") == "not_found"
        and not s.get("state_inferred")
    ]

    # disqualifier_risk: confirmed friction signals + any confirmed cap_tier signal.
    disqualifier_risk = []
    for s in signals:
        if s.get("signal_state") != "yes":
            continue
        label = s.get("signal_label", "")
        if s.get("positive_weight", 0) < 0:
            disqualifier_risk.append(f"{label} (friction signal present)")
        elif s.get("cap_tier"):
            disqualifier_risk.append(f"{label} (caps tier at {s['cap_tier']})")

    # why_contact: grounded one-liner from the top confirmed signals + fit.
    specialty = (record.get("specialty") or "").strip()
    lead = " + ".join(s.get("signal_label", "") for s in confirmed_positive[:2])
    fit = scores.get("fit_signal_score", 0)
    if lead:
        why_contact = f"{specialty + ' ' if specialty else ''}practice: {lead} (fit {fit}).".strip()
    else:
        why_contact = f"{specialty + ' ' if specialty else ''}practice, no confirmed positive signals yet (fit {fit}).".strip()

    brief = empty_call_brief()
    brief.update({
        "why_contact": why_contact,
        "opening_line": str(generated.get("opening_line") or "").strip(),
        "likely_objection": str(generated.get("likely_objection") or "").strip(),
        "discovery_question": str(generated.get("discovery_question") or "").strip(),
        "hours_of_operation": str(generated.get("hours_of_operation") or "").strip(),
        "top_evidence": top_evidence,
        "missing_to_verify": missing_to_verify,
        "disqualifier_risk": disqualifier_risk,
    })
    return brief


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract_signals(record: dict, icp_signals: list[dict],
                     context_text: str, run_id: str,
                     bullseye_min_score: int = 75) -> dict:
    """
    Run signal extraction for a single record via Claude.
    Populates all enrichment fields on the record and returns it.

    Args:
        record: Canonical record dict (mutated in-place and returned).
        icp_signals: List of ICP signal definitions from icp_checklist.json.
        context_text: Extracted website text for this practice.
        run_id: Current pipeline run ID for tracking.
        bullseye_min_score: Minimum score for Bullseye tier.

    Returns:
        Enriched record dict.
    """
    client = _get_client()
    model = _get_model()
    raw_response = None

    try:
        prompt = _build_prompt(record, context_text, icp_signals)
        print(f"    Calling Claude ({model}) for signal extraction...")

        raw_response = _call_claude(prompt, client, model)
        parsed = _parse_response(raw_response)

        # Validate and clean signals
        signals = _validate_and_clean_signals(
            parsed.get("signals", []), icp_signals
        )

        # Infer not_found signals from confirmed reinforcing signals
        # (e.g. elective procedures imply cash pay) before scoring/tiering.
        _apply_reinforcement(signals, icp_signals)

        # Score
        scores = _calculate_scores(signals, icp_signals)
        bullseye_score = scores["bullseye_score"]
        fit_signal_score = scores["fit_signal_score"]
        confidence_score = scores["confidence_score"]

        fit_confidence_status = _determine_fit_confidence_status(
            bullseye_score, confidence_score
        )

        # Sales angle
        sales_angle = parsed.get("sales_angle", [])
        if isinstance(sales_angle, list):
            sales_angle = [str(p).strip() for p in sales_angle if p]
        else:
            sales_angle = []

        # Exclusion triggers from LLM
        exclusion_triggers = parsed.get("exclusion_triggers", [])
        exclusion_rationale = parsed.get("exclusion_rationale", "")

        # Rep call brief: grounded fields derived from signals, prep lines from LLM
        generated_brief = parsed.get("call_brief") or {}
        if not isinstance(generated_brief, dict):
            generated_brief = {}
        call_brief = _build_call_brief(signals, scores, record, generated_brief)

        # Source confidence
        if context_text:
            source_confidence = record.get("source_confidence") or "partial"
        else:
            source_confidence = "limited"

        # Update record
        record.update({
            "signals": signals,
            "bullseye_score": bullseye_score,
            "fit_signal_score": fit_signal_score,
            "confidence_score": confidence_score,
            "fit_confidence_status": fit_confidence_status,
            "sales_angle": sales_angle,
            "call_brief": call_brief,
            "source_confidence": source_confidence,
            "date_enriched": date.today().isoformat(),
            "enrichment_run_id": run_id,
            "llm_model_used": model,
            "llm_prompt_version": PROMPT_VERSION,
            "enrichment_status": "complete",
            "qc_status": "pending",
            "analyst_override_classification": None,
            "override_reason": None,
            "internal_notes": "",
            "client_facing_rationale": None,
            # Store LLM-detected exclusion triggers for Step 6
            "_llm_exclusion_triggers": exclusion_triggers,
            "_llm_exclusion_rationale": exclusion_rationale,
        })

        print(f"    [OK] Bullseye: {bullseye_score} | Fit: {fit_signal_score} | Confidence: {confidence_score}")

    except json.JSONDecodeError as e:
        print(f"    [FAIL] JSON parse failure: {e}")
        record.update({
            "signals": [],
            "bullseye_score": 0,
            "fit_signal_score": 0,
            "confidence_score": 0,
            "fit_confidence_status": "LOW FIT / LOW EVIDENCE",
            "sales_angle": [],
            "call_brief": empty_call_brief(),
            "source_confidence": record.get("source_confidence") or "failed",
            "date_enriched": date.today().isoformat(),
            "enrichment_run_id": run_id,
            "llm_model_used": model,
            "llm_prompt_version": PROMPT_VERSION,
            "enrichment_status": "needs_review",
            "qc_status": "pending",
            "analyst_override_classification": None,
            "override_reason": None,
            "internal_notes": f"LLM response parse failed: {str(e)[:200]}. Raw: {(raw_response or '')[:500]}",
            "client_facing_rationale": None,
            "_llm_exclusion_triggers": [],
            "_llm_exclusion_rationale": "",
        })

    except RuntimeError as e:
        print(f"    [FAIL] Claude API failure: {e}")
        record.update({
            "signals": [],
            "bullseye_score": 0,
            "fit_signal_score": 0,
            "confidence_score": 0,
            "fit_confidence_status": "LOW FIT / LOW EVIDENCE",
            "sales_angle": [],
            "call_brief": empty_call_brief(),
            "source_confidence": record.get("source_confidence") or "failed",
            "date_enriched": date.today().isoformat(),
            "enrichment_run_id": run_id,
            "llm_model_used": model,
            "llm_prompt_version": PROMPT_VERSION,
            "enrichment_status": "failed",
            "qc_status": "pending",
            "analyst_override_classification": None,
            "override_reason": None,
            "internal_notes": f"Claude API error: {str(e)[:300]}",
            "client_facing_rationale": None,
            "_llm_exclusion_triggers": [],
            "_llm_exclusion_rationale": "",
        })

    return record
