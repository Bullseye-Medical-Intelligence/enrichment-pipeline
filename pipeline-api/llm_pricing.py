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
