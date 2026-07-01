"""
rescore_run.py — CLI entry point for the ICP re-scoring post-run pass.

Usage:
    python rescore_run.py --run-dir output/runs/<id> [--icp config/.../icp_checklist.json]

Loads an existing completed run's enriched_targets.json (signals already extracted),
applies updated ICP weights by re-running Steps 6-7 only (exclusion check + scoring
validation), then writes results back atomically. Zero LLM cost, runs in seconds.

Signals (signal_state, confidence, evidence_text, state_inferred, inferred_from) are
never modified — only scores and tiers change.

Reads credentials from pipeline-api/.env when present.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

# Load .env from pipeline-api/ when running from repo root
_env_path = Path(__file__).parent / "pipeline-api" / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def _load_icp_signals(icp_path: Path) -> list[dict]:
    """Load and return the signals list from an ICP checklist JSON."""
    icp_data = json.loads(icp_path.read_text(encoding="utf-8"))
    return icp_data.get("signals") or icp_data.get("icp_signals") or []


def _load_run_config(run_dir: Path) -> dict:
    """Load the run's frozen project config snapshot, falling back to an empty dict."""
    snapshot_path = run_dir / "project_config_snapshot.json"
    if snapshot_path.exists():
        return json.loads(snapshot_path.read_text(encoding="utf-8"))
    return {}


def _reset_exclusion_fields(record: dict) -> None:
    """Reset stale exclusion and tier fields so they are recomputed fresh.

    Clears exclusion_status, exclusion_reason, exclusion_primary_gate, and
    target_tier before re-running apply_exclusions and validate_and_finalize.
    Without this reset, stale values from the previous scoring pass would carry
    forward and corrupt the new tier assignment.
    """
    record["exclusion_status"] = "CLEAR"
    record["exclusion_reason"] = ""
    record["exclusion_primary_gate"] = ""
    record["target_tier"] = ""


def _rescore_record(record: dict, icp_signals: list[dict], run_config: dict) -> dict:
    """Re-score a single record using updated ICP weights.

    Applies reinforcement, recalculates scores, then re-runs the exclusion check
    (Step 6) and validation (Step 7) for records the pipeline left CLEAR. Signals
    are never modified — only scores and tiers change.

    Excluded records are preserved untouched. Exclusions are weight-independent
    hard gates, and the provenance that produced an LLM / customer-suppression /
    NPI-taxonomy exclusion is stripped from enriched_targets.json at write time,
    so re-deriving exclusions here would silently un-exclude those records. A
    rescore adjusts weights only; to change an exclusion outcome, re-run the pipeline.

    Returns the updated record.
    """
    # An excluded record can never be un-excluded by a weight change, and its
    # exclusion provenance is not in the output file to re-derive, so it is
    # returned exactly as-is (tier Excluded, score already capped).
    if record.get("exclusion_status") == "EXCLUDED":
        return record

    from enrichment.signal_extractor import _apply_reinforcement, _calculate_scores, _determine_fit_confidence_status
    from enrichment.exclusion_checker import apply_exclusions
    from enrichment.scorer import validate_and_finalize

    signals = record.get("signals") or []

    # Re-apply reinforcement so state_inferred values reflect the current ICP.
    # Reset any prior inferred state first so re-running is idempotent.
    for sig in signals:
        sig["state_inferred"] = False
        sig["inferred_from"] = ""
    _apply_reinforcement(signals, icp_signals)

    # Recalculate scores with the updated ICP weights.
    scores = _calculate_scores(signals, icp_signals)
    record["bullseye_score"] = scores["bullseye_score"]
    record["fit_signal_score"] = scores["fit_signal_score"]
    record["confidence_score"] = scores["confidence_score"]
    record["fit_confidence_status"] = _determine_fit_confidence_status(
        scores["fit_signal_score"], scores["confidence_score"]
    )

    # Reset stale exclusion/tier fields before re-running the check.
    _reset_exclusion_fields(record)

    # Step 6: re-run exclusion check and tier assignment.
    apply_exclusions(record, run_config)

    # Step 7: re-run scoring validation and finalization.
    validate_and_finalize(record)

    return record


def run_rescore_pass(run_dir: Path, icp_signals: list[dict]) -> dict:
    """Re-score all enriched records in a run directory using updated ICP weights.

    Skips not_enriched records (ingest-only runs have no signals to re-score).
    Preserves existing verification objects unchanged.
    Writes results atomically back to enriched_targets.json.

    Returns a summary dict with rescored count and tier_changes list.
    """
    targets_path = run_dir / "enriched_targets.json"
    if not targets_path.exists():
        raise FileNotFoundError(f"enriched_targets.json not found in {run_dir}")

    raw = json.loads(targets_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        wrapper = raw
        records = raw.get("records", [])
    else:
        wrapper = None
        records = raw

    run_config = _load_run_config(run_dir)

    rescored = 0
    tier_changes: list[dict] = []

    updated_records = []
    for record in records:
        # Skip not_enriched records — no signals to re-score.
        if record.get("enrichment_status") == "not_enriched":
            updated_records.append(record)
            continue

        old_tier = record.get("target_tier", "")
        old_score = record.get("bullseye_score", 0)

        # Preserve the existing verification object — it is never overwritten.
        existing_verification = record.get("verification")

        _rescore_record(record, icp_signals, run_config)

        # Restore the verification object if it existed before.
        if existing_verification is not None:
            record["verification"] = existing_verification

        new_tier = record.get("target_tier", "")
        new_score = record.get("bullseye_score", 0)
        rescored += 1

        if old_tier != new_tier or old_score != new_score:
            tier_changes.append({
                "practice_name": record.get("practice_name", ""),
                "old_tier": old_tier,
                "new_tier": new_tier,
                "old_score": old_score,
                "new_score": new_score,
            })

        updated_records.append(record)

    # Strip internal (_-prefixed) fields before writing — validate_and_finalize
    # re-injects _npi_taxonomy_exclusions — matching the Step-8 output convention.
    from enrichment.scorer import strip_internal_fields
    updated_records = [strip_internal_fields(r) for r in updated_records]

    # Atomic write: write to a .tmp file then os.replace() for crash safety.
    tmp_path = targets_path.with_suffix(".json.tmp")
    if wrapper is not None:
        wrapper["records"] = updated_records
        output = wrapper
    else:
        output = updated_records

    tmp_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, targets_path)

    return {"rescored": rescored, "tier_changes": tier_changes}


def run_rescore_preview(run_dir: Path, icp_signals: list[dict]) -> dict:
    """Compute what a rescore WOULD do, without writing anything.

    Strictly read-only: each record is deep-copied before scoring, so neither
    enriched_targets.json on disk nor the in-memory list is ever mutated. The
    file is never rewritten. Returns a counts-only summary — the per-tier
    transitions an operator would see if they ran the real rescore.

    Skips not_enriched records (no signals to score), matching run_rescore_pass.
    """
    targets_path = run_dir / "enriched_targets.json"
    if not targets_path.exists():
        raise FileNotFoundError(f"enriched_targets.json not found in {run_dir}")

    raw = json.loads(targets_path.read_text(encoding="utf-8"))
    records = raw.get("records", []) if isinstance(raw, dict) else raw

    run_config = _load_run_config(run_dir)

    rescored = 0
    tier_transitions: dict[str, int] = {}
    score_changed_only = 0
    unchanged = 0

    for record in records:
        if record.get("enrichment_status") == "not_enriched":
            continue

        old_tier = record.get("target_tier", "")
        old_score = record.get("bullseye_score", 0)

        # Score a deep copy so the original record is never touched.
        candidate = copy.deepcopy(record)
        _rescore_record(candidate, icp_signals, run_config)

        new_tier = candidate.get("target_tier", "")
        new_score = candidate.get("bullseye_score", 0)
        rescored += 1

        if old_tier != new_tier:
            key = f"{old_tier or '—'} → {new_tier or '—'}"
            tier_transitions[key] = tier_transitions.get(key, 0) + 1
        elif old_score != new_score:
            score_changed_only += 1
        else:
            unchanged += 1

    return {
        "preview": True,
        "rescored": rescored,
        "tier_transitions": tier_transitions,
        "score_changed_only": score_changed_only,
        "unchanged": unchanged,
    }


def main() -> None:
    """Entry point for the rescore CLI."""
    parser = argparse.ArgumentParser(description="ICP re-scoring post-run pass")
    parser.add_argument("--run-dir", required=True, help="Path to the run directory")
    parser.add_argument(
        "--icp",
        required=False,
        default=None,
        help="Path to the ICP checklist JSON (defaults to icp_snapshot.json in the run dir)",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Show what would change without writing anything (read-only).",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        sys.exit(f"Run directory not found: {run_dir}")

    if args.icp:
        icp_path = Path(args.icp)
    else:
        icp_path = run_dir / "icp_snapshot.json"

    if not icp_path.exists():
        sys.exit(f"ICP file not found: {icp_path}")

    icp_signals = _load_icp_signals(icp_path)
    if not icp_signals:
        sys.exit(f"No signals found in ICP file: {icp_path}")

    if args.preview:
        print(f"Previewing rescore of run {run_dir.name} with {len(icp_signals)} ICP signals (nothing will be saved)...")
        stats = run_rescore_preview(run_dir, icp_signals)
    else:
        print(f"Re-scoring run {run_dir.name} with {len(icp_signals)} ICP signals...")
        stats = run_rescore_pass(run_dir, icp_signals)
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
