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
from enrichment.signal_extractor import _calculate_scores

_VALID_STATES = ("yes", "no", "not_found")
_VALID_CONFIDENCE = ("high", "medium", "low")


def simulate(icp_signals: list[dict], signal_states: dict, bullseye_min: int) -> dict:
    """Compute score + tier for a hypothetical signal outcome against an ICP.

    icp_signals carry the weights and tier flags; signal_states carry the
    operator's chosen outcome per signal. Signals with no chosen state default
    to not_found, matching how a real record scores an unobserved signal.
    """
    scoring_signals = []
    tier_signals = []
    for icp_signal in icp_signals:
        sid = icp_signal["signal_id"]
        chosen = signal_states.get(sid) or {}
        state = chosen.get("state", "not_found")
        if state not in _VALID_STATES:
            state = "not_found"
        confidence = chosen.get("confidence", "high")
        if confidence not in _VALID_CONFIDENCE:
            confidence = "high"

        scoring_signals.append({
            "signal_id": sid,
            "signal_state": state,
            "confidence": confidence,
            "state_inferred": False,
        })
        # Tiering reads the ICP flags off the signal object, so merge them in.
        tier_signals.append({
            "signal_id": sid,
            "signal_label": icp_signal.get("signal_label", sid),
            "signal_state": state,
            "state_inferred": False,
            "required_for_bullseye": icp_signal.get("required_for_bullseye", False),
            "verification_required": icp_signal.get("verification_required", False),
            "cap_tier": icp_signal.get("cap_tier"),
            "floor_tier": icp_signal.get("floor_tier"),
        })

    scores = _calculate_scores(scoring_signals, icp_signals)
    record = {
        "enrichment_status": "complete",
        "source_confidence": "full",
        "signals": tier_signals,
    }
    tier = _assign_tier(record, scores["bullseye_score"], bullseye_min)
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
