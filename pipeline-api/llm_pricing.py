"""
llm_pricing.py
The single home for LLM pricing constants used by the cost-per-run display.

Rates are per million tokens, in USD, maintained MANUALLY by the operator —
never fetched from the network. When Anthropic pricing changes, update the
rates and LAST_VERIFIED here; the UI shows LAST_VERIFIED next to every cost
figure so a stale rate is visible, not silent.

The figure is an ESTIMATE: input totals include prompt-cache creation and
read tokens, which bill at different rates than fresh input. The estimate
prices everything at the fresh-input rate, so it slightly overstates real
spend — a conservative ceiling, never an understatement.
"""

import json
from pathlib import Path

PRICED_MODEL = "claude-sonnet-4-6"
INPUT_USD_PER_MTOK = 3.00
OUTPUT_USD_PER_MTOK = 15.00
LAST_VERIFIED = "2026-06-10"

_MTOK = 1_000_000


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Estimated USD cost for the given token totals at the constant rates."""
    return (
        (input_tokens / _MTOK) * INPUT_USD_PER_MTOK
        + (output_tokens / _MTOK) * OUTPUT_USD_PER_MTOK
    )


_DEFAULT_INPUT_TOKENS_PER_RECORD = 6_000   # conservative fallback when no run history
_DEFAULT_OUTPUT_TOKENS_PER_RECORD = 750    # conservative fallback
_MAX_HISTORY_RUNS = 20                     # cap on past runs used for the average


def estimate_run_cost(record_count: int, runs_path: "str | Path") -> dict:
    """Estimate Claude signal extraction cost for a prospective enrichment run.

    Scans past completed runs in runs_path for per-record token averages. Falls
    back to conservative defaults when no history is available. Covers Step 4
    (Claude signal extraction) only — crawl and URL checks have no LLM cost.

    Returns a dict with estimate fields suitable for the enrich-estimate endpoint.
    """
    runs_dir = Path(runs_path)
    input_tok_samples: list[float] = []
    output_tok_samples: list[float] = []

    if runs_dir.exists():
        candidates = sorted(
            (e for e in runs_dir.iterdir() if e.is_dir()),
            key=lambda e: e.name,
            reverse=True,
        )
        for entry in candidates:
            status_file = entry / "status.json"
            if not status_file.exists():
                continue
            try:
                with open(status_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("status") != "complete":
                    continue
                if data.get("run_type") == "discovery":
                    continue
                output_records = data.get("records_output") or 0
                in_tok = data.get("llm_input_tokens")
                out_tok = data.get("llm_output_tokens")
                if in_tok and out_tok and output_records > 0:
                    input_tok_samples.append(in_tok / output_records)
                    output_tok_samples.append(out_tok / output_records)
                    if len(input_tok_samples) >= _MAX_HISTORY_RUNS:
                        break
            except (json.JSONDecodeError, KeyError, ValueError, OSError):
                continue

    if input_tok_samples:
        avg_input = sum(input_tok_samples) / len(input_tok_samples)
        avg_output = sum(output_tok_samples) / len(output_tok_samples)
        history_run_count = len(input_tok_samples)
        using_defaults = False
    else:
        avg_input = _DEFAULT_INPUT_TOKENS_PER_RECORD
        avg_output = _DEFAULT_OUTPUT_TOKENS_PER_RECORD
        history_run_count = 0
        using_defaults = True

    est_input = int(avg_input * record_count)
    est_output = int(avg_output * record_count)
    est_cost = estimate_cost_usd(est_input, est_output)

    return {
        "record_count": record_count,
        "estimated_input_tokens": est_input,
        "estimated_output_tokens": est_output,
        "estimated_cost_usd": round(est_cost, 4),
        "avg_input_tokens_per_record": round(avg_input),
        "avg_output_tokens_per_record": round(avg_output),
        "history_run_count": history_run_count,
        "using_defaults": using_defaults,
        "priced_model": PRICED_MODEL,
        "rates_as_of": LAST_VERIFIED,
    }


def cost_summary(status) -> dict | None:
    """Build the run-summary cost block from a RunStatus, or None when the
    run predates token capture (llm_call_count is None)."""
    if status.llm_call_count is None:
        return None
    input_tokens = status.llm_input_tokens or 0
    output_tokens = status.llm_output_tokens or 0
    total_cost = estimate_cost_usd(input_tokens, output_tokens)
    records = status.records_output or 0
    return {
        "llm_calls": status.llm_call_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(total_cost, 4),
        "cost_per_record_usd": round(total_cost / records, 4) if records else None,
        "priced_model": PRICED_MODEL,
        "rates_as_of": LAST_VERIFIED,
    }
