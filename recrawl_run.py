"""
recrawl_run.py — CLI entry point for the post-run browser re-crawl pass.

Usage:
    python recrawl_run.py --run-dir output/runs/<id> [--icp config/.../icp_checklist.json]

Reads enriched_targets.json from the run directory, re-crawls records with
source_confidence "limited" or "failed" using Playwright (headless Chromium),
re-runs Steps 4-7 on those that improved, writes results back atomically.
Prints a JSON summary to stdout.

Falls back to icp_snapshot.json inside the run directory when --icp is omitted.
Reads credentials from .env (ANTHROPIC_API_KEY required for signal extraction).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Load .env from the repo root when running from here
_env_path = Path(__file__).parent / ".env"
if not _env_path.exists():
    # Also try pipeline-api/.env, mirroring verify_run.py's fallback
    _env_path = Path(__file__).parent / "pipeline-api" / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_BLOCKED_CONFIDENCES = ("limited", "failed")


def _load_icp_signals(icp_path: Path, run_dir: Path) -> list[dict]:
    """Load ICP signals from the provided path, or fall back to the run's icp_snapshot.json."""
    if icp_path and icp_path.exists():
        icp_data = json.loads(icp_path.read_text(encoding="utf-8"))
    else:
        snapshot = run_dir / "icp_snapshot.json"
        if not snapshot.exists():
            sys.exit(
                f"No ICP file found: --icp not provided and icp_snapshot.json "
                f"does not exist in {run_dir}"
            )
        icp_data = json.loads(snapshot.read_text(encoding="utf-8"))
    return icp_data.get("signals") or icp_data.get("icp_signals") or []


def _load_run_config(run_dir: Path) -> dict:
    """Load the run config snapshot from the run directory, or return a minimal default."""
    snapshot = run_dir / "project_config_snapshot.json"
    if snapshot.exists():
        try:
            return json.loads(snapshot.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _load_records(run_dir: Path) -> tuple[list[dict], dict, tuple | None]:
    """Load records from enriched_targets.json.

    Returns (records, payload, fingerprint) where payload is the full file
    structure (wrapper dict or plain list) needed for atomic rewrite and
    fingerprint identifies the loaded file version for the guarded final write.
    """
    targets_path = run_dir / "enriched_targets.json"
    if not targets_path.exists():
        sys.exit(f"enriched_targets.json not found in {run_dir}")

    # Fingerprint before load: the final write is refused if the file changed
    # while this pass ran (concurrent merge or another pass).
    loaded_fp = stat_fingerprint(targets_path)
    with open(targets_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    if isinstance(payload, dict):
        records = payload.get("records", [])
    else:
        records = payload

    return records, payload, loaded_fp


def _write_records_atomic(
    run_dir: Path, records: list[dict], payload, loaded_fp: tuple | None
) -> None:
    """Write enriched_targets.json atomically, refusing on concurrent change."""
    targets_path = run_dir / "enriched_targets.json"
    if isinstance(payload, dict):
        payload["records"] = records
        payload["record_count"] = len(records)
        out = payload
    else:
        out = records

    tmp = targets_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    guarded_replace(run_dir, targets_path, tmp, loaded_fp)


def _match_key(record: dict) -> tuple:
    """Return a stable key for matching a recrawled record back to the full list."""
    return (
        (record.get("id") or "").strip(),
        (record.get("practice_name") or "").strip(),
        (record.get("website_url") or "").strip(),
    )


from extraction.playwright_extractor import crawl_with_playwright
from enrichment.signal_extractor import extract_signals
from enrichment.exclusion_checker import apply_exclusions
from enrichment.scorer import validate_and_finalize, strip_internal_fields
from enrichment.constants import DEFAULT_BULLSEYE_MIN_SCORE, MIN_CONTEXT_CHARS
from output.atomic_write import ConcurrentRunChange, guarded_replace, stat_fingerprint
from output.evidence_writer import write_record_evidence

# Default page budget when the run config snapshot carries no
# max_pages_per_practice — mirrors the standard crawl's depth so a re-crawl
# never sees less of a site than the first pass did.
DEFAULT_RECRAWL_MAX_PAGES = 20


def run_browser_recrawl_pass(run_dir: Path, icp_signals: list[dict]) -> dict:
    """Execute the browser re-crawl pass on a completed run.

    Steps:
    1. Load enriched_targets.json.
    2. Identify records with source_confidence limited/failed.
    3. Re-crawl each with Playwright.
    4. For records that improved, re-run Steps 4-7 (signal extraction,
       exclusion, scoring).
    5. Merge improved records back and write atomically.

    Returns {"recrawled": N, "improved": M, "still_blocked": K}.
    """

    records, payload, loaded_fp = _load_records(run_dir)
    run_config = _load_run_config(run_dir)

    bullseye_min = run_config.get("bullseye_min_score", DEFAULT_BULLSEYE_MIN_SCORE)
    target_specialty = run_config.get("target_specialty", "")
    max_pages = int(run_config.get("max_pages_per_practice") or DEFAULT_RECRAWL_MAX_PAGES)
    contact_strategy = ""  # loaded from icp_data if present; not in snapshot

    # Try to load contact_strategy from icp snapshot
    snapshot = run_dir / "icp_snapshot.json"
    if snapshot.exists():
        try:
            icp_data = json.loads(snapshot.read_text(encoding="utf-8"))
            contact_strategy = icp_data.get("contact_strategy", "")
        except (json.JSONDecodeError, OSError):
            pass

    # EXCLUDED records are never re-crawled: production output lacks the internal
    # provenance (_customer_suppressed, _npi_taxonomy_exclusions) needed to
    # re-derive their exclusions, so re-processing one could silently return it
    # to a sellable tier. Same rule as reextract_run.py.
    skipped_excluded = sum(
        1 for r in records
        if r.get("source_confidence") in _BLOCKED_CONFIDENCES
        and r.get("exclusion_status") == "EXCLUDED"
    )
    blocked = [
        r for r in records
        if r.get("source_confidence") in _BLOCKED_CONFIDENCES
        and r.get("exclusion_status") != "EXCLUDED"
    ]

    if not blocked:
        summary = {"recrawled": 0, "improved": 0, "still_blocked": 0,
                   "skipped_excluded": skipped_excluded,
                   "message": "No blocked records found"}
        print(json.dumps(summary))
        return summary

    print(f"  Found {len(blocked)} blocked/thin records to re-crawl with Playwright...")
    if skipped_excluded:
        print(f"  Skipping {skipped_excluded} EXCLUDED record(s) — exclusions are preserved.")

    # Build a lookup from match key -> index in the full record list for merging
    key_to_index: dict[tuple, int] = {}
    for idx, record in enumerate(records):
        key_to_index[_match_key(record)] = idx

    stats = {"recrawled": 0, "improved": 0, "still_blocked": 0,
             "skipped_excluded": skipped_excluded}

    for record in blocked:
        url = (record.get("website_url") or "").strip()
        name = record.get("practice_name", "Unknown")

        if not url:
            print(f"  [SKIP] {name} — no website URL")
            stats["still_blocked"] += 1
            continue

        print(f"\n  [RECRAWL] {name} ({url})")
        stats["recrawled"] += 1

        # Re-crawl with Playwright at the standard page budget — a re-crawl
        # must never see less of a site than the first pass did.
        result = crawl_with_playwright(url=url, max_pages=max_pages)

        if result.error or not result.context_text:
            print(f"    [FAIL] Re-crawl failed: {result.error or 'no content returned'}")
            stats["still_blocked"] += 1
            continue

        if len(result.context_text) < MIN_CONTEXT_CHARS:
            print(f"    [THIN] Re-crawl returned only {len(result.context_text)} chars — still blocked")
            stats["still_blocked"] += 1
            continue

        # Re-crawl succeeded — update crawl fields
        print(f"    [OK] Re-crawl returned {len(result.context_text)} chars")
        original_confidence = record.get("source_confidence")
        record["_context_text"] = result.context_text
        record["_pages_crawled"] = result.pages_crawled
        record["_evidence_pages"] = result.pages or []
        record["source_confidence"] = "partial"
        # Use the resolved final URL if it differs
        if result.url and result.url != url:
            record["website_url"] = result.url

        # Step 4: Signal extraction (Claude)
        run_id = record.get("enrichment_run_id") or (
            payload.get("run_id") if isinstance(payload, dict) else ""
        ) or "recrawl"

        try:
            record = extract_signals(
                record=record,
                icp_signals=icp_signals,
                context_text=result.context_text,
                run_id=run_id,
                bullseye_min_score=bullseye_min,
                target_specialty=target_specialty,
                contact_strategy=contact_strategy,
            )
        except Exception as e:
            print(f"    [FAIL] Signal extraction failed: {str(e)[:150]}")
            # Revert the crawl mutations so this record stays in its original blocked
            # state (retryable) instead of being written with a leaked _context_text
            # and a flipped source_confidence that removes it from the re-crawl set.
            for k in ("_context_text", "_pages_crawled", "_evidence_pages"):
                record.pop(k, None)
            record["source_confidence"] = original_confidence
            stats["still_blocked"] += 1
            continue

        # Evidence Vault: persist the fresh pages now that extraction succeeded,
        # so the archived snapshot matches the text the new signals were scored
        # against. (Writing before extraction would desync vault and signals on
        # an extraction failure, which reverts the record.)
        if result.pages:
            write_record_evidence(
                run_dir, record.get("id", ""), result.pages, provenance="recrawl"
            )

        # Step 6: Exclusion check — fail closed. An error here aborts the whole
        # pass before the final write (nothing is written), rather than forcing
        # the record CLEAR and proceeding, which could un-exclude it.
        record = apply_exclusions(record, run_config)

        # Step 7: Scoring validation
        record = validate_and_finalize(record)

        # Strip internal fields before writing back (matching pipeline output convention)
        record = strip_internal_fields(record)

        tier = record.get("target_tier", "")
        score = record.get("bullseye_score", 0)
        print(f"    [RESULT] Tier: {tier} | Score: {score}")
        stats["improved"] += 1

        # Merge back into the full record list by stable key
        key = _match_key(record)
        idx = key_to_index.get(key)
        if idx is not None:
            records[idx] = record
        else:
            # Fallback: match by id alone
            record_id = (record.get("id") or "").strip()
            if record_id:
                for i, r in enumerate(records):
                    if (r.get("id") or "").strip() == record_id:
                        records[i] = record
                        break

    # Atomic write (refused if the file changed since load)
    _write_records_atomic(run_dir, records, payload, loaded_fp)

    return stats


def main() -> None:
    """CLI entry point for the post-run browser re-crawl pass."""
    parser = argparse.ArgumentParser(description="Post-run browser re-crawl pass")
    parser.add_argument("--run-dir", required=True, help="Path to the run directory")
    parser.add_argument(
        "--icp",
        default=None,
        help="Path to the ICP checklist JSON (falls back to icp_snapshot.json in run dir)",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        sys.exit(f"Run directory not found: {run_dir}")

    icp_path = Path(args.icp) if args.icp else None
    icp_signals = _load_icp_signals(icp_path, run_dir)

    print(f"Running browser re-crawl pass on {run_dir.name}...")
    try:
        stats = run_browser_recrawl_pass(run_dir, icp_signals)
    except ConcurrentRunChange as e:
        sys.exit(str(e))
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
