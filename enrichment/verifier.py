"""
verifier.py
GPT-based second pass: Bullseye verification + practice-specific sales brief.

Verification (Bullseye only): independent quality gate — not a vote, not an override.
Sales brief (all enriched CLEAR records): GPT generates practice-specific talking
points grounded in confirmed signals, replacing the generic Claude-generated angles.
"""

import json
import os
import time
from pathlib import Path

import openai
from dotenv import load_dotenv

load_dotenv()

PROMPT_VERSION = "verification_v1"
PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "verification_v1.txt"

_SALES_BRIEF_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "sales_brief_gpt_v1.txt"
_CONTEXT_EXCERPT_CHARS = 3000

# Per-call LLM timeout (seconds). Prevents a stalled socket from hanging a run.
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("LLM_REQUEST_TIMEOUT_SECONDS", "60"))


def _get_client() -> openai.OpenAI:
    """Return an initialized OpenAI client."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY not set in environment")
    return openai.OpenAI(api_key=api_key)


def _get_model() -> str:
    """Return the OpenAI model ID from environment."""
    return os.environ.get("OPENAI_MODEL", "gpt-4.1")


def _load_prompt_template() -> str:
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _format_primary_signals(signals: list[dict]) -> str:
    """Format signal list for insertion into the verification prompt."""
    lines = []
    for s in signals:
        lines.append(
            f"- [{s['signal_id']}] {s['signal_label']}: {s['signal_state'].upper()} "
            f"(confidence: {s['confidence']})\n"
            f"  Evidence: {s['evidence_text']}"
        )
    return "\n".join(lines) if lines else "(No signals returned by primary model)"


def _build_prompt(record: dict, context_text: str) -> str:
    """
    Build the verification prompt for a record.
    Uses explicit str.replace() instead of .format() so that JSON examples
    in the prompt template (which contain { and }) are left untouched.
    """
    template = _load_prompt_template()
    replacements = {
        "{practice_name}": record.get("practice_name", "Unknown"),
        "{specialty}": record.get("specialty", "Unknown"),
        "{address_city}": record.get("address_city", ""),
        "{address_state}": record.get("address_state", ""),
        "{website_url}": record.get("website_url", ""),
        "{bullseye_score}": str(record.get("bullseye_score", 0)),
        "{fit_signal_score}": str(record.get("fit_signal_score", 0)),
        "{confidence_score}": str(record.get("confidence_score", 0)),
        "{primary_signals}": _format_primary_signals(record.get("signals", [])),
        "{context_text}": context_text or "(No website text available)",
    }
    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, str(value))
    return result


def _call_gpt(prompt: str, client: openai.OpenAI, model: str,
               retries: int = 3) -> str:
    """Call GPT with retry logic. Returns raw text response."""
    last_error = None

    for attempt in range(retries + 1):
        if attempt > 0:
            wait = 2 ** attempt
            print(f"    GPT retry {attempt}/{retries} after {wait}s...")
            time.sleep(wait)

        try:
            # o-series reasoning models (o1, o3, o4-*) require max_completion_tokens
            # and do not accept temperature. Standard chat models accept both param
            # names; max_completion_tokens is the current preferred form.
            is_reasoning = model.startswith("o1") or model.startswith("o3") or model.startswith("o4")
            kwargs: dict = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are an independent medical sales intelligence analyst "
                            "performing quality verification. Be rigorous and independent."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_completion_tokens": 2048,
                "timeout": REQUEST_TIMEOUT_SECONDS,
            }
            if not is_reasoning:
                kwargs["temperature"] = 0.2
            response = client.chat.completions.create(**kwargs)
            return response.choices[0].message.content

        except openai.RateLimitError as e:
            last_error = f"Rate limit: {e}"
            time.sleep(15)
        except openai.APIStatusError as e:
            last_error = f"API error {e.status_code}: {e.message}"
            if e.status_code < 500:
                break
        except Exception as e:
            last_error = f"Unexpected: {str(e)[:100]}"

    raise RuntimeError(f"GPT API failed after {retries} retries: {last_error}")


def _parse_verification_response(raw: str) -> dict:
    """Parse GPT's JSON response into a structured dict."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    parsed = json.loads(text)

    required_keys = ["verification_result", "verifier_would_score_bullseye",
                      "signal_verifications"]
    for key in required_keys:
        if key not in parsed:
            raise ValueError(f"Verification response missing key: '{key}'")

    # Normalize verification_result
    result = str(parsed["verification_result"]).lower().strip()
    if result not in ("agree", "disagree"):
        parsed["verification_result"] = "disagree"  # Default to flagging

    return parsed


def verify_bullseye_record(record: dict, context_text: str) -> dict:
    """
    Run GPT verification for a single Bullseye-tier record.
    Mutates and returns the record with verification results.

    Rules:
    - If both agree: no change to enrichment_status
    - If GPT disagrees: set enrichment_status = "needs_review", document in internal_notes
    - Verification failure (API error): log in internal_notes, don't change status

    Args:
        record: Enriched canonical record (already scored by Claude).
        context_text: Original extracted website text.

    Returns:
        Updated record dict.
    """
    gpt_model = _get_model()

    try:
        client = _get_client()
    except EnvironmentError as e:
        # No OpenAI key - log and skip verification
        note = f"[Verification skipped: {e}]"
        record["internal_notes"] = f"{record.get('internal_notes', '')} {note}".strip()
        print(f"    Verification skipped: {e}")
        return record

    try:
        prompt = _build_prompt(record, context_text)
        print(f"    Calling GPT ({gpt_model}) for verification...")

        raw_response = _call_gpt(prompt, client, gpt_model)
        verification = _parse_verification_response(raw_response)

        verification_result = verification["verification_result"]
        verifier_scores_bullseye = verification["verifier_would_score_bullseye"]
        disagreements = verification.get("disagreements", [])
        overall_notes = verification.get("overall_notes", "")

        if verification_result == "agree" and verifier_scores_bullseye:
            # Models agree - enrichment_status unchanged (remains "complete")
            note = f"[GPT verification: AGREE. {overall_notes}]".strip()
            print("    [OK] Verification: AGREE")
        else:
            # Disagreement - flag for human review
            record["enrichment_status"] = "needs_review"
            disagreement_detail = "; ".join(
                f"{d.get('signal_id', '?')}: {d.get('verifier_note', '')}"
                for d in disagreements
            ) if disagreements else "GPT scored below Bullseye threshold"

            note = (
                f"[GPT verification: DISAGREE. "
                f"GPT would score Bullseye: {verifier_scores_bullseye}. "
                f"Disagreements: {disagreement_detail}. "
                f"Notes: {overall_notes}]"
            ).strip()
            print("    [FAIL] Verification: DISAGREE - flagged needs_review")

        # Append to internal_notes
        existing = record.get("internal_notes") or ""
        record["internal_notes"] = f"{existing} {note}".strip()

    except (json.JSONDecodeError, ValueError) as e:
        note = f"[GPT verification parse error: {str(e)[:150]}]"
        existing = record.get("internal_notes") or ""
        record["internal_notes"] = f"{existing} {note}".strip()
        record["enrichment_status"] = "needs_review"
        print(f"    [FAIL] GPT response parse failed: {e}")

    except RuntimeError as e:
        note = f"[GPT verification API error: {str(e)[:150]}]"
        existing = record.get("internal_notes") or ""
        record["internal_notes"] = f"{existing} {note}".strip()
        print(f"    [FAIL] GPT API failed: {e}")
        # Don't change enrichment_status on API failure - not a disagreement

    return record


# ---------------------------------------------------------------------------
# Practice-specific sales brief generation (all enriched CLEAR records)
# ---------------------------------------------------------------------------

def _format_confirmed_signals(signals: list[dict]) -> str:
    """Format confirmed positive signals for the sales brief prompt."""
    lines = [
        f"- {s['signal_label']}: {s.get('evidence_text', '').strip()}"
        for s in signals
        if s.get("signal_state") == "yes" and (s.get("positive_weight", 0) or 0) > 0
    ]
    return "\n".join(lines) if lines else "(none confirmed)"


def _format_friction_signals(signals: list[dict]) -> str:
    """Format confirmed friction/negative signals for the sales brief prompt."""
    lines = [
        f"- {s['signal_label']}: {s.get('evidence_text', '').strip()}"
        for s in signals
        if s.get("signal_state") == "yes" and (s.get("positive_weight", 0) or 0) <= 0
    ]
    return "\n".join(lines) if lines else "(none)"


def _build_sales_brief_prompt(record: dict, context_text: str, run_config: dict) -> str:
    """Build the practice-specific sales brief prompt."""
    template = _SALES_BRIEF_PROMPT_PATH.read_text(encoding="utf-8")
    signals = record.get("signals") or []
    replacements = {
        "{client_name}": run_config.get("client_name") or "the company",
        "{product_name}": run_config.get("product_name") or "the product",
        "{target_specialty}": run_config.get("target_specialty") or "the target specialty",
        "{practice_name}": record.get("practice_name", "Unknown"),
        "{specialty}": record.get("specialty", "Unknown"),
        "{address_city}": record.get("address_city", ""),
        "{address_state}": record.get("address_state", ""),
        "{website_url}": record.get("website_url", ""),
        "{confirmed_signals}": _format_confirmed_signals(signals),
        "{friction_signals}": _format_friction_signals(signals),
        "{context_excerpt}": (context_text or "")[:_CONTEXT_EXCERPT_CHARS],
    }
    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, str(value))
    return result


def _parse_sales_brief_response(raw: str) -> dict:
    """Parse GPT's sales brief JSON response."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
    parsed = json.loads(text)
    angles = parsed.get("sales_angle")
    if not isinstance(angles, list):
        raise ValueError("sales_brief response missing 'sales_angle' list")
    return parsed


def generate_sales_brief(record: dict, context_text: str, run_config: dict) -> dict:
    """Generate a practice-specific sales brief via GPT and update the record.

    Replaces the Claude-generated sales_angle with GPT-authored practice-specific
    talking points. Also updates call_brief.why_contact with the GPT summary.
    On API failure the existing Claude-generated content is preserved unchanged.

    Args:
        record: Enriched record (after Step 4 signal extraction).
        context_text: Extracted website text for the practice.
        run_config: Pipeline run configuration (provides product/client context).

    Returns:
        Updated record dict.
    """
    gpt_model = _get_model()
    try:
        client = _get_client()
    except EnvironmentError as e:
        print(f"    Sales brief skipped: {e}")
        return record

    try:
        prompt = _build_sales_brief_prompt(record, context_text, run_config)
        print(f"    Calling GPT ({gpt_model}) for sales brief...")
        raw_response = _call_gpt(prompt, client, gpt_model)
        parsed = _parse_sales_brief_response(raw_response)

        angles = [str(a).strip() for a in parsed["sales_angle"] if a]
        why = str(parsed.get("why_contact") or "").strip()

        # Only update if GPT returned substantive content.
        if angles:
            record["sales_angle"] = angles
        if why:
            brief = record.get("call_brief") or {}
            brief["why_contact"] = why
            record["call_brief"] = brief

        print(f"    [OK] Sales brief generated ({len(angles)} angles)")

    except (json.JSONDecodeError, ValueError) as e:
        print(f"    [FAIL] Sales brief parse failed: {e}")

    except RuntimeError as e:
        print(f"    [FAIL] Sales brief GPT error: {e}")

    return record
