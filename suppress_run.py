"""
suppress_run.py — CLI entry point for the suppression re-check post-run pass.

Usage:
    python suppress_run.py --run-dir output/runs/<id> --suppression data/existing_customers.csv

Loads a completed run's enriched_targets.json and re-checks each non-suppressed
record against an (updated) customer suppression CSV. Records that now match a
known customer are marked EXCLUDED. Records already marked suppressed are
left unchanged.

No LLM calls. No re-crawl. Runs in seconds regardless of run size.

Prints a JSON summary to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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


def _load_run_config(run_dir: Path) -> dict:
    """Load the run's frozen project config snapshot, falling back to an empty dict."""
    snapshot_path = run_dir / "project_config_snapshot.json"
    if snapshot_path.exists():
        return json.loads(snapshot_path.read_text(encoding="utf-8"))
    return {}


def _suppress_record(record: dict, reason: str, run_config: dict) -> None:
    """Mark a record as customer-suppressed and apply exclusion rules in-place."""
    from enrichment.exclusion_checker import apply_exclusions
    from enrichment.scorer import validate_and_finalize

    record["_customer_suppressed"] = True
    record["_suppression_reason"] = reason
    apply_exclusions(record, run_config)
    validate_and_finalize(record)


def run_suppress_pass(
    run_dir: Path,
    suppression_path: Path,
) -> dict:
    """Re-check all non-suppressed records against an updated suppression list.

    Skips records already marked _customer_suppressed=True. For each remaining
    record that now matches a known customer, applies the suppression exclusion
    and re-runs validate_and_finalize for consistency. Writes results atomically.

    Returns a summary dict with newly_suppressed count, already_suppressed count,
    skipped count, and a list of newly suppressed record names.
    """
    from ingestion.customer_suppression import load_suppression_list, check_suppression

    targets_path = run_dir / "enriched_targets.json"
    if not targets_path.exists():
        raise FileNotFoundError(f"enriched_targets.json not found in {run_dir}")

    suppression_list = load_suppression_list(suppression_path)
    if suppression_list.is_empty:
        raise ValueError(f"Suppression list at {suppression_path} is empty or unreadable")

    # Fingerprint before load: the final write is refused if the file changed
    # while this pass ran (concurrent merge or another pass).
    loaded_fp = stat_fingerprint(targets_path)
    raw = json.loads(targets_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        wrapper = raw
        records = raw.get("records", [])
    else:
        wrapper = None
        records = raw

    run_config = _load_run_config(run_dir)

    newly_suppressed = 0
    already_suppressed = 0
    newly_suppressed_names: list[str] = []

    for record in records:
        if record.get("_customer_suppressed"):
            already_suppressed += 1
            continue

        is_suppressed, reason = check_suppression(record, suppression_list)
        if is_suppressed:
            # A matching record that is already EXCLUDED was suppressed on a prior
            # run — the _customer_suppressed marker is stripped from output, so
            # exclusion_status is the durable signal. Count it as already suppressed,
            # not newly, so a re-check does not inflate the churn number.
            if record.get("exclusion_status") == "EXCLUDED":
                already_suppressed += 1
                continue
            _suppress_record(record, reason, run_config)
            newly_suppressed += 1
            newly_suppressed_names.append(record.get("practice_name", ""))

    if newly_suppressed:
        # Strip internal (_-prefixed) fields before writing, matching the Step-8
        # output convention: _suppress_record sets _customer_suppressed and
        # validate_and_finalize re-injects _npi_taxonomy_exclusions, neither of
        # which belongs in enriched_targets.json.
        from enrichment.scorer import strip_internal_fields
        clean = [strip_internal_fields(r) for r in records]
        tmp_path = targets_path.with_suffix(".json.tmp")
        if wrapper is not None:
            wrapper["records"] = clean
            output = wrapper
        else:
            output = clean
        tmp_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        guarded_replace(run_dir, targets_path, tmp_path, loaded_fp)

    return {
        "newly_suppressed": newly_suppressed,
        "already_suppressed": already_suppressed,
        "total": len(records),
        "newly_suppressed_names": newly_suppressed_names,
    }


def run_suppress_preview(run_dir: Path, suppression_path: Path) -> dict:
    """Report which records would be newly suppressed, without writing anything.

    Read-only: never modifies enriched_targets.json.
    """
    from ingestion.customer_suppression import load_suppression_list, check_suppression

    targets_path = run_dir / "enriched_targets.json"
    if not targets_path.exists():
        raise FileNotFoundError(f"enriched_targets.json not found in {run_dir}")

    suppression_list = load_suppression_list(suppression_path)
    if suppression_list.is_empty:
        raise ValueError(f"Suppression list at {suppression_path} is empty or unreadable")

    raw = json.loads(targets_path.read_text(encoding="utf-8"))
    records = raw.get("records", []) if isinstance(raw, dict) else raw

    would_suppress = 0
    already_suppressed = 0
    would_suppress_names: list[str] = []

    for record in records:
        if record.get("_customer_suppressed"):
            already_suppressed += 1
            continue
        is_suppressed, reason = check_suppression(record, suppression_list)
        if is_suppressed:
            # Mirror run_suppress_pass: an already-EXCLUDED match was suppressed on a
            # prior run (the _customer_suppressed marker is stripped from output), so
            # it is already suppressed, not a new one. Without this, preview reports
            # "would suppress N" while the pass reports "0 newly suppressed".
            if record.get("exclusion_status") == "EXCLUDED":
                already_suppressed += 1
                continue
            would_suppress += 1
            would_suppress_names.append(record.get("practice_name", ""))

    return {
        "preview": True,
        "would_suppress": would_suppress,
        "already_suppressed": already_suppressed,
        "total": len(records),
        "would_suppress_names": would_suppress_names,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-check a completed run against an updated customer suppression list"
    )
    parser.add_argument("--run-dir", required=True, help="Path to the run directory")
    parser.add_argument(
        "--suppression", required=True, help="Path to the customer suppression CSV"
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Report which records would be suppressed without writing anything",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        sys.exit(f"Run directory not found: {run_dir}")

    suppression_path = Path(args.suppression)
    if not suppression_path.exists():
        sys.exit(f"Suppression list not found: {suppression_path}")

    if args.preview:
        stats = run_suppress_preview(run_dir, suppression_path)
        print(json.dumps(stats))
        return

    print(f"Re-checking suppression for {run_dir.name}…")
    try:
        stats = run_suppress_pass(run_dir, suppression_path)
    except ConcurrentRunChange as e:
        sys.exit(str(e))
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
