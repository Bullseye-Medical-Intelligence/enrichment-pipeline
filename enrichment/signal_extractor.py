"""
signal_extractor.py
Calls the Claude API for signal extraction, scoring, and sales angle generation.
This is the primary LLM enrichment step — every record passes through here.
"""

import json
import os
import time
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROMPT_VERSION = "signal_extraction_v1"
PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "signal_extraction_v1.txt"

VALID_SIGNAL_STATES = {"yes", "no", "not_found"}
VALID_CONFIDENCES = {"high", "medium", "low"}

# Scoring parameters
MAX_SCORE = 100
MIN_SCORE = 0


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
    """Build the full signal extraction prompt for a record."""
    template = _load_prompt_template()
    checklist_text = _build_signal_checklist(icp_signals)

    return template.format(
        practice_name=record.get("practice_name", "Unknown"),
        specialty=record.get("specialty", "Unknown"),
        address_city=record.get("address_city", ""),
        address_state=record.get("address_state", ""),
        address_zip=record.get("address_zip", ""),
        website_url=record.get("website_url", ""),
        context_text=context_text or "(No website text available — limited public presence)",
        signal_checklist=checklist_text,
    )


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
    Validate and clean signal objects from LLM response.
    Enforces allowed values; fills defaults for missing fields.
    """
    # Build lookup of expected signals
    expected_ids = {s["signal_id"]: s for s in icp_signals}
    cleaned = []

    for sig in raw_signals:
        signal_id = sig.get("signal_id", "")
        signal_label = sig.get("signal_label", "")

        # Validate signal_state
        state = (sig.get("signal_state") or "not_found").lower().strip()
        if state not in VALID_SIGNAL_STATES:
            state = "not_found"

        # Validate confidence
        conf = (sig.get("confidence") or "low").lower().strip()
        if conf not in VALID_CONFIDENCES:
            conf = "low"

        cleaned.append({
            "signal_id": signal_id,
            "signal_label": signal_label,
            "signal_state": state,
            "evidence_text": (sig.get("evidence_text") or "").strip(),
            "source_url": (sig.get("source_url") or "").strip(),
            "source_type": "practice_website",
            "confidence": conf,
            "analyst_note": "",
        })

    return cleaned


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _calculate_scores(signals: list[dict], icp_signals: list[dict]) -> dict:
    """
    Calculate bullseye_score, fit_signal_score, and confidence_score.

    Scoring logic:
    - Start from a base of 50
    - Add/subtract weights based on signal_state ("yes" = full weight, "no" = 0, "not_found" = 0)
    - Negative-weight signals are subtracted when "yes"
    - confidence_score is the average confidence across confirmed signals
    - bullseye_score is a weighted average of fit and confidence
    """
    # Build weight lookup from ICP checklist
    weight_map = {s["signal_id"]: s.get("positive_weight", 0) for s in icp_signals}

    # Map signals by ID
    signal_map = {s["signal_id"]: s for s in signals}

    fit_delta = 0
    confidence_values = []
    conf_score_map = {"high": 90, "medium": 65, "low": 40}

    for icp_signal in icp_signals:
        sid = icp_signal["signal_id"]
        weight = weight_map.get(sid, 0)
        matched = signal_map.get(sid)

        if matched and matched["signal_state"] == "yes":
            if weight > 0:
                fit_delta += weight
            else:
                fit_delta += weight  # negative weight subtracted
            confidence_values.append(conf_score_map.get(matched["confidence"], 40))

    # Fit signal score: 50 base + weighted delta, clamped 0-100
    fit_signal_score = max(MIN_SCORE, min(MAX_SCORE, 50 + fit_delta))

    # Confidence score: average of confidence values for confirmed signals
    if confidence_values:
        confidence_score = round(sum(confidence_values) / len(confidence_values))
    else:
        confidence_score = 30  # Low confidence when no signals confirmed

    # Bullseye score: 60% fit + 40% confidence
    bullseye_score = round(0.6 * fit_signal_score + 0.4 * confidence_score)
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
    high_fit = bullseye_score >= 70
    high_confidence = confidence_score >= 65

    if high_fit and high_confidence:
        return "HIGH FIT / HIGH EVIDENCE"
    elif high_fit and not high_confidence:
        return "HIGH FIT / LOW EVIDENCE"
    elif not high_fit and high_confidence:
        return "LOW FIT / HIGH EVIDENCE"
    else:
        return "LOW FIT / LOW EVIDENCE"


def _determine_target_tier(bullseye_score: int,
                             exclusion_status: str,
                             bullseye_min_score: int = 75) -> str:
    """Determine target_tier from score and exclusion status."""
    if exclusion_status == "EXCLUDED":
        return "Excluded"
    if bullseye_score >= bullseye_min_score:
        return "Bullseye"
    if bullseye_score >= 50:
        return "Watchlist"
    return "Excluded"


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

        print(f"    ✓ Bullseye: {bullseye_score} | Fit: {fit_signal_score} | Confidence: {confidence_score}")

    except json.JSONDecodeError as e:
        print(f"    ✗ JSON parse failure: {e}")
        record.update({
            "signals": [],
            "bullseye_score": 0,
            "fit_signal_score": 0,
            "confidence_score": 0,
            "fit_confidence_status": "LOW FIT / LOW EVIDENCE",
            "sales_angle": [],
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
        print(f"    ✗ Claude API failure: {e}")
        record.update({
            "signals": [],
            "bullseye_score": 0,
            "fit_signal_score": 0,
            "confidence_score": 0,
            "fit_confidence_status": "LOW FIT / LOW EVIDENCE",
            "sales_angle": [],
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
