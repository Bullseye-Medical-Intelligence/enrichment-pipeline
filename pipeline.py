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
from ingestion import npi_lookup
from ingestion.customer_suppression import load_suppression_list, check_suppression
from extraction.url_validator import batch_validate_urls
from extraction.web_extractor import batch_extract
from enrichment.constants import (
    DEFAULT_BULLSEYE_MIN_SCORE,
    DEFAULT_NEAR_MISS_BAND,
    MIN_CONTEXT_CHARS,
)
from enrichment.signal_extractor import extract_signals
from enrichment.verifier import verify_bullseye_record, generate_sales_brief
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


def _finalize_ingest_only(records: list[dict]) -> list[dict]:
    """Shape ingested records for output without crawling or calling any LLM.

    Every record is imported as CLEAR/not_enriched — no exclusions fire at
    import time. Structural exclusions (wrong_specialty, outside_geography) only
    run during the full enrichment pass once actual crawl data is available.
    The standard validation pass completes the output schema.
    """
    finalized = []
    for record in records:
        record["enrichment_status"] = "not_enriched"
        record["bullseye_score"] = 0
        record["fit_signal_score"] = 0
        record["confidence_score"] = 0
        record["signals"] = []
        record["exclusion_status"] = "CLEAR"
        record["exclusion_reason"] = None
        record["target_tier"] = "Contender"
        record = validate_and_finalize(record)
        finalized.append(record)
    return finalized


def _records_needing_browser_retry(records: list[dict]) -> list[dict]:
    """Select records whose standard crawl came back blocked or too thin.

    A record qualifies when it has a URL but the requests-based extractor
    produced weak source data: source_confidence of "limited"/"failed", or less
    than MIN_CONTEXT_CHARS of usable text. These are exactly the records a
    headless-browser re-crawl can recover (JS challenges / soft bot gates).
    Records with no URL are skipped — a browser cannot help them.
    """
    blocked = []
    for record in records:
        if not record.get("website_url"):
            continue
        thin_context = len(record.get("_context_text", "") or "") < MIN_CONTEXT_CHARS
        weak_source = record.get("source_confidence") in ("limited", "failed")
        if thin_context or weak_source:
            blocked.append(record)
    return blocked


def _record_has_uncertain_signal(record: dict) -> bool:
    """Return True when a Bullseye record has at least one low-confidence YES signal.

    Low-confidence extraction is the false-positive risk GPT is designed to catch.
    All-medium/high-confidence records skip GPT — when Claude is certain, GPT agrees
    and the API call wastes spend.
    """
    signals = record.get("signals") or []
    return any(
        s.get("signal_state") == "yes" and s.get("confidence") == "low"
        for s in signals
    )


def _select_verification_records(
    records: list[dict], bullseye_min: int, near_miss_band: int
) -> list[dict]:
    """Select records for GPT verification (Step 5).

    Thin-context records (`source_confidence` limited/failed) are always skipped:
    the tier will be capped at Needs Verification in Step 6 regardless, and GPT
    sees the same limited text Claude already processed.

    Near-miss records (score in [bullseye_min - near_miss_band, bullseye_min))
    are always verified — borderline scores are the highest-value GPT target.

    Bullseye records (score >= bullseye_min) are only verified when Claude showed
    genuine uncertainty: at least one confirmed-YES signal extracted at low
    confidence. All-medium/high-confidence Bullseyes are skipped.

    band 0 (default): only uncertain Bullseye records are verified.
    band N > 0: also verifies records scoring [bullseye_min − N, bullseye_min).
    """
    near_miss_band = max(0, near_miss_band)
    verification_floor = bullseye_min - near_miss_band
    result = []
    for r in records:
        score = r.get("bullseye_score", 0)
        if score < verification_floor:
            continue
        # Thin-context: skip at all score levels — same reasoning, same limited text.
        if r.get("source_confidence") in ("limited", "failed"):
            continue
        if score >= bullseye_min:
            if _record_has_uncertain_signal(r):
                result.append(r)
        else:
            # Near-miss band: always verify regardless of confidence level.
            result.append(r)
    return result


def _load_manual_content(records: list[dict], manual_content_paths: list[str]) -> None:
    """Populate records' context text from operator-provided files, no crawl.

    For sites blocked by a hard CAPTCHA wall, the operator captures the page(s)
    in their own browser (Save Page As .html, or copy the visible text) and
    supplies them here. The content replaces Steps 2-3 (URL validation + web
    extraction): it is loaded into every record's `_context_text` so Step 4
    signal extraction runs on it exactly as if the crawler had fetched it. HTML
    is converted to clean text with the same extractor the browser crawler uses;
    plain text is used as-is. Multiple pages are joined with the same separator
    the crawler uses and capped at MAX_COMBINED_CHARS. source_confidence is
    "partial" — operator-vouched but not a full crawl.
    """
    from extraction.playwright_extractor import _extract_text_from_html
    from extraction.web_extractor import MAX_COMBINED_CHARS

    blocks = []
    page_labels = []
    for path in manual_content_paths:
        raw = Path(path).read_bytes().decode("utf-8", errors="replace")
        is_html = (
            path.lower().endswith((".html", ".htm"))
            or any(tag in raw[:4000].lower() for tag in ("<html", "<body", "<div", "<!doctype"))
        )
        clean_text = _extract_text_from_html(raw) if is_html else raw.strip()
        if clean_text:
            blocks.append(clean_text)
            page_labels.append(f"[Manual content] {Path(path).name}")

    combined = "\n\n---\n\n".join(blocks)
    if len(combined) > MAX_COMBINED_CHARS:
        combined = combined[:MAX_COMBINED_CHARS] + "\n\n[... truncated for token budget ...]"

    for record in records:
        record["_context_text"] = combined
        record["_pages_crawled"] = list(page_labels)
        record["_url_valid"] = True
        record["_url_error"] = ""
        # Operator-supplied content: trustworthy enough to enrich, but not a full
        # multi-page crawl, so cap honesty at "partial".
        record["source_confidence"] = "partial"


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
                  auto_browser_retry: bool = False,
                  manual_content_path: list[str] = None,
                  ingest_only: bool = False,
                  run_id: str = None) -> dict:
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
        use_playwright: If True, crawl every record with headless Chromium.
        auto_browser_retry: If True, after the standard crawl, re-crawl any
            blocked/thin records once with headless Chromium before signal
            extraction. No effect when use_playwright is already True.
        manual_content_path: If set, a list of paths to operator-provided HTML or
            text files (one per page). Replaces URL validation + web extraction
            (Steps 2-3) by loading that content into every record's context, then
            runs signal extraction on it. For single-record manual enrichment of
            CAPTCHA-blocked sites.
        ingest_only: If True, ingest + normalize + structural exclusions only,
            then write the roster with every record marked "not_enriched" and
            exit before any crawl or LLM call. Enrichment is triggered later.
        run_id: If set, use this run identifier instead of generating one. The
            API passes its own run_id so the ID in the output files matches the
            run directory it tracks; a bare CLI invocation generates its own.

    Returns:
        Dict with run summary metrics.
    """
    run_id = run_id or _generate_run_id()
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
    near_miss_band = run_config.get("verify_near_miss_band", DEFAULT_NEAR_MISS_BAND)
    subpage_keywords = run_config.get("subpage_keywords") or None
    io_concurrency = int(run_config.get("io_concurrency", 6))
    llm_concurrency = int(run_config.get("llm_concurrency", 3))

    # Config can enable auto browser-retry without a CLI flag; the flag forces it on.
    auto_browser_retry = auto_browser_retry or bool(run_config.get("auto_browser_retry", False))

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

    # -------------------------------------------------------------------------
    # STEP 1b: NPI ENRICHMENT
    # Populate registry-derived fields (taxonomy codes, REI flag) from the
    # public NPPES database before the structural pre-filter runs. The REI
    # taxonomy gate in check_structural_exclusions reads rei_taxonomy_present,
    # so NPI enrichment must complete first to let confirmed REI practices
    # skip the crawl rather than being excluded only after LLM spend.
    # Runs before ingest-only exit so the roster carries NPI fields.
    # Skip when npi_enrichment_enabled is explicitly False in run_config.
    # -------------------------------------------------------------------------
    if run_config.get("npi_enrichment_enabled", True):
        _write_progress(output_dir, 1, "NPI enrichment")
        print(f"\n{'-'*40}")
        print("STEP 1b: NPI ENRICHMENT (NPPES registry lookup)")
        print(f"{'-'*40}")
        npi_lookup.enrich_records(records, run_config)

    # -------------------------------------------------------------------------
    # STEP 1c: CUSTOMER SUPPRESSION
    # Exclude existing customers before any crawl or LLM spend. Optional:
    # skipped when suppression_list_path is absent from run_config. Runs before
    # the ingest-only exit so suppressed records appear as EXCLUDED in every
    # roster view rather than surfacing as prospects the operator has to skip.
    # -------------------------------------------------------------------------
    customer_suppressed: list[dict] = []
    suppression_path = run_config.get("suppression_list_path")
    if suppression_path:
        _write_progress(output_dir, 1, "Customer suppression check")
        print(f"\n{'-'*40}")
        print("STEP 1c: CUSTOMER SUPPRESSION")
        print(f"{'-'*40}")
        suppression_list = load_suppression_list(suppression_path)
        if not suppression_list.is_empty:
            remaining: list[dict] = []
            for record in records:
                is_suppressed, reason = check_suppression(record, suppression_list)
                if is_suppressed:
                    record["_customer_suppressed"] = True
                    record["_suppression_reason"] = reason
                    customer_suppressed.append(record)
                else:
                    remaining.append(record)
            records = remaining
            if customer_suppressed:
                print(f"\n  {len(customer_suppressed)} records suppressed "
                      f"(existing customers); {len(records)} remaining")
            else:
                print(f"\n  0 records matched suppression list; {len(records)} remaining")
        else:
            print(f"  [WARN] Suppression list at {suppression_path} is empty or unreadable")

    if ingest_only:
        print(f"\n{'-'*40}")
        print("INGEST-ONLY MODE - Writing roster (no crawl, no LLM)")
        print(f"{'-'*40}")
        output_records = _finalize_ingest_only(records)
        for r in customer_suppressed:
            r["enrichment_status"] = "not_enriched"
            r["bullseye_score"] = 0
            r["fit_signal_score"] = 0
            r["confidence_score"] = 0
            r["signals"] = []
            r["exclusion_status"] = "EXCLUDED"
            r["exclusion_reason"] = r.get("_suppression_reason") or "Existing customer"
            r["target_tier"] = "Excluded"
            r = validate_and_finalize(r)
            output_records.append(r)
        output_records = [strip_internal_fields(r) for r in output_records]
        for r in output_records:
            r["source_pipeline_version"] = PIPELINE_VERSION
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
        print(f"\n  Ingest complete: {len(output_records)} records in {elapsed:.1f}s")
        print(f"  Roster written: {json_path}")
        return {
            "run_id": run_id,
            "records_input": records_input_total,
            "records_output": len(output_records),
            "excluded": len(customer_suppressed),
            "ingest_only": True,
            "elapsed_seconds": round(elapsed, 1),
        }

    # -------------------------------------------------------------------------
    # STRUCTURAL PRE-FILTER (cost routing): records that deterministic
    # specialty/geography/REI-taxonomy rules will exclude skip crawl + LLM
    # entirely. They rejoin the set at Step 6, where apply_exclusions formally
    # marks them. Signal-dependent exclusions still run later, unchanged.
    # -------------------------------------------------------------------------
    pre_excluded = []
    eligible = []
    for record in records:
        triggered, _ = check_structural_exclusions(record, run_config)
        (pre_excluded if triggered else eligible).append(record)
    if pre_excluded:
        print(f"\n  Pre-filter: {len(pre_excluded)} records skip enrichment "
              f"(wrong specialty / outside geography / REI taxonomy); "
              f"{len(eligible)} eligible")
    records = eligible

    if manual_content_path:
        # -------------------------------------------------------------------------
        # MANUAL CONTENT MODE: operator-provided page content replaces Steps 2-3.
        # Used to enrich a single CAPTCHA-blocked site the crawler cannot reach.
        # -------------------------------------------------------------------------
        _write_progress(output_dir, 3, "Loading manual content", 0, len(records))
        print(f"\n{'-'*40}")
        print("STEPS 2-3: MANUAL CONTENT (no crawl)")
        print(f"{'-'*40}")
        _load_manual_content(records, manual_content_path)
        loaded = sum(1 for r in records if r.get("_context_text", ""))
        print(f"\n  Loaded operator content into {loaded}/{len(records)} record(s) "
              f"from {len(manual_content_path)} page(s)")
    else:
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
    # STEP 3b: AUTO BROWSER-RETRY (opt-in)
    # Records the standard crawler could not reach (bot gates, JS challenges)
    # come back blocked/thin. Re-crawl just those once with headless Chromium
    # before spending LLM budget, so blocked sites are recovered automatically
    # instead of waiting for an operator to click "Re-crawl with Browser".
    # No-op when the run is already Playwright-based or in manual-content mode.
    # -------------------------------------------------------------------------
    if auto_browser_retry and not use_playwright and not manual_content_path:
        blocked = _records_needing_browser_retry(records)
        if blocked:
            _write_progress(output_dir, 3, "Browser retry (blocked sites)", 0, len(blocked))
            print(f"\n{'-'*40}")
            print(f"STEP 3b: BROWSER RETRY ({len(blocked)} blocked/thin sites)")
            print(f"{'-'*40}")
            before = sum(1 for r in blocked if r.get("_context_text", ""))
            batch_extract(blocked, timeout=timeout, retries=retries,
                          max_pages=max_pages, keywords=subpage_keywords,
                          max_workers=io_concurrency, use_playwright=True)
            after = sum(1 for r in blocked if r.get("_context_text", ""))
            print(f"\n  Browser retry recovered {after - before} of {len(blocked)} blocked records")
        else:
            print("\n  No blocked/thin records — skipping browser retry")

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
                    target_specialty=run_config.get("target_specialty", ""),
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
    # STEP 5: GPT SECOND PASS — verification (Bullseye) + sales brief (all)
    # -------------------------------------------------------------------------
    verification_floor = bullseye_min - max(0, near_miss_band)
    bullseye_records = _select_verification_records(records, bullseye_min, near_miss_band)
    # Sales brief candidates: enriched records with confirmed signals that aren't
    # already flagged as LLM-detected exclusions (avoid spending GPT on soon-to-
    # be-excluded records). Bullseye records get both verification + sales brief.
    brief_candidates = [
        r for r in records
        if r.get("enrichment_status") not in ("not_enriched", "failed")
        and not r.get("_llm_exclusion_triggers")
        and any(
            s.get("signal_state") == "yes"
            for s in (r.get("signals") or [])
        )
    ]
    print(f"\n{'-'*40}")
    print("STEP 5: GPT SECOND PASS")
    print(f"{'-'*40}")
    if near_miss_band > 0:
        print(f"  {len(bullseye_records)} records scored >= {verification_floor} "
              f"(Bullseye {bullseye_min} + near-miss band {near_miss_band}) → verification")
    else:
        print(f"  {len(bullseye_records)} Bullseye records → verification")
    print(f"  {len(brief_candidates)} records → practice-specific sales brief")

    _write_progress(output_dir, 5, "GPT second pass", 0,
                    len(bullseye_records) + len(brief_candidates))

    # 5a — Bullseye verification
    if bullseye_records:
        for i, record in enumerate(bullseye_records):
            print(f"\n  [V {i+1}/{len(bullseye_records)}] {record.get('practice_name', 'Unknown')} "
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
        print(f"  No records scored >= {verification_floor} — verification skipped")

    # 5b — Practice-specific sales brief for all enriched candidates
    if brief_candidates:
        for i, record in enumerate(brief_candidates):
            print(f"\n  [B {i+1}/{len(brief_candidates)}] {record.get('practice_name', 'Unknown')}")
            context_text = record.get("_context_text", "")
            try:
                record = generate_sales_brief(record, context_text, run_config)
            except Exception as e:
                error_msg = str(e)[:200]
                print(f"    [FAIL] Sales brief error: {error_msg}")
                all_errors.append({
                    "record_id": record.get("id", "unknown"),
                    "step": "sales_brief",
                    "error": error_msg,
                    "resolution": "Sales brief skipped, Claude-generated content preserved",
                })
            time.sleep(0.3)
    else:
        print("  No enriched records with confirmed signals — sales brief skipped")

    # -------------------------------------------------------------------------
    # STEP 6: EXCLUSION CHECK
    # -------------------------------------------------------------------------
    # Rejoin records held out by the structural pre-filter and customer suppression
    # so they are formally excluded, tiered, and written to output alongside the
    # enriched set. apply_exclusions handles the _customer_suppressed flag.
    records = records + pre_excluded + customer_suppressed
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
    contender_final = sum(1 for r in records if r.get("target_tier") == "Contender")
    excluded_final = sum(1 for r in records if r.get("target_tier") == "Excluded")
    print(f"  Tiers: {bullseye_final} Bullseye | {contender_final} Contender | {excluded_final} Excluded")

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
    print(f"  Contender:     {contender_final}")
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
        "contender": contender_final,
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
        "--auto-browser-retry",
        action="store_true",
        help="After the standard crawl, re-crawl blocked/thin sites once with "
             "headless Chromium before signal extraction (recovers bot-gated sites "
             "automatically). Ignored when --playwright is set.",
    )
    parser.add_argument(
        "--manual-content-path",
        action="append",
        default=None,
        help="Path to an operator-provided HTML or text file. Replaces URL "
             "validation + web extraction: loads that content into the record(s) "
             "and runs signal extraction on it. Pass once per page to supply "
             "multiple pages (Home, About, Providers). For manual enrichment of "
             "CAPTCHA-blocked sites.",
    )
    parser.add_argument(
        "--ingest-only",
        action="store_true",
        help="Ingest + normalize + structural exclusions only; write the roster "
             "(all records 'not_enriched') and exit before any crawl or LLM call",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Use this run identifier instead of generating one (the API passes "
             "its own so output files match the tracked run directory)",
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
        auto_browser_retry=args.auto_browser_retry,
        manual_content_path=args.manual_content_path,
        ingest_only=args.ingest_only,
        run_id=args.run_id,
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
