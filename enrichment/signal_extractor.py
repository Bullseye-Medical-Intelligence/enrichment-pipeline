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
    DEFAULT_BULLSEYE_MIN_SCORE,
    FIT_WEIGHT,
    HIGH_CONFIDENCE_THRESHOLD,
    HIGH_FIT_THRESHOLD,
    INFERENCE_CREDIT,
    LLM_MAX_TOKENS,
    MAX_SCORE,
    MIN_CONTEXT_CHARS,
    MIN_SCORE,
    NO_SIGNAL_CONFIDENCE,
    SIGNAL_CONFIDENCE_CREDIT,
    empty_call_brief,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROMPT_VERSION = "signal_extraction_v3"
_SYSTEM_TEMPLATE_PATH = Path(__file__).parent.parent / "prompts" / "signal_extraction_system_v3.txt"
_USER_TEMPLATE_PATH = Path(__file__).parent.parent / "prompts" / "signal_extraction_user_v3.txt"
_SYSTEM_TEMPLATE: str = _SYSTEM_TEMPLATE_PATH.read_text(encoding="utf-8")
_USER_TEMPLATE: str = _USER_TEMPLATE_PATH.read_text(encoding="utf-8")

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

def _build_signal_checklist(signals: list[dict]) -> str:
    """Format ICP signal definitions for insertion into the prompt."""
    lines = []
    for s in signals:
        if s.get("source_type") == "static_lookup":
            print(
                f"    [WARN] Signal {s['signal_id']!r} has source_type=static_lookup, "
                "which is not yet implemented. It will be evaluated against website "
                "text instead. Remove source_type from the ICP profile to silence this warning."
            )
        note = f" [{s['note']}]" if s.get("note") else ""
        lines.append(
            f"- signal_id: {s['signal_id']}\n"
            f"  signal_label: {s['signal_label']}\n"
            f"  instruction: {s['prompt_instruction']}{note}"
        )
    return "\n\n".join(lines)


def _build_system_prompt(icp_signals: list[dict]) -> str:
    """Build the cacheable system prompt — identical for every record in a run."""
    return _SYSTEM_TEMPLATE.replace("{signal_checklist}", _build_signal_checklist(icp_signals))


def _build_user_message(record: dict, context_text: str) -> str:
    """Build the per-record user message containing practice info and website text."""
    replacements = {
        "{practice_name}": record.get("practice_name", "Unknown"),
        "{specialty}": record.get("specialty", "Unknown"),
        "{address_city}": record.get("address_city", ""),
        "{address_state}": record.get("address_state", ""),
        "{address_zip}": record.get("address_zip", ""),
        "{website_url}": record.get("website_url", ""),
        "{context_text}": context_text or "(No website text available — limited public presence)",
    }
    result = _USER_TEMPLATE
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, str(value))
    return result


# ---------------------------------------------------------------------------
# LLM call with retry
# ---------------------------------------------------------------------------

def _call_claude(system_prompt: str, user_message: str,
                  client: anthropic.Anthropic, model: str,
                  retries: int = 3) -> str:
    """Call Claude with a cached system prompt and per-record user message.

    The system prompt is marked ephemeral so Claude caches it across all
    records in a run — the signal checklist and instructions are identical
    per run, so only the first call pays full input token cost for that block.
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
                max_tokens=LLM_MAX_TOKENS,
                timeout=REQUEST_TIMEOUT_SECONDS,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": user_message,
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
    Falls back to json_repair for malformed output (unescaped newlines/quotes in
    evidence_text are the most common LLM JSON bug).
    Raises ValueError if JSON is missing required keys after repair.
    """
    # Strip markdown code blocks if present
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        from json_repair import repair_json
        parsed = json.loads(repair_json(text))

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
            "required_for_bullseye": bool(icp_by_id[signal_id].get("required_for_bullseye", False)),
            "cap_tier": icp_by_id[signal_id].get("cap_tier", ""),
            "exclude_if_yes": bool(icp_by_id[signal_id].get("exclude_if_yes", False)),
            "state_inferred": False,
            "inferred_from": "",
            "not_found_reason": "",
            "analyst_note": "",
        }

    # Enforce sourcing requirement: a "yes" must have both evidence text and a
    # source URL traceable to the crawled pages. Without these anchors the claim
    # is unverifiable — downgrade to not_found so it does not inflate the score.
    for sig in validated_map.values():
        if sig["signal_state"] == "yes":
            has_evidence = bool(sig.get("evidence_text", "").strip())
            has_source = bool(sig.get("source_url", "").strip())
            if not has_evidence or not has_source:
                sig["not_found_reason"] = "evidence_gate"
                sig["signal_state"] = "not_found"
                sig["confidence"] = "low"
                # Clear the partial/unverifiable claim so stale evidence cannot
                # render in the UI or client PDF for a signal now marked not_found.
                sig["evidence_text"] = ""
                sig["source_url"] = ""

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
                "required_for_bullseye": bool(icp_sig.get("required_for_bullseye", False)),
                "cap_tier": icp_sig.get("cap_tier", ""),
                "exclude_if_yes": bool(icp_sig.get("exclude_if_yes", False)),
                "state_inferred": False,
                "inferred_from": "",
                "not_found_reason": "",
                "analyst_note": "",
            })

    return normalized


def _no_context_reason(record: dict, context_text: str) -> str:
    """Human-readable reason a record reached signal extraction with no usable text.

    Distinguishes the failure modes an operator otherwise can't tell apart from a
    bare score of 0: no URL at all, URL that failed validation (with the HTTP/
    network error), URL valid but the crawler got blocked or returned nothing,
    or a site so thin it fell under the minimum-context bar.
    """
    url = (record.get("website_url") or "").strip()
    url_error = (record.get("_url_error") or "").strip()
    chars = len((context_text or "").strip())

    if not url:
        return "No website URL provided — nothing to crawl."
    if url_error:
        return (f"Website could not be reached: {url_error}. "
                "Try 'Re-crawl with Browser' or paste the page content manually.")
    if chars == 0:
        return ("Website returned no readable text — likely a bot/security wall or "
                "a script-rendered page. Try 'Re-crawl with Browser' or paste the "
                "page content manually.")
    return (f"Website returned only {chars} characters of text (under the minimum "
            f"needed to evaluate). Paste fuller page content manually to enrich.")


def _build_empty_signals(icp_signals: list[dict]) -> list[dict]:
    """Return one not_found/low-confidence signal per ICP signal.

    Used when there is no meaningful website text — avoids sending an empty
    context to the LLM which would produce hallucinated signal states.
    """
    return [
        {
            "signal_id": icp_sig["signal_id"],
            "signal_label": icp_sig["signal_label"],
            "signal_state": "not_found",
            "evidence_text": "",
            "source_url": "",
            "source_type": "practice_website",
            "confidence": "low",
            "positive_weight": icp_sig.get("positive_weight", 0),
            "verification_required": bool(icp_sig.get("verification_required", False)),
            "required_for_bullseye": bool(icp_sig.get("required_for_bullseye", False)),
            "cap_tier": icp_sig.get("cap_tier", ""),
            "exclude_if_yes": bool(icp_sig.get("exclude_if_yes", False)),
            "state_inferred": False,
            "inferred_from": "",
            "not_found_reason": "no_context",
            "analyst_note": "",
        }
        for icp_sig in icp_signals
    ]


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
            target["inferred_from"] = icp_sig["signal_id"]


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
    - A confirmed-absent ("no") desirable signal applies its no_weight penalty
      (usually 0 or negative) — a missing must-have costs points, not just credit.
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
        no_weight = icp_signal.get("no_weight", 0)
        matched = signal_map.get(sid)
        state = matched["signal_state"] if matched else "not_found"
        inferred = bool(matched and matched.get("state_inferred"))

        if weight >= 0:
            max_positive += weight
            if state == "yes":
                credit = SIGNAL_CONFIDENCE_CREDIT.get(
                    matched.get("confidence", "low"), SIGNAL_CONFIDENCE_CREDIT["low"]
                )
                achieved += weight * credit
                confidence_values.append(
                    CONFIDENCE_SCORE_MAP.get(matched["confidence"], CONFIDENCE_SCORE_MAP["low"])
                )
            elif state == "not_found":
                if inferred:
                    achieved += weight * INFERENCE_CREDIT
                    confidence_values.append(CONFIDENCE_SCORE_MAP["medium"])
                else:
                    achieved += not_found_weight  # penalty (usually <= 0)
            elif state == "no":
                achieved += no_weight  # confirmed absent — penalty (usually <= 0), default 0
        else:
            # Friction signal: only a confirmed "yes" applies the negative weight,
            # scaled by confidence so a low-confidence friction claim has less impact.
            if state == "yes":
                credit = SIGNAL_CONFIDENCE_CREDIT.get(
                    matched.get("confidence", "low"), SIGNAL_CONFIDENCE_CREDIT["low"]
                )
                achieved += weight * credit  # weight is negative -> subtracts
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


def _determine_fit_confidence_status(fit_signal_score: int,
                                      confidence_score: int) -> str:
    """
    Determine fit_confidence_status from the two independent scoring dimensions.
    Returns one of the four canonical quadrant labels.

    Both axes are read independently — never from the blended bullseye_score.
    HIGH_FIT_THRESHOLD (70) applies to fit_signal_score (procedure match).
    HIGH_CONFIDENCE_THRESHOLD (65) applies to confidence_score (evidence quality).
    Reading from the blend would collapse HIGH FIT / LOW EVIDENCE into LOW FIT
    for exactly the records this quadrant is designed to surface.
    """
    high_fit = fit_signal_score >= HIGH_FIT_THRESHOLD
    high_evidence = confidence_score >= HIGH_CONFIDENCE_THRESHOLD

    if high_fit and high_evidence:
        return "HIGH FIT / HIGH EVIDENCE"
    elif high_fit and not high_evidence:
        return "HIGH FIT / LOW EVIDENCE"
    elif not high_fit and high_evidence:
        return "LOW FIT / HIGH EVIDENCE"
    else:
        return "LOW FIT / LOW EVIDENCE"


# ---------------------------------------------------------------------------
# Rep call brief
# ---------------------------------------------------------------------------

def _parse_providers(raw: list) -> tuple[list[dict], list[str]]:
    """Validate LLM-returned providers and format provider_names strings.

    Returns (validated_providers, provider_names) where validated_providers is
    a clean list of {name, title} dicts and provider_names is a list of
    formatted strings like "Dr. Jane Smith, MD".
    """
    if not isinstance(raw, list):
        return [], []
    validated = []
    for entry in raw[:8]:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        title = (entry.get("title") or "").strip()
        validated.append({"name": name, "title": title})
    provider_names = [
        f"{p['name']}, {p['title']}" if p["title"] else p["name"]
        for p in validated
    ]
    return validated, provider_names


def _parse_primary_contact(raw: dict | None, providers: list[dict]) -> dict | None:
    """Validate the LLM's primary_contact pick against the extracted providers list.

    Returns a {name, title, reason} dict if the pick is valid, falls back to
    providers[0] if the pick is missing or unrecognisable, returns None when
    no providers were found at all.
    """
    if not providers:
        return None
    if isinstance(raw, dict):
        name = (raw.get("name") or "").strip()
        title = (raw.get("title") or "").strip()
        reason = (raw.get("reason") or "").strip()
        # Verify the name appears in the providers list (case-insensitive substring).
        name_lower = name.lower()
        if name and any(name_lower in p["name"].lower() for p in providers):
            return {"name": name, "title": title, "reason": reason}
    # Fall back to first provider with no reason.
    first = providers[0]
    return {"name": first["name"], "title": first["title"], "reason": ""}


def _format_key_contact(primary: dict | None) -> str:
    """Format a rep-friendly 'Ask for ...' string from the primary contact."""
    if not primary:
        return ""
    name = primary.get("name") or ""
    reason = primary.get("reason") or ""
    if not name:
        return ""
    return f"Ask for {name} — {reason}" if reason else f"Ask for {name}"


def _build_call_brief(signals: list[dict], scores: dict, record: dict,
                       generated: dict, primary_contact: dict | None = None) -> dict:
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

    # Integrity gate: if no signals survived as confirmed "yes", the LLM-generated
    # claim-based prep lines (opening_line, objection, discovery question) reference
    # nothing verifiable and must be cleared so a rep cannot open with a fabricated
    # claim. hours_of_operation is factual (office hours stated on the site) and
    # kept regardless of signal state.
    if not top_evidence:
        generated = {"hours_of_operation": generated.get("hours_of_operation", "")}

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
        "key_contact": _format_key_contact(primary_contact),
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
                     bullseye_min_score: int = DEFAULT_BULLSEYE_MIN_SCORE) -> dict:
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
    model = _get_model()
    raw_response = None

    try:
        # Short-circuit: skip the LLM call when there is no meaningful website
        # text. Sending an empty context to Claude produces hallucinated signals;
        # returning all-not_found is more accurate and cheaper.
        if len(context_text or "") < MIN_CONTEXT_CHARS:
            signals = _build_empty_signals(icp_signals)
            _apply_reinforcement(signals, icp_signals)
            scores = _calculate_scores(signals, icp_signals)
            record.update({
                "signals": signals,
                "bullseye_score": scores["bullseye_score"],
                "fit_signal_score": scores["fit_signal_score"],
                "confidence_score": scores["confidence_score"],
                "fit_confidence_status": _determine_fit_confidence_status(
                    scores["fit_signal_score"], scores["confidence_score"]
                ),
                "sales_angle": [],
                "call_brief": empty_call_brief(),
                "source_confidence": "limited",
                "date_enriched": date.today().isoformat(),
                "enrichment_run_id": run_id,
                "llm_model_used": "",
                "llm_prompt_version": PROMPT_VERSION,
                "enrichment_status": "partial",
                "qc_status": "pending",
                "analyst_override_classification": None,
                "override_reason": None,
                # Record-level reason the operator sees: a score of 0 with no
                # signals is otherwise unexplained. Surfaces the actual crawl
                # outcome so "active site, no data" is no longer a mystery.
                "internal_notes": _no_context_reason(record, context_text),
                "client_facing_rationale": None,
                "_llm_exclusion_triggers": [],
                "_llm_exclusion_rationale": "",
            })
            print("    [SKIP] No website text — all signals set to not_found (no LLM call)")
            return record

        client = _get_client()
        system_prompt = _build_system_prompt(icp_signals)
        user_message = _build_user_message(record, context_text)
        print(f"    Calling Claude ({model}) for signal extraction...")

        raw_response = _call_claude(system_prompt, user_message, client, model)
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
            fit_signal_score, confidence_score
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

        # Provider extraction
        validated_providers, provider_names = _parse_providers(parsed.get("providers", []))
        primary_contact = _parse_primary_contact(parsed.get("primary_contact"), validated_providers)

        # Rep call brief: grounded fields derived from signals, prep lines from LLM
        generated_brief = parsed.get("call_brief") or {}
        if not isinstance(generated_brief, dict):
            generated_brief = {}
        call_brief = _build_call_brief(signals, scores, record, generated_brief, primary_contact)

        # Gate sales_angle: only serve bullets when confirmed evidence exists.
        # When top_evidence is empty, the integrity gate already cleared the
        # opening_line — sales_angle bullets would have the same grounding problem.
        if not call_brief.get("top_evidence"):
            sales_angle = []

        # Source confidence
        if context_text:
            source_confidence = record.get("source_confidence") or "partial"
        else:
            source_confidence = "limited"

        # When the crawl succeeded but no signal was confirmed, the record has no
        # fit evidence and will land in Manual Review — record why so the operator
        # sees an explanation instead of a bare score of 0.
        no_evidence_note = (
            "" if call_brief.get("top_evidence")
            else "Site was crawled but no ICP signals were confirmed — needs manual review."
        )

        # Update record
        record.update({
            "signals": signals,
            "bullseye_score": bullseye_score,
            "fit_signal_score": fit_signal_score,
            "confidence_score": confidence_score,
            "fit_confidence_status": fit_confidence_status,
            "sales_angle": sales_angle,
            "call_brief": call_brief,
            "provider_names": provider_names,
            "source_confidence": source_confidence,
            "date_enriched": date.today().isoformat(),
            "enrichment_run_id": run_id,
            "llm_model_used": model,
            "llm_prompt_version": PROMPT_VERSION,
            "enrichment_status": "complete",
            "qc_status": "pending",
            "analyst_override_classification": None,
            "override_reason": None,
            "internal_notes": no_evidence_note,
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
