"""
verifier.py
GPT-based verification for Bullseye-tier records (bullseye_score >= 75).
Second-opinion quality gate — not a vote, not an override.
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
    """Build the verification prompt for a record."""
    template = _load_prompt_template()
    return template.format(
        practice_name=record.get("practice_name", "Unknown"),
        specialty=record.get("specialty", "Unknown"),
        address_city=record.get("address_city", ""),
        address_state=record.get("address_state", ""),
        website_url=record.get("website_url", ""),
        bullseye_score=record.get("bullseye_score", 0),
        fit_signal_score=record.get("fit_signal_score", 0),
        confidence_score=record.get("confidence_score", 0),
        primary_signals=_format_primary_signals(record.get("signals", [])),
        context_text=context_text or "(No website text available)",
    )


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
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an independent medical sales intelligence analyst "
                            "performing quality verification. Be rigorous and independent."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2048,
                temperature=0.2,
            )
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
        # No OpenAI key — log and skip verification
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
            # Models agree — enrichment_status unchanged (remains "complete")
            note = f"[GPT verification: AGREE. {overall_notes}]".strip()
            print(f"    ✓ Verification: AGREE")
        else:
            # Disagreement — flag for human review
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
            print(f"    ✗ Verification: DISAGREE — flagged needs_review")

        # Append to internal_notes
        existing = record.get("internal_notes") or ""
        record["internal_notes"] = f"{existing} {note}".strip()

    except (json.JSONDecodeError, ValueError) as e:
        note = f"[GPT verification parse error: {str(e)[:150]}]"
        existing = record.get("internal_notes") or ""
        record["internal_notes"] = f"{existing} {note}".strip()
        record["enrichment_status"] = "needs_review"
        print(f"    ✗ GPT response parse failed: {e}")

    except RuntimeError as e:
        note = f"[GPT verification API error: {str(e)[:150]}]"
        existing = record.get("internal_notes") or ""
        record["internal_notes"] = f"{existing} {note}".strip()
        print(f"    ✗ GPT API failed: {e}")
        # Don't change enrichment_status on API failure — not a disagreement

    return record
