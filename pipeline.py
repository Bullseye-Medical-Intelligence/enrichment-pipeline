#!/usr/bin/env python3
"""
pipeline.py
Bullseye Enrichment Pipeline - Main Entry Point
================================================
Orchestrates all 8 pipeline steps per PIPELINE.md spec.

Usage:
    python pipeline.py --input data/outscraper_export.csv --source outscraper
    python pipeline.py --input data/apify_places_export.csv --source apify_places
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
import hashlib
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
from ingestion.apify_places_adapter import load_apify_places_csv
from ingestion import npi_lookup
from ingestion.consolidator import consolidate_records
from ingestion.customer_suppression import load_suppression_list, check_suppression
from extraction.url_validator import batch_validate_urls
from extraction.web_extractor import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    MAX_CRAWL_PAGES,
    batch_extract,
)
from enrichment.constants import (
    DEFAULT_BULLSEYE_MIN_SCORE,
    MIN_CONTEXT_CHARS,
)
from enrichment.config_validator import validate_icp, validate_run_config
from enrichment.signal_extractor import LLMAccountError, extract_signals
from enrichment.exclusion_checker import (
    ExclusionCanaryTripped,
    apply_exclusions,
    build_exclusion_canary_report,
    build_exclusion_canary_state,
    check_structural_exclusions,
)
from enrichment.scorer import validate_and_finalize, strip_internal_fields
from output.json_writer import write_json
from output.csv_writer import write_csv
from output.log_writer import write_run_log
from output.evidence_writer import write_record_evidence

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


def _validate_icp_signals(icp_signals: list[dict]) -> None:
    """Thin shim kept for callers; delegates to enrichment.config_validator."""
    validate_icp({"signals": icp_signals})


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


def _checkpoint_path(output_dir: str) -> Path:
    """Path to the Step 4 crash-recovery checkpoint for this output directory."""
    return Path(output_dir) / "step4_checkpoint.ndjson"


def _checkpoint_fingerprint(input_file: str, config_path: str, icp_path: str,
                            crawl_mode: str = "") -> str:
    """Identify the inputs a checkpoint's records were produced from.

    Record ids are deterministic content hashes, so without this a later run in
    the same output directory would match every id and silently restore the
    previous run's signals — scored against the OLD ICP weights, with no Claude
    calls made. The ICP and config are hashed by content (an edited weight or
    flag must invalidate); the input CSV by identity + size + mtime, which is
    enough to catch a different or re-exported list without re-reading it.
    crawl_mode captures how the page text was obtained (HTTP vs browser vs
    manual content) — the same list re-run with --playwright must re-extract,
    not resume from thin HTTP-crawl results.
    """
    h = hashlib.sha256()
    for path in (config_path, icp_path):
        try:
            h.update(Path(path).read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\x00")
    try:
        st = os.stat(input_file)
        h.update(f"{Path(input_file).name}|{st.st_size}|{st.st_mtime_ns}".encode())
    except OSError:
        h.update(b"<no-input>")
    h.update(b"\x00" + crawl_mode.encode())
    return h.hexdigest()[:16]


def _write_step4_checkpoint(output_dir: str, record: dict, fingerprint: str = "") -> None:
    """Append a completed Step 4 record to the NDJSON checkpoint file (best-effort, thread-safe).

    When a fingerprint is supplied, the append is skipped unless the file's
    header still carries it: with the fixed ./output default, a still-running
    older process could otherwise append its old-ICP records under a newer
    run's re-stamped header, mixing two runs' records in one checkpoint.
    """
    path = _checkpoint_path(output_dir)
    with _checkpoint_lock:
        try:
            if fingerprint and _read_checkpoint_stamp(path) != fingerprint:
                return  # another run owns this checkpoint now
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # Non-fatal: worst case is re-processing this record on resume


def _read_checkpoint_stamp(path: Path) -> str:
    """The fingerprint stamped on a checkpoint's first line, or "" when absent."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = json.loads(f.readline())
        return head.get("_checkpoint_fingerprint", "") if isinstance(head, dict) else ""
    except (OSError, json.JSONDecodeError):
        return ""


def _init_step4_checkpoint(output_dir: str, fingerprint: str) -> None:
    """Start a fresh checkpoint stamped with the inputs it belongs to."""
    path = _checkpoint_path(output_dir)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"_checkpoint_fingerprint": fingerprint}) + "\n")
    except OSError:
        pass  # Non-fatal: the run proceeds without resume capability


def _clear_step4_checkpoint(output_dir: str) -> None:
    """Delete the checkpoint. Called on successful completion.

    The checkpoint exists only to resume a killed run; once the run has written
    its output it is stale state that a later run must never inherit.
    """
    try:
        _checkpoint_path(output_dir).unlink()
    except OSError:
        pass


def _load_step4_checkpoint(output_dir: str, fingerprint: str) -> dict:
    """Return {record_id: record_dict} for records already processed for THESE inputs.

    A checkpoint whose fingerprint does not match the current input/config/ICP
    belongs to a different run: it is discarded (and the file removed, so new
    appends never mix two runs' records). Handles a corrupted final line
    (process killed mid-write) by skipping bad JSON.
    """
    path = _checkpoint_path(output_dir)
    if not path.exists():
        return {}
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # A process killed mid-append can truncate inside a multibyte
        # character. Decode leniently so only the torn tail line fails JSON
        # parse below (and is re-processed) instead of resume crashing with
        # UnicodeDecodeError on every retry.
        text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()

    stamped = ""
    if lines:
        try:
            head = json.loads(lines[0])
            if isinstance(head, dict) and "_checkpoint_fingerprint" in head:
                stamped = head["_checkpoint_fingerprint"]
                lines = lines[1:]
        except json.JSONDecodeError:
            pass
    if stamped != fingerprint:
        # Reusing a mismatched checkpoint would restore signals scored against
        # a different ICP. Name the actual reason: an unstamped file is a
        # pre-upgrade checkpoint (resume cannot verify what produced it), not
        # evidence the operator changed anything.
        if stamped:
            print("  Discarding a checkpoint written for different inputs "
                  "(config, ICP, or input file changed) — re-extracting from scratch.")
        else:
            print("  Discarding a checkpoint from an older pipeline version "
                  "(no input fingerprint recorded, so resume cannot verify it matches "
                  "this run) — re-extracting from scratch. This happens once after "
                  "upgrading.")
        try:
            path.unlink()
        except OSError:
            pass
        return {}

    completed: dict = {}
    for line in lines:
        try:
            rec = json.loads(line)
            # Skip failed rows on load too, not just on write. A failed record is not
            # completed work; ignoring it here re-attempts it on resume and applies
            # the same retry behavior to checkpoints written by older versions that
            # did persist failures.
            if rec.get("enrichment_status") == "failed":
                continue
            rid = rec.get("id") or rec.get("record_id")
            if rid:
                completed[rid] = rec
        except json.JSONDecodeError:
            pass  # Corrupted last line — that record will be re-processed
    return completed


# Tracks when the CURRENT step began so the UI can compute a within-step rate
# and ETA. Written only from the step-collection threads (one at a time), so a
# plain module dict suffices.
_progress_step_state: dict = {"key": None, "started_at": None}


def _write_progress(output_dir: str, step_num: int, step_name: str,
                     records_done: int = 0, records_total: int = 0) -> None:
    """Write current step to progress.json so the UI can poll it.

    Stamps step_started_at when the (step_num, step_name) pair changes, so a
    reader can derive records/minute and time remaining for the running step —
    elapsed-since-run-start alone says nothing about a long step's progress.
    """
    path = Path(output_dir) / "progress.json"
    step_key = (step_num, step_name)
    if _progress_step_state["key"] != step_key:
        _progress_step_state["key"] = step_key
        _progress_step_state["started_at"] = datetime.now(timezone.utc).isoformat()
    data = {
        "step_num": step_num,
        "step_name": step_name,
        "step_total": 8,
        "records_done": records_done,
        "records_total": records_total,
        "step_started_at": _progress_step_state["started_at"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass  # Non-fatal: progress display is best-effort


def _step_progress(output_dir: str, step_num: int, step_name: str):
    """Build a per-record progress callback that updates progress.json for a step."""
    return lambda done, total: _write_progress(output_dir, step_num, step_name, done, total)


def _finalize_ingest_only(records: list[dict], run_config: dict) -> list[dict]:
    """Shape ingested records for output without crawling or calling any LLM.

    Structural exclusions ARE evaluated here. They are decided entirely from the
    ingested row — specialty, geography, NPI taxonomy, missing website — so
    nothing a crawl could learn changes them, and withholding them until the
    enrichment pass left the roster showing accounts that the very next step
    would drop. An operator reads a billable count off this roster; a count that
    sheds accounts later is the count a client argues with.

    Signal-driven exclusions are NOT evaluated: those need a crawl. The standard
    validation pass completes the output schema.
    """
    finalized = []
    for record in records:
        record["enrichment_status"] = "not_enriched"
        record["bullseye_score"] = 0
        record["fit_signal_score"] = 0
        record["confidence_score"] = 0
        record["signals"] = []
        triggered, rationale = check_structural_exclusions(record, run_config)
        if triggered:
            record["exclusion_status"] = "EXCLUDED"
            record["exclusion_reason"] = " ".join(rationale)
            record["target_tier"] = "Excluded"
            # Internal (leading underscore, stripped before output): lets the
            # caller's canary name which rules emptied a roster, not just how many.
            record["_structural_triggers"] = list(triggered)
        else:
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


def _load_manual_content(records: list[dict], manual_content_paths: list[str]) -> None:
    """Populate records' context text from operator-provided files, no crawl.

    For sites blocked by a hard CAPTCHA wall, the operator captures the page(s)
    in their own browser (Save Page As .html, or copy the visible text) and
    supplies them here. The content replaces Steps 2-3 (URL validation + web
    extraction): it is loaded into every record's `_context_text` so Step 4
    signal extraction runs on it exactly as if the crawler had fetched it. HTML
    is converted to clean text with the same extractor the browser crawler uses;
    plain text is used as-is. Each page is wrapped as "[Source: <url>]\n<text>"
    — the same shape a live crawl produces — using the record's own website_url,
    so Step 4's evidence gate can accept a "yes" (the model is told to cite that
    header as source_url; without it every "yes" is force-downgraded because
    source_url comes back "not_found"). Pages are joined with the crawler's
    separator and capped at MAX_COMBINED_CHARS. source_confidence is "partial" —
    operator-vouched but not a full crawl.
    """
    from extraction.playwright_extractor import _extract_text_from_html
    from extraction.web_extractor import MAX_COMBINED_CHARS

    page_texts = []
    page_labels = []
    for path in manual_content_paths:
        raw = Path(path).read_bytes().decode("utf-8", errors="replace")
        is_html = (
            path.lower().endswith((".html", ".htm"))
            or any(tag in raw[:4000].lower() for tag in ("<html", "<body", "<div", "<!doctype"))
        )
        clean_text = _extract_text_from_html(raw) if is_html else raw.strip()
        if clean_text:
            page_texts.append(clean_text)
            page_labels.append(f"[Manual content] {Path(path).name}")

    for record in records:
        # Attribute the pasted content to the record's own (CAPTCHA-walled) URL.
        # website_url is normalized to an http(s) URL at ingest, so this satisfies
        # the http(s) source requirement in the Step 4 evidence gate. When a record
        # has no URL there is nothing to attribute to — leave the text unheadered
        # and let the gate fire (the claim would be genuinely unverifiable).
        source_url = (record.get("website_url") or "").strip()
        if source_url:
            blocks = [f"[Source: {source_url}]\n{text}" for text in page_texts]
            record["_evidence_pages"] = [{"url": source_url, "text": text} for text in page_texts]
        else:
            blocks = list(page_texts)
            record["_evidence_pages"] = [{"url": "", "text": text} for text in page_texts]
        combined = "\n\n---\n\n".join(blocks)
        if len(combined) > MAX_COMBINED_CHARS:
            combined = combined[:MAX_COMBINED_CHARS] + "\n\n[... truncated for token budget ...]"
        record["_context_text"] = combined
        record["_pages_crawled"] = list(page_labels)
        record["_evidence_provenance"] = "operator_supplied"
        record["_url_valid"] = True
        record["_url_error"] = ""
        # Operator-supplied content: trustworthy enough to enrich, but not a full
        # multi-page crawl, so cap honesty at "partial".
        record["source_confidence"] = "partial"


def _sync_to_drive(output_dir: str, drive_config: dict) -> None:
    """Sync the output directory to Google Drive via rclone (best-effort, never blocks the run)."""
    import subprocess

    remote = drive_config.get("remote", "gdrive:BEMI-Runs")
    rclone_bin = drive_config.get("rclone_path", "rclone")
    try:
        result = subprocess.run(
            [
                rclone_bin, "copy", output_dir, remote,
                "--exclude", "step4_checkpoint.ndjson",
                "--exclude", "progress.json",
                "--transfers", "8",
            ],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0:
            print(f"  Drive sync: run output uploaded to {remote}")
        else:
            print(f"  [WARN] Drive sync failed (rclone exit {result.returncode}): "
                  f"{result.stderr.strip()[:200]}")
    except FileNotFoundError:
        print(f"  [WARN] Drive sync skipped: rclone not found at {rclone_bin!r}. "
              "Run scripts/setup_drive_sync.sh to install it.")
    except subprocess.TimeoutExpired:
        print("  [WARN] Drive sync timed out after 180s — output may be partially uploaded.")
    except Exception as e:
        print(f"  [WARN] Drive sync error: {e}")


def _sync_shared_data_to_drive(drive_config: dict) -> None:
    """Sync ICP profiles, projects, and registry to Drive (best-effort, never blocks the run).

    These three directories/files live outside the per-run output directory and are
    needed for multi-device consistency. Synced to gdrive:BEMI-Data (configurable via
    data_remote in drive_sync config) alongside the per-run output in gdrive:BEMI-Runs.

    Pull from another machine with:
        rclone copy gdrive:BEMI-Data/icp_profiles ./icp_profiles
        rclone copy gdrive:BEMI-Data/projects ./projects
        rclone copy gdrive:BEMI-Data/master_practice_registry.json .
    """
    import subprocess

    repo_root = Path(__file__).parent
    rclone_bin = drive_config.get("rclone_path", "rclone")
    runs_remote = drive_config.get("remote", "gdrive:BEMI-Runs")
    data_remote = drive_config.get("data_remote", runs_remote.replace("BEMI-Runs", "BEMI-Data"))

    # Each entry: (local_path, rclone_dest, check_method)
    items = [
        (repo_root / "icp_profiles",                  f"{data_remote}/icp_profiles", "dir"),
        (repo_root / "projects",                       f"{data_remote}/projects",     "dir"),
        (repo_root / "master_practice_registry.json",  data_remote,                   "file"),
    ]

    for src, dest, kind in items:
        if kind == "dir" and not src.is_dir():
            continue
        if kind == "file" and not src.is_file():
            continue
        try:
            result = subprocess.run(
                [rclone_bin, "copy", str(src), dest, "--transfers", "4"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                print(f"  Drive sync: {src.name} → {dest}")
            else:
                print(f"  [WARN] Drive sync: {src.name} failed "
                      f"(exit {result.returncode}): {result.stderr.strip()[:120]}")
        except FileNotFoundError:
            print("  [WARN] Drive sync: rclone not found — shared data not uploaded.")
            return
        except subprocess.TimeoutExpired:
            print(f"  [WARN] Drive sync: {src.name} timed out after 60s.")
        except Exception as e:
            print(f"  [WARN] Drive sync: {src.name} error: {e}")


def _capture_evidence(records: list[dict], output_dir: str) -> int:
    """Write Evidence Vault snapshots for every record that has crawled pages.

    Runs once after extraction (Step 3 / 3b / manual content) so the archived
    pages always reflect the text Step 4 actually scored. Per-record failures
    are logged and skipped — evidence capture must never fail an enrichment run.
    Returns the number of records captured.
    """
    captured = 0
    for record in records:
        pages = record.get("_evidence_pages") or []
        if not pages:
            continue
        try:
            written = write_record_evidence(
                Path(output_dir),
                record.get("id", ""),
                pages,
                provenance=record.get("_evidence_provenance", "crawl"),
            )
            if written:
                captured += 1
        except Exception as e:
            print(f"    [WARN] Evidence capture failed for "
                  f"{record.get('practice_name', 'Unknown')}: {e}")
    return captured


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


def _exclusion_check_fail_closed(record: dict, run_config: dict) -> tuple[dict, dict | None]:
    """Run Step 6's exclusion check on one record, failing closed on any error.

    On success returns (record, None). If apply_exclusions raises, the record
    cannot be certified safe, so it is routed to Manual Review (held out of the
    call queue) rather than tiered by bare score in Step 7 — which would bypass
    every exclusion and cap gate and could ship a should-be-Excluded record.
    exclusion_status stays CLEAR because no exclusion was confirmed; the operator
    decides. Returns (record, error_entry) so the caller can collect the error.
    """
    try:
        record = apply_exclusions(record, run_config)
        return record, None
    except Exception as e:
        error_msg = str(e)[:200]
        print(f"  [FAIL] Exclusion check error for {record.get('id', '?')}: {error_msg}")
        record["exclusion_status"] = "CLEAR"
        record["exclusion_reason"] = None
        record["exclusion_primary_gate"] = ""
        record["target_tier"] = "Manual Review"
        return record, {
            "record_id": record.get("id", "unknown"),
            "step": "exclusion_check",
            "error": error_msg,
            "resolution": "Exclusion check failed; routed to Manual Review (fail-closed)",
        }


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
        source_type: "outscraper", "manual", or "apify_places".
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

    # Load and validate configs — fail before any crawl or LLM spend.
    print("Loading configuration...")
    run_config = _load_json_config(config_path)
    icp_data = _load_json_config(icp_path)
    validate_icp(icp_data, source_label=icp_path)
    config_warnings = validate_run_config(run_config, source_label=config_path)
    for w in config_warnings:
        print(f"  [WARN] {w}")
    icp_signals = icp_data.get("signals", [])

    timeout = run_config.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS)
    retries = run_config.get("request_retries", 3)
    max_pages = run_config.get("max_pages_per_practice", MAX_CRAWL_PAGES)
    bullseye_min = run_config.get("bullseye_min_score", DEFAULT_BULLSEYE_MIN_SCORE)
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
    elif source_type == "apify_places":
        raw_records = load_apify_places_csv(input_file)
    else:
        raise ValueError(
            f"Unknown source type: '{source_type}'. "
            "Use 'outscraper', 'manual', or 'apify_places'."
        )

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
    # Populate registry-derived fields (taxonomy codes, exclusion flags) from
    # the public NPPES database before the structural pre-filter runs.
    # _npi_taxonomy_exclusions is read by check_structural_exclusions so that
    # taxonomy-matched practices skip the crawl rather than being excluded only
    # after LLM spend. Runs before ingest-only exit so the roster carries NPI
    # fields. Skip when npi_enrichment_enabled is explicitly False in run_config.
    # -------------------------------------------------------------------------
    if run_config.get("npi_enrichment_enabled", True):
        _write_progress(output_dir, 1, "NPI enrichment", 0, len(records))
        print(f"\n{'-'*40}")
        print("STEP 1b: NPI ENRICHMENT (NPPES registry lookup)")
        print(f"{'-'*40}")
        npi_lookup.enrich_records(
            records, run_config,
            progress_callback=_step_progress(output_dir, 1, "NPI enrichment"),
        )

    # -------------------------------------------------------------------------
    # STEP 1d: PRACTICE-LOCATION CONSOLIDATION (two passes)
    # Pass 1 merges provider rows that describe the same physical location;
    # Pass 2 links surviving locations into multi-location groups WITHOUT
    # merging them. Runs after NPI enrichment so each provider's taxonomy is
    # carried into providers[], and before suppression, the ingest-only roster
    # exit and the structural pre-filter — so no crawl or LLM budget is ever
    # spent per-provider, and the no-spend roster reports the practice count.
    # -------------------------------------------------------------------------
    consolidation_summary = {"enabled": False}
    if records:
        _write_progress(output_dir, 1, "Practice consolidation", 0, len(records))
        records, consolidation_summary = consolidate_records(records, run_config)
        if consolidation_summary.get("enabled"):
            print(f"\n{'-'*40}")
            print("STEP 1d: PRACTICE-LOCATION CONSOLIDATION")
            print(f"{'-'*40}")
            print(f"  Pass 1: {consolidation_summary['input_count']} rows -> "
                  f"{consolidation_summary['output_count']} practice locations "
                  f"({consolidation_summary['merged_groups']} merged, "
                  f"{consolidation_summary['rows_merged_away']} rows absorbed)")
            if consolidation_summary["review_pairs"]:
                print(f"  Review queue: {consolidation_summary['review_pairs']} near-match "
                      "pair(s) kept separate for analyst review")
            if consolidation_summary["unblocked_count"]:
                print(f"  {consolidation_summary['unblocked_count']} row(s) had no "
                      "parseable street or ZIP and were not eligible for "
                      "consolidation")
            print(f"  Pass 2: {consolidation_summary['multi_location_groups']} "
                  "multi-location group(s) linked (not merged)")

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
        output_records = _finalize_ingest_only(records, run_config)
        # Report, never halt. An operator ran this to SEE their list, so an
        # unmapped website column or a registry source with no website field
        # must produce a loud roster rather than no roster at all. Same
        # diagnostic the pre-filter prints, one step earlier and one step
        # cheaper: nothing has been spent yet, and the mapping defect it names
        # is the reason a list empties itself.
        ingest_rule_counts: dict = {}
        ingest_rule_examples: dict = {}
        for r in output_records:
            if r.get("exclusion_status") != "EXCLUDED":
                continue
            for rule in (r.get("_structural_triggers") or ["structural"]):
                ingest_rule_counts[rule] = ingest_rule_counts.get(rule, 0) + 1
                ingest_rule_examples.setdefault(rule, r.get("practice_name", ""))
        ingest_excluded = sum(
            1 for r in output_records if r.get("exclusion_status") == "EXCLUDED")
        canary_text = build_exclusion_canary_report(
            len(output_records), ingest_excluded, ingest_rule_counts,
            ingest_rule_examples, run_config, stage="ingest",
        )
        if canary_text:
            print(canary_text)
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
            consolidation=consolidation_summary,
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
    rule_counts: dict = {}
    rule_examples: dict = {}
    for record in records:
        triggered, rationale = check_structural_exclusions(record, run_config)
        for rule in triggered:
            rule_counts[rule] = rule_counts.get(rule, 0) + 1
            if rule not in rule_examples and rationale:
                rule_examples[rule] = rationale[0]
        (pre_excluded if triggered else eligible).append(record)
    if pre_excluded:
        print(f"\n  Pre-filter: {len(pre_excluded)} records skip enrichment "
              f"(wrong specialty / outside geography / excluded taxonomy); "
              f"{len(eligible)} eligible")

    # Canary: a pre-filter that empties the roster is a defect until proven
    # otherwise. Halting here costs nothing (no crawl or LLM spend has happened)
    # and stops a mapping mismatch from completing as "no qualified targets".
    canary = build_exclusion_canary_report(
        len(records), len(pre_excluded), rule_counts, rule_examples,
        run_config, "structural pre-filter")
    if canary:
        print("\n" + canary, flush=True)
        raise ExclusionCanaryTripped(
            f"Structural pre-filter excluded {len(pre_excluded)} of {len(records)} "
            f"records; run halted. See the diagnostic above."
        )
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
                                       max_workers=io_concurrency,
                                       progress_callback=_step_progress(output_dir, 2, "URL validation"))

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
                                 use_playwright=use_playwright,
                                 progress_callback=_step_progress(output_dir, 3, "Web extraction"))

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
                          max_workers=io_concurrency, use_playwright=True,
                          progress_callback=_step_progress(output_dir, 3, "Browser retry (blocked sites)"))
            after = sum(1 for r in blocked if r.get("_context_text", ""))
            print(f"\n  Browser retry recovered {after - before} of {len(blocked)} blocked records")
        else:
            print("\n  No blocked/thin records — skipping browser retry")

    # -------------------------------------------------------------------------
    # EVIDENCE VAULT CAPTURE
    # Archive the per-page text the crawler saw (timestamp + sha256 per page)
    # under <output_dir>/evidence/<record_id>/, so every signal claim stays
    # verifiable even after the live site changes. Runs after all extraction
    # paths (Step 3, 3b browser retry, manual content) so snapshots match the
    # text Step 4 scores. Disable with evidence_capture_enabled: false.
    # -------------------------------------------------------------------------
    if run_config.get("evidence_capture_enabled", True):
        captured = _capture_evidence(records, output_dir)
        if captured:
            print(f"\n  Evidence Vault: archived page snapshots for {captured} record(s)")

    # -------------------------------------------------------------------------
    # STEP 4: SIGNAL EXTRACTION (Claude)
    # -------------------------------------------------------------------------
    _write_progress(output_dir, 4, "Signal extraction (Claude)", 0, len(records))
    print(f"\n{'-'*40}")
    print("STEP 4: SIGNAL EXTRACTION (Claude)")
    print(f"{'-'*40}")

    manual_sig = ""
    if manual_content_path:
        manual_parts = []
        for p in manual_content_path:
            try:
                st = os.stat(p)
                manual_parts.append(f"{Path(p).name}|{st.st_size}|{st.st_mtime_ns}")
            except OSError:
                manual_parts.append(f"{Path(p).name}|<unreadable>")
        manual_sig = ";".join(manual_parts)
    # Consolidation settings change which rows become which record, so a
    # checkpoint written under different settings must not be resumed from.
    consolidation_cfg = run_config.get("consolidation") or {}
    consolidation_sig = json.dumps(consolidation_cfg, sort_keys=True, default=str)
    crawl_mode = (
        f"pw={int(bool(use_playwright))}"
        f"|retry={int(bool(auto_browser_retry))}"
        f"|manual={manual_sig}"
        f"|consolidation={consolidation_sig}"
    )
    checkpoint_fingerprint = _checkpoint_fingerprint(
        input_file, config_path, icp_path, crawl_mode
    )
    checkpoint = _load_step4_checkpoint(output_dir, checkpoint_fingerprint)
    if checkpoint:
        print(f"  Resuming from checkpoint: {len(checkpoint)} records already processed.")
    else:
        _init_step4_checkpoint(output_dir, checkpoint_fingerprint)

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
                    contact_strategy=icp_data.get("contact_strategy", ""),
                    product_context=icp_data.get("product_context", ""),
                ), None
            except LLMAccountError:
                # The account is rejected, not this record. Retrying and moving
                # on would fail every remaining record identically; let it reach
                # the run-level handler, which halts with the checkpoint intact.
                raise
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
            try:
                idx, record, error = future.result()
            except LLMAccountError as e:
                # Stop the run rather than mark every remaining record failed.
                # Records already extracted are in the checkpoint, so resuming
                # after the account is fixed continues instead of re-spending.
                for pending in futures:
                    pending.cancel()
                print("\n" + "=" * 72, flush=True)
                print("  RUN HALTED — the Claude account was rejected", flush=True)
                print("=" * 72, flush=True)
                print(f"  {e}", flush=True)
                print(f"\n  {checkpoint_start + done_count} of {len(records)} records "
                      "were extracted before this and are saved in the Step 4",
                      flush=True)
                print("  checkpoint. Fix the account, then re-run the same command:",
                      flush=True)
                print("  extraction resumes from the checkpoint and does not "
                      "re-spend on them.", flush=True)
                print("=" * 72, flush=True)
                raise
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
            # Do not checkpoint a failed record. Checkpointing exists to avoid
            # re-spending on already-completed work across a crash/resume; a
            # transient failure (rate-limit exhaustion, an API error) should be
            # re-attempted on resume, not frozen as failed and skipped forever.
            if record.get("enrichment_status") != "failed":
                _write_step4_checkpoint(output_dir, record, checkpoint_fingerprint)

    # Verification (Step 5) runs as a separate post-run pass via verify_run.py.

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

    for i, record in enumerate(records):
        records[i], exclusion_error = _exclusion_check_fail_closed(record, run_config)
        if exclusion_error:
            all_errors.append(exclusion_error)

    excluded_count = sum(1 for r in records if r.get("exclusion_status") == "EXCLUDED")
    print(f"\n  {excluded_count} records excluded")

    # Same canary at the post-enrichment gate. This one reports rather than halts:
    # the crawl and LLM spend already happened, so discarding the output would
    # destroy paid work. It must not pass silently, so the diagnostic goes to the
    # console and into the run log.
    final_rule_counts: dict = {}
    final_rule_examples: dict = {}
    for record in records:
        if record.get("exclusion_status") != "EXCLUDED":
            continue
        # exclusion_primary_gate is the canonical key for what fired; the triggered
        # rule list is not persisted on the record and adding it would change the
        # output contract for a diagnostic.
        rule = record.get("exclusion_primary_gate") or "unspecified"
        final_rule_counts[rule] = final_rule_counts.get(rule, 0) + 1
        if rule not in final_rule_examples:
            final_rule_examples[rule] = record.get("exclusion_reason") or ""
    exclusion_canary = build_exclusion_canary_state(
        len(records), excluded_count, final_rule_counts, run_config)
    exclusion_canary_report = build_exclusion_canary_report(
        len(records), excluded_count, final_rule_counts, final_rule_examples,
        run_config, "exclusion check")
    if exclusion_canary_report:
        print("\n" + exclusion_canary_report, flush=True)
        all_errors.append(
            f"Exclusion canary: {excluded_count} of {len(records)} records excluded "
            f"— treat this run as suspect until the config is checked."
        )

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

    # Sum LLM token usage before internal fields are stripped — run-level
    # metadata for the cost display, never a per-record output field.
    llm_usage_totals = {
        "llm_input_tokens": sum((r.get("_llm_usage") or {}).get("input_tokens", 0) for r in records),
        "llm_output_tokens": sum((r.get("_llm_usage") or {}).get("output_tokens", 0) for r in records),
        "llm_call_count": sum(1 for r in records if r.get("_llm_usage")),
    }

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
        llm_usage=llm_usage_totals,
        consolidation=consolidation_summary,
        exclusion_canary=exclusion_canary,
    )

    # The run's output is written — the crash-recovery checkpoint has served its
    # purpose. Removing it here stops a later run in this output directory from
    # inheriting these records (see _checkpoint_fingerprint).
    _clear_step4_checkpoint(output_dir)

    elapsed = time.time() - start_time

    # Optional: sync output directory to Google Drive after every run.
    # Enable with drive_sync.enabled: true in run_config.json.
    drive_cfg = run_config.get("drive_sync") or {}
    if drive_cfg.get("enabled"):
        print(f"\n{'-'*40}")
        print("DRIVE SYNC")
        print(f"{'-'*40}")
        _sync_to_drive(output_dir, drive_cfg)
        _sync_shared_data_to_drive(drive_cfg)

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
        choices=["outscraper", "manual", "apify_places"],
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

    try:
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
    except ExclusionCanaryTripped as e:
        # A deliberate halt, not a crash: the full diagnostic is already on stdout.
        # A traceback here would read as a bug in the pipeline rather than a bug
        # in the run configuration, which is what this is telling the operator.
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except LLMAccountError as e:
        # Same shape: the diagnostic and the resume instructions are on stdout,
        # and the problem is the account, not the pipeline.
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
