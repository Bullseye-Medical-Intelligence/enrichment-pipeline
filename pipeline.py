#!/usr/bin/env python3
"""
pipeline.py
Bullseye Enrichment Pipeline - Main Entry Point
================================================
Orchestrates all 8 pipeline steps per PIPELINE.md spec.

Usage:
    python pipeline.py --input data/outscraper_export.csv --source outscraper
    python pipeline.py --input data/manual_list.csv --source manual
    python pipeline.py --input data/export.csv --source outscraper --dry-run
    python pipeline.py --input data/export.csv --source outscraper --limit 10

Steps:
    1. INGEST       - Load CSV, normalize to canonical schema, dedup
    2. URL VALIDATE - HEAD requests, reachability check
    3. WEB EXTRACT  - requests + BeautifulSoup page text extraction
    4. SIGNAL EXTRACT (Claude) - LLM signal extraction, scoring, sales angles
    5. VERIFICATION (GPT) - Bullseye-tier records only
    6. EXCLUSION CHECK - Apply hard + configurable exclusion rules
    7. SCORING VALIDATION - Clamp scores, validate fields
    8. OUTPUT GENERATION - Write JSON, CSV, run_log.json
"""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Module imports
# ---------------------------------------------------------------------------
from ingestion.outscraper_adapter import load_outscraper_csv
from ingestion.manual_adapter import load_manual_csv
from extraction.url_validator import batch_validate_urls
from extraction.web_extractor import batch_extract
from enrichment.constants import DEFAULT_BULLSEYE_MIN_SCORE
from enrichment.signal_extractor import extract_signals
from enrichment.verifier import verify_bullseye_record
from enrichment.exclusion_checker import apply_exclusions, check_structural_exclusions
from enrichment.scorer import validate_and_finalize, strip_internal_fields
from output.json_writer import write_json
from output.csv_writer import write_csv
from output.log_writer import write_run_log

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PIPELINE_VERSION = "v1.0"
DEFAULT_CONFIG_PATH = "config/run_config.json"
DEFAULT_ICP_PATH = "config/icp_checklist.json"
DEFAULT_OUTPUT_DIR = "./output"


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _generate_run_id() -> str:
    """Generate a unique run ID based on timestamp."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"RUN-{ts}"


def _load_json_config(path: str) -> dict:
    """Load a JSON config file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _deduplicate_records(records: list[dict]) -> tuple[list[dict], int]:
    """
    Deduplicate records by ID.
    Returns (deduplicated_list, duplicates_removed_count).
    """
    seen_ids = {}
    deduped = []
    dupes = 0

    for record in records:
        rid = record.get("id", "")
        if rid in seen_ids:
            dupes += 1
        else:
            seen_ids[rid] = True
            deduped.append(record)

    return deduped, dupes


_checkpoint_lock = threading.Lock()


def _write_step4_checkpoint(output_dir: str, record: dict) -> None:
    """Append a completed Step 4 record to the NDJSON checkpoint file (best-effort, thread-safe)."""
    path = Path(output_dir) / "step4_checkpoint.ndjson"
    with _checkpoint_lock:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # Non-fatal: worst case is re-processing this record on resume


def _load_step4_checkpoint(output_dir: str) -> dict:
    """Return {record_id: record_dict} for all records already in the checkpoint.

    Handles a corrupted final line (process killed mid-write) by skipping bad JSON.
    """
    path = Path(output_dir) / "step4_checkpoint.ndjson"
    if not path.exists():
        return {}
    completed: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
            rid = rec.get("id") or rec.get("record_id")
            if rid:
                completed[rid] = rec
        except json.JSONDecodeError:
            pass  # Corrupted last line — that record will be re-processed
    return completed


def _write_progress(output_dir: str, step_num: int, step_name: str,
                     records_done: int = 0, records_total: int = 0) -> None:
    """Write current step to progress.json so the UI can poll it."""
    path = Path(output_dir) / "progress.json"
    data = {
        "step_num": step_num,
        "step_name": step_name,
        "step_total": 8,
        "records_done": records_done,
        "records_total": records_total,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass  # Non-fatal: progress display is best-effort


def _finalize_ingest_only(records: list[dict], run_config: dict) -> list[dict]:
    """Shape ingested records for output without crawling or calling any LLM.

    Each record is marked `enrichment_status = "not_enriched"` with zeroed
    scores and no signals, then run through the deterministic exclusion check
    (only structural rules fire — wrong specialty / outside geography / no web
    presence) and the standard validation pass so the output schema is complete.
    This is what `--ingest-only` writes: the full roster, ready for the operator
    to review before spending any crawl or LLM budget on enrichment.
    """
    finalized = []
    for record in records:
        record["enrichment_status"] = "not_enriched"
        record["bullseye_score"] = 0
        record["fit_signal_score"] = 0
        record["confidence_score"] = 0
        record["signals"] = []
        record = apply_exclusions(record, run_config)
        record = validate_and_finalize(record)
        finalized.append(record)
    return finalized


def _validate_required_fields(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Validate that records have minimum required fields.
    Returns (valid_records, invalid_records).
    """
    valid = []
    invalid = []
    for record in records:
        if not record.get("practice_name"):
            invalid.append({
                "record_id": record.get("id", "unknown"),
                "step": "ingestion",
                "error": "Missing required field: practice_name",
                "resolution": "Record dropped during ingestion validation",
            })
        else:
            valid.append(record)
    return valid, invalid


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(input_file: str, source_type: str,
                  output_dir: str = DEFAULT_OUTPUT_DIR,
                  config_path: str = DEFAULT_CONFIG_PATH,
                  icp_path: str = DEFAULT_ICP_PATH,
                  dry_run: bool = False,
                  limit: int = None,
                  use_playwright: bool = False,
                  ingest_only: bool = False) -> dict:
    """
    Run the full enrichment pipeline.

    Args:
        input_file: Path to input CSV file.
        source_type: "outscraper" or "manual".
        output_dir: Directory for output files.
        config_path: Path to run_config.json.
        icp_path: Path to icp_checklist.json.
        dry_run: If True, skip all LLM calls (parse + normalize only).
        limit: If set, process only the first N records.
        ingest_only: If True, ingest + normalize + structural exclusions only,
            then write the roster with every record marked "not_enriched" and
            exit before any crawl or LLM call. Enrichment is triggered later.

    Returns:
        Dict with run summary metrics.
    """
    run_id = _generate_run_id()
    start_time = time.time()

    print(f"\n{'='*60}")
    print("BULLSEYE ENRICHMENT PIPELINE")
    print(f"Run ID:  {run_id}")
    print(f"Input:   {input_file}")
    print(f"Source:  {source_type}")
    print(f"Dry run: {dry_run}")
    if limit:
        print(f"Limit:   {limit} records")
    print(f"{'='*60}\n")

    # Load configs
    print("Loading configuration...")
    run_config = _load_json_config(config_path)
    icp_data = _load_json_config(icp_path)
    icp_signals = icp_data.get("signals", [])

    timeout = run_config.get("request_timeout_seconds", 15)
    retries = run_config.get("request_retries", 3)
    max_pages = run_config.get("max_pages_per_practice", 5)
    bullseye_min = run_config.get("bullseye_min_score", DEFAULT_BULLSEYE_MIN_SCORE)
    subpage_keywords = run_config.get("subpage_keywords") or None
    io_concurrency = int(run_config.get("io_concurrency", 6))
    llm_concurrency = int(run_config.get("llm_concurrency", 3))

    print(f"  Project: {run_config.get('client_name', 'Unknown')}")
    print(f"  Target specialty: {run_config.get('target_specialty', 'Any')}")
    print(f"  Target geography: {run_config.get('target_geography', 'Any')}")
    print(f"  ICP signals loaded: {len(icp_signals)}")
    print(f"  Bullseye min score: {bullseye_min}")

    all_errors = []
    all_warnings = []

    # -------------------------------------------------------------------------
    # STEP 1: INGEST
    # -------------------------------------------------------------------------
    _write_progress(output_dir, 1, "Ingesting records")
    print(f"\n{'-'*40}")
    print("STEP 1: INGEST")
    print(f"{'-'*40}")

    if source_type == "outscraper":
        raw_records = load_outscraper_csv(input_file)
    elif source_type == "manual":
        raw_records = load_manual_csv(input_file)
    else:
        raise ValueError(f"Unknown source type: '{source_type}'. Use 'outscraper' or 'manual'.")

    records_input_total = len(raw_records)

    # Tag with pipeline metadata
    input_filename = Path(input_file).name
    for r in raw_records:
        r["raw_input_source"] = input_filename
        r["source_pipeline_version"] = PIPELINE_VERSION

    # Deduplicate
    records, dupes_removed = _deduplicate_records(raw_records)
    if dupes_removed > 0:
        msg = f"Removed {dupes_removed} duplicate records"
        all_warnings.append(msg)
        print(f"  [WARN] {msg}")

    # Validate required fields
    records, invalid = _validate_required_fields(records)
    all_errors.extend(invalid)
    if invalid:
        print(f"  [WARN] {len(invalid)} records dropped: missing required fields")

    # Apply limit for testing
    if limit and limit > 0:
        records = records[:limit]
        print(f"  Limit applied: processing {len(records)} of {records_input_total} records")

    print(f"\n  [OK] {len(records)} records ready for enrichment")

    if dry_run:
        print(f"\n{'-'*40}")
        print("DRY RUN MODE - Skipping Steps 2-8 (no LLM calls, no HTTP requests)")
        print(f"{'-'*40}")
        print(f"\n  Would process {len(records)} records.")
        for r in records[:5]:
            print(f"  -> {r['id']}: {r['practice_name']} ({r['address_city']}, {r['address_state']})")
        if len(records) > 5:
            print(f"  ... and {len(records) - 5} more")
        elapsed = time.time() - start_time
        print(f"\n  Dry run complete in {elapsed:.1f}s")
        return {"run_id": run_id, "records_processed": len(records), "dry_run": True}

    if ingest_only:
        print(f"\n{'-'*40}")
        print("INGEST-ONLY MODE - Writing roster (no crawl, no LLM)")
        print(f"{'-'*40}")
        output_records = _finalize_ingest_only(records, run_config)
        output_records = [strip_internal_fields(r) for r in output_records]
        for r in output_records:
            r["source_pipeline_version"] = PIPELINE_VERSION

        excluded_ingest = sum(1 for r in output_records if r.get("target_tier") == "Excluded")
        json_path = write_json(output_records, output_dir=output_dir, run_id=run_id)
        write_csv(output_records, output_dir=output_dir, pipeline_version=PIPELINE_VERSION)
        write_run_log(
            run_id=run_id,
            records=output_records,
            errors=all_errors,
            warnings=all_warnings,
            input_file=input_file,
            input_source_type=source_type,
            records_input=records_input_total,
            pipeline_version=PIPELINE_VERSION,
            output_dir=output_dir,
        )
        elapsed = time.time() - start_time
        print(f"\n  Ingest complete: {len(output_records)} records "
              f"({excluded_ingest} pre-excluded) in {elapsed:.1f}s")
        print(f"  Roster written: {json_path}")
        return {
            "run_id": run_id,
            "records_input": records_input_total,
            "records_output": len(output_records),
            "excluded": excluded_ingest,
            "ingest_only": True,
            "elapsed_seconds": round(elapsed, 1),
        }

    # -------------------------------------------------------------------------
    # STRUCTURAL PRE-FILTER (cost routing): records that deterministic
    # specialty/geography rules will exclude skip crawl + LLM entirely. They
    # rejoin the set at Step 6, where apply_exclusions formally marks them.
    # Signal-dependent exclusions still run later, unchanged.
    # -------------------------------------------------------------------------
    pre_excluded = []
    eligible = []
    for record in records:
        triggered, _ = check_structural_exclusions(record, run_config)
        (pre_excluded if triggered else eligible).append(record)
    if pre_excluded:
        print(f"\n  Pre-filter: {len(pre_excluded)} records skip enrichment "
              f"(wrong specialty / outside geography); {len(eligible)} eligible")
    records = eligible

    # -------------------------------------------------------------------------
    # STEP 2: URL VALIDATION
    # -------------------------------------------------------------------------
    _write_progress(output_dir, 2, "URL validation", 0, len(records))
    print(f"\n{'-'*40}")
    print("STEP 2: URL VALIDATION")
    print(f"{'-'*40}")

    records = batch_validate_urls(records, timeout=timeout, retries=retries,
                                   max_workers=io_concurrency)

    url_valid_count = sum(1 for r in records if r.get("_url_valid", False))
    print(f"\n  {url_valid_count}/{len(records)} URLs valid")

    # -------------------------------------------------------------------------
    # STEP 3: WEB EXTRACTION
    # -------------------------------------------------------------------------
    _write_progress(output_dir, 3, "Web extraction", 0, len(records))
    print(f"\n{'-'*40}")
    print("STEP 3: WEB EXTRACTION")
    print(f"{'-'*40}")

    records = batch_extract(records, timeout=timeout, retries=retries,
                             max_pages=max_pages, keywords=subpage_keywords,
                             max_workers=io_concurrency,
                             use_playwright=use_playwright)

    extracted_count = sum(1 for r in records if r.get("_context_text", ""))
    print(f"\n  {extracted_count}/{len(records)} records with extracted text")

    # -------------------------------------------------------------------------
    # STEP 4: SIGNAL EXTRACTION (Claude)
    # -------------------------------------------------------------------------
    _write_progress(output_dir, 4, "Signal extraction (Claude)", 0, len(records))
    print(f"\n{'-'*40}")
    print("STEP 4: SIGNAL EXTRACTION (Claude)")
    print(f"{'-'*40}")

    checkpoint = _load_step4_checkpoint(output_dir)
    if checkpoint:
        print(f"  Resuming from checkpoint: {len(checkpoint)} records already processed.")

    # Restore checkpoint records; collect only unprocessed records for the thread pool
    to_process = []
    for i, record in enumerate(records):
        record_id = record.get("id") or record.get("record_id", "")
        if record_id and record_id in checkpoint:
            records[i] = checkpoint[record_id]
            print(f"  [{i+1}/{len(records)}] {record.get('practice_name', 'Unknown')} — checkpoint")
        else:
            to_process.append((i, record))

    errors_lock = threading.Lock()
    checkpoint_start = len(checkpoint)

    def _extract_with_retry(idx: int, rec: dict) -> tuple[int, dict, str | None]:
        """Call extract_signals with exponential backoff on rate-limit errors."""
        context_text = rec.get("_context_text", "")
        for attempt in range(5):
            try:
                return idx, extract_signals(
                    record=rec,
                    icp_signals=icp_signals,
                    context_text=context_text,
                    run_id=run_id,
                    bullseye_min_score=bullseye_min,
                ), None
            except Exception as e:
                err_str = str(e)
                is_rate_limit = (
                    "429" in err_str
                    or "rate_limit" in err_str.lower()
                    or "rate limit" in err_str.lower()
                    or "overloaded" in err_str.lower()
                )
                if is_rate_limit and attempt < 4:
                    wait = min(5 * (2 ** attempt), 60)
                    print(f"    [RATE LIMIT] {rec.get('practice_name', '?')} — retrying in {wait}s")
                    time.sleep(wait)
                    continue
                return idx, rec, err_str[:200]
        return idx, rec, "Max retries exceeded"  # unreachable

    done_count = 0
    with ThreadPoolExecutor(max_workers=max(1, llm_concurrency)) as executor:
        futures = [executor.submit(_extract_with_retry, idx, rec) for idx, rec in to_process]
        for future in as_completed(futures):
            idx, record, error = future.result()
            done_count += 1
            total_done = checkpoint_start + done_count
            _write_progress(output_dir, 4, "Signal extraction (Claude)", total_done, len(records))
            print(f"\n  [{total_done}/{len(records)}] {record.get('practice_name', 'Unknown')}")

            if error is not None:
                print(f"    [FAIL] Unhandled error in signal extraction: {error}")
                record.update({
                    "signals": [],
                    "bullseye_score": 0,
                    "fit_signal_score": 0,
                    "confidence_score": 0,
                    "fit_confidence_status": "LOW FIT / LOW EVIDENCE",
                    "sales_angle": [],
                    "source_confidence": record.get("source_confidence") or "failed",
                    "enrichment_status": "failed",
                    "qc_status": "pending",
                    "internal_notes": f"Unhandled error: {error}",
                    "analyst_override_classification": None,
                    "override_reason": None,
                    "client_facing_rationale": None,
                    "_llm_exclusion_triggers": [],
                    "_llm_exclusion_rationale": "",
                })
                with errors_lock:
                    all_errors.append({
                        "record_id": record.get("id", "unknown"),
                        "step": "signal_extraction",
                        "error": error,
                        "resolution": "Record marked failed, enrichment_status=failed",
                    })

            records[idx] = record
            _write_step4_checkpoint(output_dir, record)

    # -------------------------------------------------------------------------
    # STEP 5: BULLSEYE VERIFICATION (GPT - conditional)
    # -------------------------------------------------------------------------
    bullseye_records = [
        r for r in records if r.get("bullseye_score", 0) >= bullseye_min
    ]

    if bullseye_records:
        _write_progress(output_dir, 5, "Bullseye verification (GPT)", 0, len(bullseye_records))
        print(f"\n{'-'*40}")
        print("STEP 5: BULLSEYE VERIFICATION (GPT)")
        print(f"{'-'*40}")
        print(f"  {len(bullseye_records)} records qualify for verification")

        for i, record in enumerate(bullseye_records):
            print(f"\n  [{i+1}/{len(bullseye_records)}] {record.get('practice_name', 'Unknown')} "
                  f"(score: {record.get('bullseye_score', 0)})")
            context_text = record.get("_context_text", "")
            try:
                record = verify_bullseye_record(record, context_text)
            except Exception as e:
                error_msg = str(e)[:200]
                print(f"    [FAIL] Verification error: {error_msg}")
                all_errors.append({
                    "record_id": record.get("id", "unknown"),
                    "step": "verification",
                    "error": error_msg,
                    "resolution": "Verification skipped, status unchanged",
                })
            time.sleep(0.5)

        needs_review = sum(
            1 for r in bullseye_records if r.get("enrichment_status") == "needs_review"
        )
        if needs_review > 0:
            all_warnings.append(
                f"{needs_review} Bullseye-tier records triggered LLM disagreement "
                f"and are flagged needs_review"
            )
    else:
        print(f"\n  STEP 5: SKIPPED - no records scored >= {bullseye_min}")

    # -------------------------------------------------------------------------
    # STEP 6: EXCLUSION CHECK
    # -------------------------------------------------------------------------
    # Rejoin records held out by the structural pre-filter so they are formally
    # excluded, tiered, and written to output alongside the enriched set.
    records = records + pre_excluded
    _write_progress(output_dir, 6, "Exclusion check", 0, len(records))
    print(f"\n{'-'*40}")
    print("STEP 6: EXCLUSION CHECK")
    print(f"{'-'*40}")

    for record in records:
        try:
            record = apply_exclusions(record, run_config)
        except Exception as e:
            error_msg = str(e)[:200]
            print(f"  [FAIL] Exclusion check error for {record.get('id', '?')}: {error_msg}")
            record["exclusion_status"] = "CLEAR"
            record["exclusion_reason"] = None
            all_errors.append({
                "record_id": record.get("id", "unknown"),
                "step": "exclusion_check",
                "error": error_msg,
                "resolution": "Exclusion check skipped, status set to CLEAR",
            })

    excluded_count = sum(1 for r in records if r.get("exclusion_status") == "EXCLUDED")
    print(f"\n  {excluded_count} records excluded")

    # -------------------------------------------------------------------------
    # STEP 7: SCORING VALIDATION
    # -------------------------------------------------------------------------
    _write_progress(output_dir, 7, "Scoring validation", 0, len(records))
    print(f"\n{'-'*40}")
    print("STEP 7: SCORING VALIDATION")
    print(f"{'-'*40}")

    for record in records:
        record = validate_and_finalize(record)

    # Count tiers
    bullseye_final = sum(1 for r in records if r.get("target_tier") == "Bullseye")
    watchlist_final = sum(1 for r in records if r.get("target_tier") == "Watchlist")
    excluded_final = sum(1 for r in records if r.get("target_tier") == "Excluded")
    print(f"  Tiers: {bullseye_final} Bullseye | {watchlist_final} Watchlist | {excluded_final} Excluded")

    # Strip internal fields before output
    output_records = [strip_internal_fields(r) for r in records]

    # Inject pipeline version into all records
    for r in output_records:
        r["source_pipeline_version"] = PIPELINE_VERSION

    # -------------------------------------------------------------------------
    # STEP 8: OUTPUT GENERATION
    # -------------------------------------------------------------------------
    _write_progress(output_dir, 8, "Writing output files", 0, len(records))
    print(f"\n{'-'*40}")
    print("STEP 8: OUTPUT GENERATION")
    print(f"{'-'*40}")

    json_path = write_json(output_records, output_dir=output_dir, run_id=run_id)
    csv_path = write_csv(output_records, output_dir=output_dir,
                          pipeline_version=PIPELINE_VERSION)
    log_path = write_run_log(
        run_id=run_id,
        records=output_records,
        errors=all_errors,
        warnings=all_warnings,
        input_file=input_file,
        input_source_type=source_type,
        records_input=records_input_total,
        pipeline_version=PIPELINE_VERSION,
        output_dir=output_dir,
    )

    elapsed = time.time() - start_time

    print(f"\n{'='*60}")
    print(f"RUN COMPLETE: {run_id}")
    print(f"  Input:         {records_input_total} records")
    print(f"  Output:        {len(output_records)} records")
    print(f"  Bullseye:      {bullseye_final}")
    print(f"  Watchlist:     {watchlist_final}")
    print(f"  Excluded:      {excluded_final}")
    print(f"  Errors:        {len(all_errors)}")
    print(f"  Warnings:      {len(all_warnings)}")
    print(f"  Elapsed:       {elapsed:.1f}s")
    print("\n  Outputs:")
    print(f"    {json_path}")
    print(f"    {csv_path}")
    print(f"    {log_path}")
    print(f"{'='*60}\n")

    return {
        "run_id": run_id,
        "records_input": records_input_total,
        "records_output": len(output_records),
        "bullseye": bullseye_final,
        "watchlist": watchlist_final,
        "excluded": excluded_final,
        "errors": len(all_errors),
        "elapsed_seconds": round(elapsed, 1),
        "json_path": json_path,
        "csv_path": csv_path,
        "log_path": log_path,
    }


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bullseye Enrichment Pipeline - convert raw prospect lists to intelligence",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pipeline.py --input data/outscraper_export.csv --source outscraper
  python pipeline.py --input data/manual_list.csv --source manual
  python pipeline.py --input data/export.csv --source outscraper --dry-run
  python pipeline.py --input data/export.csv --source outscraper --limit 5
        """,
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to input CSV file",
    )
    parser.add_argument(
        "--source", "-s",
        required=True,
        choices=["outscraper", "manual"],
        help="Input source type: 'outscraper' or 'manual'",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to run_config.json (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--icp",
        default=DEFAULT_ICP_PATH,
        help=f"Path to icp_checklist.json (default: {DEFAULT_ICP_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and normalize only - no LLM calls, no HTTP requests",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N records (for testing)",
    )
    parser.add_argument(
        "--playwright",
        action="store_true",
        help="Use headless Chromium (Playwright) instead of requests for web extraction",
    )
    parser.add_argument(
        "--ingest-only",
        action="store_true",
        help="Ingest + normalize + structural exclusions only; write the roster "
             "(all records 'not_enriched') and exit before any crawl or LLM call",
    )

    args = parser.parse_args()

    # Validate input file exists
    if not Path(args.input).exists():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    # Validate config files exist
    for cfg_path, name in [(args.config, "run_config"), (args.icp, "icp_checklist")]:
        if not Path(cfg_path).exists():
            print(f"ERROR: Config file not found: {cfg_path}", file=sys.stderr)
            sys.exit(1)

    run_pipeline(
        input_file=args.input,
        source_type=args.source,
        output_dir=args.output_dir,
        config_path=args.config,
        icp_path=args.icp,
        dry_run=args.dry_run,
        limit=args.limit,
        use_playwright=args.playwright,
        ingest_only=args.ingest_only,
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
