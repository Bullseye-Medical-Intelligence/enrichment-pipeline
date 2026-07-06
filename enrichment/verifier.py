"""
enrichment/verifier.py
Post-run Needs Verification pass.

Operates on a completed run's enriched_targets.json. Targets only records
in the Needs Verification tier. Two phases per record:

  a. Anchor-check (free): confirm each "yes" signal's evidence_text appears
     verbatim (normalized whitespace+case) in the page text. _context_text is
     stripped from output, so it is rehydrated from the Evidence Vault first.
     Anchor-failed signals are demoted to not_found. Records with any
     anchor failure skip GPT — compromised evidence is not worth re-checking.

  b. Blind GPT re-extraction (survivors): feed GPT the raw page text + ICP
     signal definitions only. No Claude verdicts shown. Get independent
     per-signal verdicts and determine recommended_action.

Results are written as an additive "verification" object on each record.
Original signals/scores/tier are never overwritten.

Idempotent: records with an existing verification.verified_at are skipped.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import openai
from dotenv import load_dotenv

from output.evidence_writer import read_record_context_text

load_dotenv()

REQUEST_TIMEOUT_SECONDS = int(os.environ.get("LLM_REQUEST_TIMEOUT_SECONDS", "60"))
_MAX_CONTEXT_CHARS = 25000  # trim context before sending to GPT; matches the
# Evidence Vault read budget so GPT re-extraction sees the same text the anchor
# check did (a lower cap hid gating evidence on later subpages and biased to hold)


def _get_client() -> openai.OpenAI:
    """Return an initialized OpenAI client."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY not set in environment")
    return openai.OpenAI(api_key=api_key)


def _get_model() -> str:
    """Return the OpenAI model ID from environment."""
    return os.environ.get("OPENAI_MODEL", "gpt-5.5")


def _normalize(text: str) -> str:
    """Lowercase and collapse all whitespace to single spaces for anchor comparison."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _anchor_check(evidence_text: str, context_text: str) -> bool:
    """Return True if evidence_text appears (normalized) within context_text."""
    if not evidence_text or not context_text:
        return False
    return _normalize(evidence_text) in _normalize(context_text)


def _find_gating_signal_ids(record: dict, icp_signals: list[dict]) -> list[str]:
    """Return signal_ids whose not_found state caused the Needs Verification tier cap.

    A gating signal is one that is required_for_bullseye or verification_required,
    is not_found on the record, and is not state_inferred.
    """
    record_signal_map = {
        s.get("signal_id"): s
        for s in (record.get("signals") or [])
    }
    gating = []
    for icp_sig in icp_signals:
        sid = icp_sig.get("signal_id")
        if not sid:
            continue
        if not (icp_sig.get("required_for_bullseye") or icp_sig.get("verification_required")):
            continue
        rec_sig = record_signal_map.get(sid, {})
        if rec_sig.get("signal_state") == "not_found" and not rec_sig.get("state_inferred"):
            gating.append(sid)
    return gating


def _format_icp_signal_definitions(icp_signals: list[dict]) -> str:
    """Format ICP signal definitions for the blind extraction prompt."""
    lines = []
    for s in icp_signals:
        sid = s.get("signal_id", "?")
        label = s.get("signal_label", "?")
        instruction = s.get("prompt_instruction", "")
        exclude = " [EXCLUDE IF CONFIRMED YES]" if s.get("exclude_if_yes") else ""
        lines.append(f"- [{sid}] {label}{exclude}: {instruction}")
    return "\n".join(lines) if lines else "(no signals defined)"


def _build_blind_extraction_prompt(record: dict, context_text: str, icp_signals: list[dict]) -> str:
    """Build the blind GPT re-extraction prompt. No Claude verdicts included."""
    trimmed = context_text[:_MAX_CONTEXT_CHARS]
    if len(context_text) > _MAX_CONTEXT_CHARS:
        trimmed += "\n\n[... content trimmed for token budget ...]"
    signal_defs = _format_icp_signal_definitions(icp_signals)
    return f"""You are an independent medical practice intelligence analyst performing a fresh evaluation.

Your task: determine whether each ICP signal below is present at this practice based ONLY on the website text provided. Do not infer from context outside this text. Do not assume anything not stated.

Practice: {record.get("practice_name", "Unknown")}
Website: {record.get("website_url", "")}

--- WEBSITE TEXT ---
{trimmed}
--- END WEBSITE TEXT ---

ICP Signals to evaluate:
{signal_defs}

ATTRIBUTION, NOT MENTION: mark a signal "yes" ONLY when the website text attributes the capability to THIS practice as a current, offered service — a services/treatments/procedures listing, or an explicit "we offer / we perform / available at our office" statement. Text that merely MENTIONS the topic is NOT "yes": a provider's personal biography, interests, training, or own medical history ("has a deep interest in...", "conceived through IVF"); an educational, condition-explainer, blog, or news page; a statement that patients are referred elsewhere; a patient testimonial; or historical/aspirational language ("previously offered", "coming soon"). If the best evidence you can find is one of those, the signal is "not_found", never "yes". Absence of attribution is "not_found", not "no".

For each signal return:
- signal_id (string, exact as listed above)
- signal_state: "yes" if the text attributes the service to this practice (see ATTRIBUTION rule above), "no" if explicitly contradicted, "not_found" if unclear, absent, or only mentioned
- confidence: "high" if verbatim quote, "medium" if clearly implied, "low" if weak/indirect (only when signal_state is "yes")
- evidence_text: exact short quote from the website text supporting a "yes" verdict (only when signal_state is "yes", empty string otherwise)

Return a JSON object with no other text:
{{
  "signal_verdicts": [
    {{"signal_id": "...", "signal_state": "yes"|"no"|"not_found", "confidence": "high"|"medium"|"low"|null, "evidence_text": "..."}}
  ],
  "notes": ""
}}"""


def _call_gpt(prompt: str, retries: int = 3) -> str:
    """Call GPT with retry logic. Returns raw text response."""
    client = _get_client()
    model = _get_model()
    last_error = None

    for attempt in range(retries + 1):
        if attempt > 0:
            wait = 2 ** attempt
            print(f"    GPT retry {attempt}/{retries} after {wait}s…")
            time.sleep(wait)
        try:
            # o1/o3/o4 and the GPT-5 family are reasoning models that reject a
            # custom temperature (400: "Only the default (1) value is supported"),
            # so we omit it for them; older chat models still get the low temperature.
            is_reasoning = model.startswith(("o1", "o3", "o4", "gpt-5"))
            kwargs: dict = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are an independent medical practice intelligence analyst. Return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                "max_completion_tokens": 2048,
                "timeout": REQUEST_TIMEOUT_SECONDS,
            }
            if not is_reasoning:
                kwargs["temperature"] = 0.1
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


def _parse_blind_extraction_response(raw: str) -> dict:
    """Parse GPT's blind extraction JSON response."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    parsed = json.loads(text)
    if "signal_verdicts" not in parsed:
        raise ValueError("Blind extraction response missing 'signal_verdicts'")
    return parsed


def _verify_record(record: dict, icp_signals: list[dict]) -> dict:
    """Run the full anchor + GPT pass on one Needs Verification record.

    Returns the record with a new `verification` object added.
    Never modifies signals, target_tier, or score fields.
    """
    context_text = record.get("_context_text") or ""
    record_signals = record.get("signals") or []
    model = _get_model()
    now = datetime.now(timezone.utc).isoformat()

    per_signal_verdicts = []
    anchor_failures = []

    # --- Phase a: anchor-check all yes-signals ---
    for sig in record_signals:
        if sig.get("signal_state") != "yes":
            continue
        evidence = (sig.get("evidence_text") or "").strip()
        found = _anchor_check(evidence, context_text)
        verdict = {
            "signal_id": sig.get("signal_id"),
            "anchor_found": found,
            "anchor_failed": not found,
            "gpt_verdict": None,
            "gpt_confidence": None,
            "gpt_evidence": None,
        }
        per_signal_verdicts.append(verdict)
        if not found:
            anchor_failures.append(sig.get("signal_id"))

    if anchor_failures:
        return {
            **record,
            "verification": {
                "verified_at": now,
                "verifier_model": model,
                "method": "anchor",
                "per_signal_verdicts": per_signal_verdicts,
                "anchor_failures": anchor_failures,
                "recommended_action": "hold",
                "notes": (
                    f"Anchor-check failed for {len(anchor_failures)} signal(s): "
                    f"{', '.join(anchor_failures)}. GPT skipped — compromised evidence."
                ),
            },
        }

    # --- Phase b: blind GPT re-extraction ---
    gating_ids = _find_gating_signal_ids(record, icp_signals)
    if not gating_ids:
        # No identifiable gating signal — nothing to promote
        return {
            **record,
            "verification": {
                "verified_at": now,
                "verifier_model": model,
                "method": "anchor",
                "per_signal_verdicts": per_signal_verdicts,
                "anchor_failures": [],
                "recommended_action": "hold",
                "notes": "No unconfirmed gating signals identified. No GPT call made.",
            },
        }

    prompt = _build_blind_extraction_prompt(record, context_text, icp_signals)
    print(f"    Blind GPT re-extraction ({model})…")
    raw_response = _call_gpt(prompt)
    parsed = _parse_blind_extraction_response(raw_response)

    gpt_verdicts = {v["signal_id"]: v for v in parsed.get("signal_verdicts", [])}
    exclude_if_yes_ids = {s.get("signal_id") for s in icp_signals if s.get("exclude_if_yes")}

    # Update per_signal_verdicts with GPT results
    all_verdict_ids = {v["signal_id"] for v in per_signal_verdicts}
    for sig_id, gv in gpt_verdicts.items():
        if sig_id in all_verdict_ids:
            for v in per_signal_verdicts:
                if v["signal_id"] == sig_id:
                    v["gpt_verdict"] = gv.get("signal_state")
                    v["gpt_confidence"] = gv.get("confidence")
                    v["gpt_evidence"] = gv.get("evidence_text") or ""
        else:
            per_signal_verdicts.append({
                "signal_id": sig_id,
                "anchor_found": None,
                "anchor_failed": False,
                "gpt_verdict": gv.get("signal_state"),
                "gpt_confidence": gv.get("confidence"),
                "gpt_evidence": gv.get("evidence_text") or "",
            })

    # Determine recommended_action
    # Disqualify if any exclude_if_yes signal confirmed yes by GPT
    disqualifying = [
        sid for sid in exclude_if_yes_ids
        if gpt_verdicts.get(sid, {}).get("signal_state") == "yes"
    ]
    if disqualifying:
        recommended_action = "disqualify"
        notes = f"GPT confirmed exclusion signal(s): {', '.join(disqualifying)}."
    elif all(gpt_verdicts.get(sid, {}).get("signal_state") == "yes" for sid in gating_ids):
        recommended_action = "promote"
        notes = (
            f"GPT independently confirmed gating signal(s): {', '.join(gating_ids)}. "
            f"Operator confirmation required before client export."
        )
    else:
        still_missing = [
            sid for sid in gating_ids
            if gpt_verdicts.get(sid, {}).get("signal_state") != "yes"
        ]
        recommended_action = "hold"
        notes = f"GPT did not confirm gating signal(s): {', '.join(still_missing)}."

    return {
        **record,
        "verification": {
            "verified_at": now,
            "verifier_model": model,
            "method": "anchor+gpt",
            "per_signal_verdicts": per_signal_verdicts,
            "anchor_failures": [],
            "recommended_action": recommended_action,
            "notes": notes,
        },
    }


def run_verification_pass(run_dir: Path, icp_signals: list[dict]) -> dict:
    """Run the post-run Needs Verification pass on a completed run.

    Reads enriched_targets.json, verifies eligible records, writes the file
    back atomically. Returns counts: {promoted, held, disqualified, skipped, errors}.

    Eligible: target_tier == "Needs Verification", enrichment_status == "complete",
              no existing verification.verified_at (idempotent).
    """
    targets_path = run_dir / "enriched_targets.json"
    if not targets_path.exists():
        raise FileNotFoundError(f"enriched_targets.json not found in {run_dir}")

    with open(targets_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    if isinstance(payload, dict):
        records = payload.get("records", [])
    else:
        records = payload

    stats = {"promoted": 0, "held": 0, "disqualified": 0, "skipped": 0, "errors": 0}

    for i, record in enumerate(records):
        if record.get("target_tier") != "Needs Verification":
            stats["skipped"] += 1
            continue
        if record.get("enrichment_status") != "complete":
            stats["skipped"] += 1
            continue
        if record.get("verification", {}).get("verified_at"):
            stats["skipped"] += 1
            continue

        name = record.get("practice_name", f"record[{i}]")
        print(f"\n  [VER] {name}")
        # _context_text is stripped from enriched_targets.json at output time;
        # rehydrate it from the Evidence Vault so anchor-check + blind GPT have
        # the page text the crawler actually saw.
        if not (record.get("_context_text") or "").strip():
            record["_context_text"] = read_record_context_text(run_dir, record.get("id", ""))
        try:
            record = _verify_record(record, icp_signals)
            action = record["verification"]["recommended_action"]
            print(f"    → {action}")
            if action == "promote":
                stats["promoted"] += 1
            elif action == "disqualify":
                stats["disqualified"] += 1
            else:
                stats["held"] += 1
        except EnvironmentError as e:
            print(f"    [SKIP] {e}")
            stats["skipped"] += 1
        except Exception as e:
            print(f"    [ERROR] {str(e)[:150]}")
            stats["errors"] += 1

        # Drop any rehydrated internal context before persisting — _context_text
        # is never part of the output schema.
        record.pop("_context_text", None)
        records[i] = record

    # Atomic write
    if isinstance(payload, dict):
        payload["records"] = records
        out = payload
    else:
        out = records

    tmp = targets_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    os.replace(tmp, targets_path)

    return stats
