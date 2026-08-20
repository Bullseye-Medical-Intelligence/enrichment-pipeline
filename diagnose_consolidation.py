"""
diagnose_consolidation.py — why did (or didn't) two rows become one practice?

Read-only. No writes, no network, no LLM. Answers two questions an operator
actually asks:

  1. "How many locations would collapse if I turned contact blocking on?"
     Run against an ingest CSV or a run's enriched_targets.json with --compare.

  2. "Why are these two rows separate?"
     Run with --domain to see both rows' block keys, their pair score, the
     fields that matched, and the gate that decided it.

Usage:
    python diagnose_consolidation.py --run-dir output/runs/<id>
    python diagnose_consolidation.py --run-dir output/runs/<id> --domain obgynofatlanta.com
    python diagnose_consolidation.py --input data/list.csv --source outscraper --compare

The numbers it prints come from the consolidation engine itself, not from a
re-implementation, so they are the same figures a run would produce.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ingestion.consolidator import (  # noqa: E402
    MAX_CONTACT_BLOCK,
    MERGE_THRESHOLD,
    REVIEW_THRESHOLD,
    _block_key,
    _contact_block_key,
    consolidate_records,
    domain_policy,
    identity_of,
    score_pair,
    units_conflict,
)


def _load_rows(args) -> list[dict]:
    """Load the rows to diagnose from a run directory or a source CSV."""
    if args.run_dir:
        path = Path(args.run_dir) / "enriched_targets.json"
        if not path.exists():
            sys.exit(f"enriched_targets.json not found in {args.run_dir}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("records", payload) if isinstance(payload, dict) else payload
        if not rows:
            sys.exit("No records in enriched_targets.json")
        return rows

    from ingestion.apify_places_adapter import load_apify_places_csv
    from ingestion.manual_adapter import load_manual_csv
    from ingestion.outscraper_adapter import load_outscraper_csv

    loaders = {"outscraper": load_outscraper_csv, "manual": load_manual_csv,
               "apify_places": load_apify_places_csv}
    if args.source not in loaders:
        sys.exit(f"--source must be one of {', '.join(sorted(loaders))}")
    return loaders[args.source](args.input)


def _run_config(args) -> dict:
    """Config snapshot from the run directory, else an empty default."""
    if args.run_dir:
        snapshot = Path(args.run_dir) / "project_config_snapshot.json"
        if snapshot.exists():
            try:
                return json.loads(snapshot.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
    return {}


def _with_contact_blocking(config: dict, enabled: bool) -> dict:
    """Copy of config with contact blocking forced on or off."""
    settings = dict((config or {}).get("consolidation") or {})
    settings["enabled"] = True
    settings["contact_blocking"] = enabled
    return {**(config or {}), "consolidation": settings}


def _report_compare(rows: list[dict], config: dict) -> None:
    """Print the before/after a contact-blocking decision needs (RULE M4).

    Both figures come from the same rows through the same engine with one
    argument varied, which is what makes the delta attributable.
    """
    off, off_summary = consolidate_records(
        [dict(r) for r in rows], _with_contact_blocking(config, False))
    on, on_summary = consolidate_records(
        [dict(r) for r in rows], _with_contact_blocking(config, True))

    collapsed = len(off) - len(on)
    print("\n=== CONTACT BLOCKING: OFF vs ON ===")
    print(f"  input rows                      {len(rows):>7,}")
    print(f"  locations, contact blocking OFF {len(off):>7,}")
    print(f"  locations, contact blocking ON  {len(on):>7,}")
    pct = (collapsed / len(off) * 100) if off else 0.0
    print(f"  additional collapse             {collapsed:>7,}  ({pct:.1f}% of OFF)")
    print(f"  cross-address merges            {on_summary['cross_address_merges']:>7,}")
    print(f"  review pairs   OFF -> ON        {off_summary['review_pairs']:>7,} -> "
          f"{on_summary['review_pairs']:,}")
    skipped = on_summary["contact_blocks_skipped_oversized"]
    print(f"  oversized blocks skipped        {skipped:>7,}"
          f"   (> {MAX_CONTACT_BLOCK} rows on one number)")
    rescued = on_summary["unblocked_rescued_by_contact"]
    print(f"  address-unblocked rows reached  {rescued:>7,}"
          f"   of {off_summary['unblocked_count']:,} unblocked")
    if skipped:
        print("\n  NOTE: skipped blocks are numbers shared by more rows than one")
        print("  front desk plausibly serves. Inspect them before raising the cap.")


def _report_groups(rows: list[dict], config: dict) -> None:
    """Print the size distribution of (phone, domain) blocks — the risk tail."""
    noise, umbrella = domain_policy(config)
    excluded = noise | umbrella
    sizes = Counter()
    keys: dict[tuple, int] = {}
    for row in rows:
        key = _contact_block_key(identity_of(row), excluded)
        if key is not None:
            keys[key] = keys.get(key, 0) + 1
    for count in keys.values():
        sizes[count] += 1

    print("\n=== (phone, domain) BLOCK SIZES ===")
    if not sizes:
        print("  no row carries both a phone and a non-excluded domain")
        return
    print(f"  {'rows in block':>14}  {'blocks':>7}")
    for size in sorted(sizes):
        flag = "   <- skipped, over the cap" if size > MAX_CONTACT_BLOCK else ""
        print(f"  {size:>14}  {sizes[size]:>7}{flag}")
    largest = max(keys.values())
    print(f"  largest block: {largest} rows")


def _report_domain(rows: list[dict], config: dict, domain: str) -> None:
    """Explain, pair by pair, what the engine decided for one domain's rows."""
    noise, umbrella = domain_policy(config)
    excluded = noise | umbrella
    wanted = domain.strip().lower().removeprefix("www.")

    matches = [r for r in rows if identity_of(r)["domain"] == wanted]
    if not matches:
        print(f"\nNo rows carry the registrable domain '{wanted}'.")
        return

    print(f"\n=== ROWS ON {wanted} ({len(matches)}) ===")
    for row in matches:
        ident = identity_of(row)
        print(f"  {row.get('practice_name') or '(no name)'}")
        print(f"      {row.get('address_street') or '(no street)'} "
              f"{row.get('address_unit') or ''} | "
              f"{row.get('address_city') or ''} {row.get('address_zip') or ''} | "
              f"{row.get('phone') or '(no phone)'}")
        print(f"      address block : {_block_key(ident)}")
        print(f"      contact block : {_contact_block_key(ident, excluded)}")

    print(f"\n=== PAIR DECISIONS (merge >= {MERGE_THRESHOLD}, "
          f"review {REVIEW_THRESHOLD}-{MERGE_THRESHOLD - 1}) ===")
    for a in range(len(matches)):
        for b in range(a + 1, len(matches)):
            left, right = identity_of(matches[a]), identity_of(matches[b])
            label = (f"  [{a}] x [{b}]")
            if units_conflict(left, right):
                print(f"{label}  SPLIT — units differ "
                      f"({left['unit']} vs {right['unit']}); hard veto, never scored")
                continue
            same_address = _block_key(left) is not None and _block_key(left) == _block_key(right)
            ck_l = _contact_block_key(left, excluded)
            same_contact = ck_l is not None and ck_l == _contact_block_key(right, excluded)
            if not same_address and not same_contact:
                print(f"{label}  SPLIT — no shared block key; never compared")
                continue
            score, matched = score_pair(left, right, noise)
            path = "address block" if same_address else "contact block"
            verdict = ("MERGE" if score >= MERGE_THRESHOLD
                       else "REVIEW" if score >= REVIEW_THRESHOLD else "SPLIT")
            print(f"{label}  {verdict} — score {score} via {path} "
                  f"({', '.join(matched) or 'nothing matched'})")


def main() -> None:
    """Parse arguments and print the requested consolidation diagnostics."""
    parser = argparse.ArgumentParser(
        description="Read-only consolidation diagnostics for a run or a source CSV")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-dir", help="A run directory containing enriched_targets.json")
    source.add_argument("--input", help="A source CSV to diagnose before running")
    parser.add_argument("--source", default="outscraper",
                        help="CSV source type when using --input")
    parser.add_argument("--domain", help="Explain the pair decisions for one domain")
    parser.add_argument("--compare", action="store_true",
                        help="Report locations with contact blocking off vs on")
    args = parser.parse_args()

    rows = _load_rows(args)
    config = _run_config(args)
    print(f"Loaded {len(rows):,} rows")

    if args.domain:
        _report_domain(rows, config, args.domain)
    if args.compare or not args.domain:
        _report_compare(rows, config)
        _report_groups(rows, config)


if __name__ == "__main__":
    main()
