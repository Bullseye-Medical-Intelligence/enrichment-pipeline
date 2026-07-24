"""
reextract_run.py — CLI entry point for the signal re-extraction post-run pass.

Usage:
    python reextract_run.py --run-dir output/runs/<id> --icp config/.../icp_checklist.json
    python reextract_run.py --run-dir output/runs/<id> --icp config/.../icp_checklist.json --preview

Loads a completed run's enriched_targets.json, rehydrates each record's page
text from the Evidence Vault (_context_text is stripped from output at write
time), re-runs Claude signal extraction (Step 4) on it using the supplied ICP,
then immediately re-runs Steps 6-7 (exclusion check + scoring validation).

Records are skipped when:
  - enrichment_status == "not_enriched" (ingest-only roster rows)
  - no Evidence Vault snapshot (records that were never successfully crawled)
  - exclusion_status == "EXCLUDED" — an exclusion is a hard gate, not a signal
    outcome. The provenance that produced a customer-suppression / NPI-taxonomy /
    LLM exclusion is stripped from enriched_targets.json at write time, so
    re-deriving exclusions here would silently un-exclude those records and
    return an existing customer or a hospital-owned practice to a sellable tier.
    Re-extraction only ever re-adjudicates CLEAR records (where fresh signals
    can still ADD an exclusion); to change an exclusion outcome, re-run the
    pipeline. Mirrors the same guard in rescore_run.py.

Records with thin context (source_confidence limited/failed) are not skipped —
extract_signals() handles the thin-context short-circuit itself and sets
enrichment_status = "partial" accordingly.

Use this when new signals have been added to an ICP and you want to evaluate
existing crawled content without re-crawling.

Prints a JSON summary to stdout.
Reads credentials from pipeline-api/.env (ANTHROPIC_API_KEY, CLAUDE_MODEL).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from output.atomic_write import ConcurrentRunChange, guarded_replace, stat_fingerprint

# Load .env from pipeline-api/ when running from repo root
_env_path = Path(__file__).parent / "pipeline-api" / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def _load_icp_data(icp_path: Path) -> dict:
    """Load the full ICP data dict from an ICP checklist JSON."""
    return json.loads(icp_path.read_text(encoding="utf-8"))


def _load_run_config(run_dir: Path) -> dict:
    """Load the run's frozen project config snapshot, falling back to an empty dict."""
    snapshot_path = run_dir / "project_config_snapshot.json"
    if snapshot_path.exists():
        return json.loads(snapshot_path.read_text(encoding="utf-8"))
    return {}


def _rehydrate_missing_context(run_dir: Path, records: list[dict]) -> None:
    """Refill `_context_text` from the Evidence Vault for records missing it.

    _context_text is stripped from enriched_targets.json at output time, so a
    completed run never carries it. Rehydrate from the archived page snapshots
    so re-extraction sees the same text the original crawl scored.
    """
    from output.evidence_writer import read_record_context_text

    for record in records:
        if (record.get("_context_text") or "").strip():
            continue
        record["_context_text"] = read_record_context_text(run_dir, record.get("id", ""))


def _is_eligible(record: dict) -> bool:
    """Return True if a record has stored context text, was enriched, and is not EXCLUDED.

    EXCLUDED records are never re-extracted: their exclusion provenance
    (_customer_suppressed, _npi_taxonomy_exclusions, _llm_exclusion_triggers) is
    stripped from the output file, so re-running apply_exclusions on fresh
    signals cannot reconstruct a suppression or taxonomy exclusion — the record
    would silently return to a sellable tier. Un-excluding requires a full
    pipeline re-run.
    """
    if record.get("enrichment_status") == "not_enriched":
        return False
    if record.get("exclusion_status") == "EXCLUDED":
        return False
    return bool((record.get("_context_text") or "").strip())


def _reextract_record(
    record: dict,
    icp_signals: list[dict],
    run_config: dict,
    contact_strategy: str,
) -> None:
    """Re-run signal extraction + Steps 6-7 on a single record, mutating it in-place.

    Uses the stored _context_text. Makes a Claude API call (same as pipeline Step 4).
    extract_signals() handles the thin-context short-circuit internally.
    """
    from enrichment.signal_extractor import extract_signals, DEFAULT_BULLSEYE_MIN_SCORE
    from enrichment.exclusion_checker import apply_exclusions
    from enrichment.scorer import validate_and_finalize

    context_text = record.get("_context_text") or ""
    run_id = record.get("enrichment_run_id") or "reextract"
    bullseye_min = run_config.get("bullseye_min_score", DEFAULT_BULLSEYE_MIN_SCORE)
    target_specialty = run_config.get("target_specialty", "")

    # Reset stale tier/exclusion fields before re-running. Only CLEAR records
    # ever reach this point (_is_eligible skips EXCLUDED), so this clears a
    # stale tier ahead of apply_exclusions — it can never wipe a hard exclusion.
    record["exclusion_status"] = "CLEAR"
    record["exclusion_reason"] = ""
    record["exclusion_primary_gate"] = ""
    record["target_tier"] = ""

    extract_signals(
        record,
        icp_signals,
        context_text,
        run_id,
        bullseye_min_score=bullseye_min,
        target_specialty=target_specialty,
        contact_strategy=contact_strategy,
    )

    apply_exclusions(record, run_config)
    validate_and_finalize(record)


def run_reextract_pass(
    run_dir: Path,
    icp_signals: list[dict],
    run_config: dict,
    icp_data: dict,
    llm_concurrency: int = 3,
) -> dict:
    """Re-extract signals for all eligible records in a run directory.

    Skips not_enriched records and records without stored _context_text.
    Re-runs Steps 4, 6, and 7 using the supplied ICP and config.
    Writes results atomically back to enriched_targets.json.

    Returns a summary dict with processed count, skipped count, and tier_changes list.
    """
    targets_path = run_dir / "enriched_targets.json"
    if not targets_path.exists():
        raise FileNotFoundError(f"enriched_targets.json not found in {run_dir}")

    # Fingerprint before load: the final write is refused if the file changed
    # while this pass ran (concurrent merge or another pass).
    loaded_fp = stat_fingerprint(targets_path)
    raw = json.loads(targets_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        wrapper = raw
        records = list(raw.get("records", []))
    else:
        wrapper = None
        records = list(raw)

    _rehydrate_missing_context(run_dir, records)
    contact_strategy = icp_data.get("contact_strategy", "")
    eligible_indices = [i for i, r in enumerate(records) if _is_eligible(r)]
    skipped_excluded = sum(
        1 for r in records if r.get("exclusion_status") == "EXCLUDED"
    )

    if not eligible_indices:
        return {
            "processed": 0,
            "skipped": len(records),
            "skipped_excluded": skipped_excluded,
            "tier_changes": [],
        }

    old_tiers = {i: records[i].get("target_tier", "") for i in eligible_indices}
    old_scores = {i: records[i].get("bullseye_score", 0) for i in eligible_indices}
    tier_changes: list[dict] = []

    def _process(idx: int) -> int:
        _reextract_record(records[idx], icp_signals, run_config, contact_strategy)
        return idx

    with ThreadPoolExecutor(max_workers=llm_concurrency) as pool:
        futures = {pool.submit(_process, i): i for i in eligible_indices}
        for future in as_completed(futures):
            idx = future.result()
            new_tier = records[idx].get("target_tier", "")
            new_score = records[idx].get("bullseye_score", 0)
            if old_tiers[idx] != new_tier or old_scores[idx] != new_score:
                tier_changes.append({
                    "practice_name": records[idx].get("practice_name", ""),
                    "old_tier": old_tiers[idx],
                    "new_tier": new_tier,
                    "old_score": old_scores[idx],
                    "new_score": new_score,
                })

    # Sum this pass's Claude spend before internal fields are stripped. Only the
    # records re-extracted just now carry _llm_usage (it never survives to
    # output), so this is exactly the pass's own cost. The caller adds it to the
    # run's totals — without it, re-extracting a run spends real budget that
    # never appears in the reported cost.
    llm_usage = {
        "llm_input_tokens": sum(
            (r.get("_llm_usage") or {}).get("input_tokens", 0) for r in records),
        "llm_output_tokens": sum(
            (r.get("_llm_usage") or {}).get("output_tokens", 0) for r in records),
        "llm_call_count": sum(1 for r in records if r.get("_llm_usage")),
    }

    # Strip internal (_-prefixed) fields — including the rehydrated _context_text
    # and any re-extraction bookkeeping — so the written output keeps its contract.
    from enrichment.scorer import strip_internal_fields
    records = [strip_internal_fields(r) for r in records]

    tmp_path = targets_path.with_suffix(".json.tmp")
    if wrapper is not None:
        wrapper["records"] = records
        output = wrapper
    else:
        output = records

    tmp_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    guarded_replace(run_dir, targets_path, tmp_path, loaded_fp)

    return {
        "processed": len(eligible_indices),
        "skipped": len(records) - len(eligible_indices),
        "skipped_excluded": skipped_excluded,
        "tier_changes": tier_changes,
        **llm_usage,
    }


def run_reextract_preview(run_dir: Path) -> dict:
    """Report how many records would be re-extracted, without any LLM calls.

    Read-only: never writes to disk, never calls Claude.
    Returns eligible count, per-reason skip counts, and total.
    """
    targets_path = run_dir / "enriched_targets.json"
    if not targets_path.exists():
        raise FileNotFoundError(f"enriched_targets.json not found in {run_dir}")

    raw = json.loads(targets_path.read_text(encoding="utf-8"))
    records = raw.get("records", []) if isinstance(raw, dict) else raw

    _rehydrate_missing_context(run_dir, records)

    eligible = 0
    skipped_not_enriched = 0
    skipped_excluded = 0
    skipped_no_context = 0

    for record in records:
        if record.get("enrichment_status") == "not_enriched":
            skipped_not_enriched += 1
        elif record.get("exclusion_status") == "EXCLUDED":
            # Hard exclusions are preserved, never re-adjudicated (see _is_eligible).
            skipped_excluded += 1
        elif not (record.get("_context_text") or "").strip():
            skipped_no_context += 1
        else:
            eligible += 1

    return {
        "preview": True,
        "eligible": eligible,
        "skipped_not_enriched": skipped_not_enriched,
        "skipped_excluded": skipped_excluded,
        "skipped_no_context": skipped_no_context,
        "total": len(records),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-extract signals from stored _context_text using an updated ICP"
    )
    parser.add_argument("--run-dir", required=True, help="Path to the run directory")
    parser.add_argument("--icp", required=True, help="Path to the ICP checklist JSON")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Report eligible record count without calling Claude",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Claude worker count (default: llm_concurrency from run_config, or 3)",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        sys.exit(f"Run directory not found: {run_dir}")

    icp_path = Path(args.icp)
    if not icp_path.exists():
        sys.exit(f"ICP file not found: {icp_path}")

    if args.preview:
        stats = run_reextract_preview(run_dir)
        print(json.dumps(stats))
        return

    icp_data = _load_icp_data(icp_path)
    icp_signals = icp_data.get("signals") or icp_data.get("icp_signals") or []
    run_config = _load_run_config(run_dir)
    llm_concurrency = args.concurrency or run_config.get("llm_concurrency", 3)

    print(f"Re-extracting signals for {run_dir.name}…")
    try:
        stats = run_reextract_pass(run_dir, icp_signals, run_config, icp_data, llm_concurrency)
    except ConcurrentRunChange as e:
        sys.exit(str(e))
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
