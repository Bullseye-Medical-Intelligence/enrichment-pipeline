"""
simulate_icp.py

Deterministic ICP scoring simulator. Given an ICP signal set and a hypothetical
per-signal outcome (yes / no / not_found), compute the resulting score and tier
without any crawl or LLM call. This lets an operator see how their weight and
flag choices behave while authoring an ICP profile.

Reads a JSON payload from stdin and writes a JSON result to stdout:

    in:  {
           "icp_signals": [ {signal_id, positive_weight, ...flags}, ... ],
           "signal_states": { "<signal_id>": {"state": "yes", "confidence": "high"}, ... },
           "bullseye_min": 90
         }
    out: {"bullseye_score": N, "fit_signal_score": N, "confidence_score": N,
          "tier": "...", "tier_cap_reason": "..."}

Scoring and tiering reuse the pipeline's own functions — there is one home for
that logic and this is a thin entry point onto it, never a reimplementation.
"""

import json
import sys

from enrichment.constants import DEFAULT_BULLSEYE_MIN_SCORE
from enrichment.exclusion_checker import _assign_tier
from enrichment.signal_extractor import _apply_reinforcement, _calculate_scores

_VALID_STATES = ("yes", "no", "not_found")
_VALID_CONFIDENCE = ("high", "medium", "low")


def simulate(icp_signals: list[dict], signal_states: dict, bullseye_min: int) -> dict:
    """Compute score + tier for a hypothetical signal outcome against an ICP.

    icp_signals carry the weights and tier flags; signal_states carry the
    operator's chosen outcome per signal. Signals with no chosen state default
    to not_found, matching how a real record scores an unobserved signal.
    """
    # Build a single merged signal list carrying both scoring fields (signal_state,
    # confidence, state_inferred) and tiering flags (required_for_bullseye, etc.)
    # so reinforcement, scoring, tiering, and exclusion all operate on the same
    # objects and state_inferred propagates correctly through all three passes.
    signals = []
    for icp_signal in icp_signals:
        sid = icp_signal["signal_id"]
        chosen = signal_states.get(sid) or {}
        state = chosen.get("state", "not_found")
        if state not in _VALID_STATES:
            state = "not_found"
        confidence = chosen.get("confidence", "high")
        if confidence not in _VALID_CONFIDENCE:
            confidence = "high"

        signals.append({
            "signal_id": sid,
            "signal_label": icp_signal.get("signal_label", sid),
            "signal_state": state,
            "confidence": confidence,
            "state_inferred": False,
            "required_for_bullseye": icp_signal.get("required_for_bullseye", False),
            "required_for_contender": icp_signal.get("required_for_contender", False),
            "verification_required": icp_signal.get("verification_required", False),
            "cap_tier": icp_signal.get("cap_tier"),
            "floor_tier": icp_signal.get("floor_tier"),
            "exclude_if_yes": bool(icp_signal.get("exclude_if_yes", False)),
            "inhibited_by": icp_signal.get("inhibited_by"),
        })

    # Reinforcement: a `reinforces` signal that is "yes" marks its not_found
    # target as state_inferred, granting partial fit credit and skipping
    # verification gates — must run before scoring and tiering.
    _apply_reinforcement(signals, icp_signals)

    scores = _calculate_scores(signals, icp_signals)
    record = {
        "enrichment_status": "complete",
        "source_confidence": "full",
        "signals": signals,
    }
    tier = _assign_tier(record, scores["bullseye_score"], bullseye_min)

    # exclude_if_yes overrides tier — mirrors apply_exclusions in the real pipeline.
    # inhibited_by: when the named signal is also "yes", the exclusion is suppressed.
    signal_states_map = {s["signal_id"]: s.get("signal_state") for s in signals}
    for sig in signals:
        if sig.get("exclude_if_yes") and sig.get("signal_state") == "yes":
            inhibitor = sig.get("inhibited_by")
            if inhibitor and signal_states_map.get(inhibitor) == "yes":
                continue
            label = sig.get("signal_label") or sig["signal_id"]
            return {
                "bullseye_score": scores["bullseye_score"],
                "fit_signal_score": scores["fit_signal_score"],
                "confidence_score": scores["confidence_score"],
                "tier": "Excluded",
                "tier_cap_reason": f"{label} confirmed present (immediate exclusion).",
            }

    return {
        "bullseye_score": scores["bullseye_score"],
        "fit_signal_score": scores["fit_signal_score"],
        "confidence_score": scores["confidence_score"],
        "tier": tier,
        "tier_cap_reason": record.get("tier_cap_reason", ""),
    }


def main() -> int:
    """Read a simulation payload from stdin, print the result as JSON to stdout."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid JSON payload: {exc}"}))
        return 1

    icp_signals = payload.get("icp_signals")
    if not isinstance(icp_signals, list) or not icp_signals:
        print(json.dumps({"error": "icp_signals must be a non-empty list"}))
        return 1
    signal_states = payload.get("signal_states") or {}
    if not isinstance(signal_states, dict):
        print(json.dumps({"error": "signal_states must be an object"}))
        return 1
    bullseye_min = payload.get("bullseye_min", DEFAULT_BULLSEYE_MIN_SCORE)

    result = simulate(icp_signals, signal_states, bullseye_min)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
