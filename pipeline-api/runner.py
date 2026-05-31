"""
runner.py
Pipeline subprocess management. Spawns pipeline.py and monitors it
asynchronously, updating status.json when the process exits.
"""

import asyncio
import json
import logging
import os
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import icp_profiles
import projects
import record_adapter
import reviews
import runs
from config import (
    ICP_SNAPSHOT_FILENAME,
    MAX_CONCURRENT_RUNS,
    MAX_CSV_SIZE_BYTES,
    OUTPUT_RUNS_PATH,
    PIPELINE_REPO_PATH,
    PIPELINE_SCRIPT,
    PROJECT_CONFIG_SNAPSHOT_FILENAME,
    PYTHON_EXECUTABLE,
)

if TYPE_CHECKING:
    from fastapi import BackgroundTasks, UploadFile

logger = logging.getLogger(__name__)


def spawn_pipeline(
    run_id: str,
    input_path: Path,
    source_type: str,
    run_dir: Path,
    config_path: Path,
    icp_path: Path,
    extra_flags: list[str] | None = None,
) -> subprocess.Popen:
    """
    Launch pipeline.py as a non-blocking subprocess.

    Args:
        run_id: Run identifier for logging.
        input_path: Absolute path to the saved input.csv.
        source_type: 'outscraper' or 'manual'.
        run_dir: Absolute path to the run output directory.
        config_path: Absolute path to the project_config snapshot.
        icp_path: Absolute path to the ICP profile snapshot.
        extra_flags: Optional list of extra CLI flags (e.g. ["--playwright"]).

    Returns:
        The running Popen object.
    """
    cmd = [
        PYTHON_EXECUTABLE,
        str(PIPELINE_REPO_PATH / PIPELINE_SCRIPT),
        "--input", str(input_path),
        "--source", source_type,
        "--output-dir", str(run_dir),
        "--config", str(config_path),
        "--icp", str(icp_path),
    ]
    if extra_flags:
        cmd.extend(extra_flags)
    logger.info("Spawning pipeline for run %s: %s", run_id, " ".join(cmd))

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(
        cmd,
        cwd=str(PIPELINE_REPO_PATH),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return process


async def monitor_pipeline(
    run_id: str,
    process: subprocess.Popen,
    success_status: str = "complete",
) -> None:
    """
    Wait for the pipeline subprocess to exit and update status.json.

    Runs as a FastAPI BackgroundTask. Uses run_in_executor so process.wait()
    does not block the event loop.

    success_status is the status to set on a clean exit — "complete" for a
    full enrichment run, "ingested" for an --ingest-only roster load.
    """
    loop = asyncio.get_event_loop()
    try:
        # communicate() reads both pipes while waiting, preventing pipe-buffer deadlock.
        _, stderr_bytes = await loop.run_in_executor(None, process.communicate)

        if process.returncode == 0:
            failure_msg = _validate_output_files(run_id)
            if failure_msg:
                runs.update_run_status(
                    run_id,
                    status="failed",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    error_summary=failure_msg,
                )
                logger.error("Run %s: output validation failed: %s", run_id, failure_msg)
            else:
                counts = _read_completion_counts(run_id)
                runs.update_run_status(
                    run_id,
                    status=success_status,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    **counts,
                )
                logger.info("Run %s finished with status '%s'", run_id, success_status)
        else:
            error_text = stderr_bytes.decode("utf-8", errors="replace")[:2000]
            runs.update_run_status(
                run_id,
                status="failed",
                completed_at=datetime.now(timezone.utc).isoformat(),
                error_summary=error_text or f"Pipeline exited with code {process.returncode}",
            )
            logger.error(
                "Run %s failed (exit %d): %.500s",
                run_id, process.returncode, error_text,
            )

    except Exception as e:
        logger.exception("Unexpected error monitoring run %s", run_id)
        try:
            runs.update_run_status(
                run_id,
                status="failed",
                completed_at=datetime.now(timezone.utc).isoformat(),
                error_summary=f"Monitor error: {str(e)[:200]}",
            )
        except Exception:
            logger.exception(
                "Failed to write failed status for run %s after monitor error", run_id
            )


async def _prepare_run(
    file,
    source_type: str,
    project_id: str,
    operator: str,
) -> tuple[str, "Path", "Path", "Path", int]:
    """Validate the upload, snapshot config/ICP, and create the run directory.

    Shared by the full-run and ingest-only flows so the validation, snapshot,
    and run-record creation logic lives in one place. Leaves the run in
    'pending' status; the caller spawns the pipeline and flips it to running.

    Returns (run_id, run_directory, config_snapshot, icp_snapshot, row_count).

    Raises ValueError if the concurrent-run cap is reached, the project/ICP
    cannot be resolved, or CSV validation fails.
    """
    import validator  # imported here to avoid circular imports at module load

    active = runs.count_active_runs()
    if active >= MAX_CONCURRENT_RUNS:
        raise ValueError(
            f"Too many runs in progress ({active}/{MAX_CONCURRENT_RUNS}). "
            f"Wait for a run to finish before starting another."
        )

    # Resolve the project and its ICP profile before touching the CSV, so a bad
    # configuration is rejected with a clear message and no run dir is created.
    project_config = projects.get_project(project_id)
    if project_config is None:
        raise ValueError(f"Project '{project_id}' does not exist. Create it first.")
    projects.validate_project_config(project_config)
    icp_profile = icp_profiles.get_icp_profile(project_config["icp_profile_id"])

    content, row_count = await validator.validate_csv_upload(file, source_type, project_id)

    run_id = runs.generate_run_id()
    run_directory = OUTPUT_RUNS_PATH / run_id
    run_directory.mkdir(parents=True, exist_ok=True)

    (run_directory / "input.csv").write_bytes(content)

    # Snapshot the resolved config and ICP into the run folder. These frozen
    # copies are what pipeline.py reads, so a later project edit never alters a
    # past run's inputs.
    config_snapshot = run_directory / PROJECT_CONFIG_SNAPSHOT_FILENAME
    icp_snapshot = run_directory / ICP_SNAPSHOT_FILENAME
    _write_json(config_snapshot, project_config)
    _write_json(icp_snapshot, icp_profile)

    runs.create_run(
        run_id=run_id,
        project_id=project_id,
        source_type=source_type,
        input_filename=getattr(file, "filename", None) or "upload.csv",
        operator=operator,
        records_input=row_count,
        metadata={
            "client_name": project_config.get("client_name"),
            "product_name": project_config.get("product_name"),
            "target_specialty": project_config.get("target_specialty"),
            "target_geography": project_config.get("target_geography") or [],
            "icp_profile_id": project_config.get("icp_profile_id"),
            "icp_profile_name": icp_profile.get("name"),
            "icp_profile_version": icp_profile.get("version"),
        },
    )
    return run_id, run_directory, config_snapshot, icp_snapshot, row_count


async def orchestrate_run(
    file,
    source_type: str,
    project_id: str,
    operator: str,
    background_tasks,
) -> tuple[str, int]:
    """
    Full run creation flow: validate CSV, save input, create run record,
    spawn pipeline subprocess, and register background monitor.

    Used by the Bearer-auth API route to run ingest + enrichment in one shot.

    Returns:
        (run_id, row_count)

    Raises:
        ValueError if the project/ICP cannot be resolved or CSV validation fails.
    """
    run_id, run_directory, config_snapshot, icp_snapshot, row_count = await _prepare_run(
        file, source_type, project_id, operator
    )

    process = spawn_pipeline(
        run_id,
        run_directory / "input.csv",
        source_type,
        run_directory,
        config_snapshot,
        icp_snapshot,
    )
    runs.update_run_status(run_id, status="running")
    background_tasks.add_task(monitor_pipeline, run_id, process)

    logger.info("Run %s (full) started by '%s' (%d rows)", run_id, operator, row_count)
    return run_id, row_count


async def orchestrate_ingest(
    file,
    source_type: str,
    project_id: str,
    operator: str,
    background_tasks,
) -> tuple[str, int]:
    """Ingest-only flow: load + normalize the roster without crawl or LLM spend.

    Same preparation as a full run, but spawns the pipeline with --ingest-only
    and lands the run in 'ingested' status. Enrichment is triggered separately
    via orchestrate_enrich_all once the operator has reviewed the list.

    Returns (run_id, row_count).
    """
    run_id, run_directory, config_snapshot, icp_snapshot, row_count = await _prepare_run(
        file, source_type, project_id, operator
    )

    process = spawn_pipeline(
        run_id,
        run_directory / "input.csv",
        source_type,
        run_directory,
        config_snapshot,
        icp_snapshot,
        extra_flags=["--ingest-only"],
    )
    runs.update_run_status(run_id, status="running")
    background_tasks.add_task(monitor_pipeline, run_id, process, "ingested")

    logger.info("Run %s (ingest-only) started by '%s' (%d rows)", run_id, operator, row_count)
    return run_id, row_count


async def orchestrate_enrich_all(
    run_id: str,
    operator: str,
    background_tasks,
    auto_browser_retry: bool = False,
) -> None:
    """Enrich an already-ingested run in place: spawn the full pipeline on its roster.

    Reuses the run's existing input.csv and config/ICP snapshots, so enrichment
    runs against the exact roster and config the operator reviewed. Overwrites
    the run's enriched_targets.json with the enriched result and flips the run
    to 'complete'.

    When auto_browser_retry is True, the pipeline re-crawls blocked/thin sites
    once with headless Chromium before signal extraction.

    Raises:
        FileNotFoundError if the run does not exist.
        ValueError if the run is not in 'ingested' status, its inputs are
        missing, or the concurrent-run cap is reached.
    """
    active = runs.count_active_runs()
    if active >= MAX_CONCURRENT_RUNS:
        raise ValueError(
            f"Too many runs in progress ({active}/{MAX_CONCURRENT_RUNS}). "
            "Wait for a run to finish before starting another."
        )

    status = runs.get_run(run_id)
    if status is None:
        raise FileNotFoundError(f"Run '{run_id}' not found")
    if status.status != "ingested":
        raise ValueError(
            f"Run is {status.status!r}; only an 'ingested' run can be enriched."
        )

    run_directory = runs.run_dir(run_id)
    input_csv = run_directory / "input.csv"
    config_snapshot = run_directory / PROJECT_CONFIG_SNAPSHOT_FILENAME
    icp_snapshot = run_directory / ICP_SNAPSHOT_FILENAME
    for path, label in ((input_csv, "input.csv"),
                        (config_snapshot, "config snapshot"),
                        (icp_snapshot, "ICP snapshot")):
        if not path.exists():
            raise ValueError(f"Run is missing its {label}; cannot enrich.")

    process = spawn_pipeline(
        run_id,
        input_csv,
        status.source_type,
        run_directory,
        config_snapshot,
        icp_snapshot,
        extra_flags=["--auto-browser-retry"] if auto_browser_retry else None,
    )
    runs.update_run_status(run_id, status="running", completed_at=None)
    background_tasks.add_task(monitor_pipeline, run_id, process)

    logger.info(
        "Run %s enrichment started by '%s' (auto_browser_retry=%s)",
        run_id, operator, auto_browser_retry,
    )


async def orchestrate_playwright_retry(
    source_run_id: str,
    operator: str,
    background_tasks,
) -> tuple[str, int]:
    """Start a new run for the limited/failed records from a completed run, using Playwright.

    Reads limited/failed records from the source run's enriched_targets.json,
    builds a manual-format input CSV, and spawns a new pipeline run with
    the --playwright flag so headless Chromium handles the crawl.

    Args:
        source_run_id: The completed run whose limited/failed records are retried.
        operator: Name of the user triggering the retry.
        background_tasks: FastAPI BackgroundTasks instance.

    Returns:
        (new_run_id, row_count)

    Raises:
        ValueError if the source run is not complete, has no limited/failed records,
        or the concurrent-run cap is reached.
        FileNotFoundError if the source run does not exist.
    """
    import csv
    import io

    active = runs.count_active_runs()
    if active >= MAX_CONCURRENT_RUNS:
        raise ValueError(
            f"Too many runs in progress ({active}/{MAX_CONCURRENT_RUNS}). "
            "Wait for a run to finish before starting another."
        )

    source_status = runs.get_run(source_run_id)
    if source_status is None:
        raise FileNotFoundError(f"Run '{source_run_id}' not found")
    if source_status.status != "complete":
        raise ValueError(f"Source run is {source_status.status!r} — only complete runs can be retried.")

    source_dir = runs.run_dir(source_run_id)
    results_path = source_dir / "enriched_targets.json"
    if not results_path.exists():
        raise ValueError("Source run has no enriched_targets.json")

    with open(results_path, "r", encoding="utf-8") as f:
        all_records = record_adapter.normalize_records_payload(json.load(f))

    limited = [r for r in all_records if r.get("source_confidence") in ("limited", "failed")]
    if not limited:
        raise ValueError("No limited/failed records in this run to retry.")

    # Build manual-format CSV in memory
    fieldnames = ["practice_name", "website_url", "phone",
                  "address_city", "address_state", "address_zip", "specialty"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for rec in limited:
        writer.writerow({
            "practice_name": rec.get("practice_name", ""),
            "website_url": record_adapter.normalize_homepage_url(rec.get("website_url", "")),
            "phone": rec.get("phone", ""),
            "address_city": rec.get("address_city", ""),
            "address_state": rec.get("address_state", ""),
            "address_zip": rec.get("address_zip", ""),
            "specialty": rec.get("specialty", ""),
        })
    csv_bytes = buf.getvalue().encode("utf-8")
    row_count = len(limited)

    # Load project config from source run's snapshot
    config_snapshot_src = source_dir / PROJECT_CONFIG_SNAPSHOT_FILENAME
    icp_snapshot_src = source_dir / ICP_SNAPSHOT_FILENAME
    if not config_snapshot_src.exists() or not icp_snapshot_src.exists():
        raise ValueError("Source run is missing config snapshots.")
    with open(config_snapshot_src, "r", encoding="utf-8") as f:
        project_config = json.load(f)
    with open(icp_snapshot_src, "r", encoding="utf-8") as f:
        icp_profile = json.load(f)

    project_id = source_status.project_id

    new_run_id = runs.generate_run_id()
    run_directory = OUTPUT_RUNS_PATH / new_run_id
    run_directory.mkdir(parents=True, exist_ok=True)

    (run_directory / "input.csv").write_bytes(csv_bytes)

    config_snapshot = run_directory / PROJECT_CONFIG_SNAPSHOT_FILENAME
    icp_snapshot = run_directory / ICP_SNAPSHOT_FILENAME
    _write_json(config_snapshot, project_config)
    _write_json(icp_snapshot, icp_profile)

    runs.create_run(
        run_id=new_run_id,
        project_id=project_id,
        source_type="manual",
        input_filename=f"{source_run_id}_playwright_retry.csv",
        operator=operator,
        records_input=row_count,
        metadata={
            "client_name": project_config.get("client_name"),
            "product_name": project_config.get("product_name"),
            "target_specialty": project_config.get("target_specialty"),
            "target_geography": project_config.get("target_geography") or [],
            "icp_profile_id": project_config.get("icp_profile_id"),
            "icp_profile_name": icp_profile.get("name"),
            "icp_profile_version": icp_profile.get("version"),
            "playwright_retry_of": source_run_id,
        },
    )

    process = spawn_pipeline(
        new_run_id,
        run_directory / "input.csv",
        "manual",
        run_directory,
        config_snapshot,
        icp_snapshot,
        extra_flags=["--playwright"],
    )
    runs.update_run_status(new_run_id, status="running")
    background_tasks.add_task(monitor_pipeline, new_run_id, process)

    logger.info(
        "Playwright retry run %s started by '%s' (%d rows, source=%s)",
        new_run_id, operator, row_count, source_run_id,
    )
    return new_run_id, row_count


def _prepare_single_record_job(source_run_id: str, record_id: str):
    """Validate a source run + record and build a scratch job dir for re-enrich.

    Shared setup for the in-place re-crawl / manual-content paths: confirms the
    source run is complete and contains the record, writes a one-row manual CSV
    into a hidden scratch dir nested in the source run dir (no status.json, so it
    is invisible to list_runs / orphan recovery / the run cap), and returns the
    handles the caller needs to spawn the pipeline.

    Returns (source_dir, record, scratch_dir, config_snapshot_src, icp_snapshot_src).

    Raises:
        FileNotFoundError if the run or record is not found.
        ValueError if the run is not complete or its inputs are missing.
    """
    source_status = runs.get_run(source_run_id)
    if source_status is None:
        raise FileNotFoundError(f"Run '{source_run_id}' not found")
    if source_status.status != "complete":
        raise ValueError(f"Source run is {source_status.status!r} — only complete runs can be re-enriched.")

    source_dir = runs.run_dir(source_run_id)
    results_path = source_dir / "enriched_targets.json"
    if not results_path.exists():
        raise ValueError("Source run has no enriched_targets.json")

    with open(results_path, "r", encoding="utf-8") as f:
        all_records = record_adapter.normalize_records_payload(json.load(f))
    record = next((r for r in all_records if record_adapter.get_record_id(r) == record_id), None)
    if record is None:
        raise FileNotFoundError(f"Record '{record_id}' not found in run '{source_run_id}'")

    config_snapshot_src = source_dir / PROJECT_CONFIG_SNAPSHOT_FILENAME
    icp_snapshot_src = source_dir / ICP_SNAPSHOT_FILENAME
    if not config_snapshot_src.exists() or not icp_snapshot_src.exists():
        raise ValueError("Source run is missing config snapshots.")

    # Hidden, run-scoped scratch dir. The leading dot keeps it out of
    # is_valid_run_id, and the absence of a status.json keeps it out of
    # list_runs(), so orphan recovery and the concurrent-run cap never see it.
    scratch_dir = source_dir / f".recrawl_{secrets.token_hex(4)}"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    return source_dir, record, scratch_dir, config_snapshot_src, icp_snapshot_src


def _write_single_record_csv(scratch_dir: Path, record: dict, website_url: str) -> Path:
    """Write a one-row manual-format input.csv into the scratch dir."""
    import csv
    import io

    fieldnames = ["practice_name", "website_url", "phone",
                  "address_city", "address_state", "address_zip", "specialty"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerow({
        "practice_name": record.get("practice_name", ""),
        "website_url": website_url,
        "phone": record.get("phone", ""),
        "address_city": record.get("address_city", ""),
        "address_state": record.get("address_state", ""),
        "address_zip": record.get("address_zip", ""),
        "specialty": record.get("specialty", ""),
    })
    input_path = scratch_dir / "input.csv"
    input_path.write_bytes(buf.getvalue().encode("utf-8"))
    return input_path


async def orchestrate_single_recrawl(
    source_run_id: str,
    record_id: str,
    website_url_override: str,
    operator: str,
) -> str:
    """Re-crawl one record with a headless browser and update its run in place.

    Runs the single record through the pipeline (--playwright) in a hidden
    scratch dir, then merges the updated record back into the source run's
    enriched_targets.json. The source run stays 'complete' the whole time — no
    new run appears in the dashboard. Blocks until the merge is done so the
    operator returns to fresh data.

    Returns the source_run_id (the same run, now updated).

    Raises:
        FileNotFoundError if run/record not found.
        ValueError if the run is not complete or its inputs are missing.
    """
    source_dir, record, scratch_dir, config_src, icp_src = _prepare_single_record_job(
        source_run_id, record_id
    )

    url = (record_adapter.normalize_homepage_url(website_url_override)
           or record_adapter.normalize_homepage_url(record.get("website_url", "")))
    input_path = _write_single_record_csv(scratch_dir, record, url)

    process = spawn_pipeline(
        source_run_id,
        input_path,
        "manual",
        scratch_dir,
        config_src,
        icp_src,
        extra_flags=["--playwright"],
    )
    logger.info(
        "In-place browser re-crawl of record %s in run %s started by '%s' (url=%s)",
        record_id, source_run_id, operator, url,
    )
    await _run_inplace_update(source_run_id, scratch_dir, record_id, process, "browser re-crawl")
    return source_run_id


async def orchestrate_manual_content_recrawl(
    source_run_id: str,
    record_id: str,
    contents: list[tuple[bytes, str]],
    operator: str,
) -> str:
    """Enrich one record from operator-provided page content, updating in place.

    For a site behind a hard CAPTCHA wall the crawler cannot clear, the operator
    captures the page(s) in their own browser (Save Page As .html, or copy the
    visible text) and submits them. Each page is run through the pipeline (one
    --manual-content-path flag per page) in a hidden scratch dir and the updated
    record is merged back into the source run. The source run stays 'complete'.

    contents is a list of (content_bytes, content_filename) pairs, one per page.

    Returns the source_run_id.

    Raises:
        FileNotFoundError if run/record not found.
        ValueError if the run is not complete, its inputs are missing, or no
        usable content was provided / a page is too large.
    """
    usable = [(b, name) for (b, name) in contents if b and b.strip()]
    if not usable:
        raise ValueError("No content provided. Upload an HTML file or paste page content.")
    for content_bytes, _ in usable:
        if len(content_bytes) > MAX_CSV_SIZE_BYTES:
            raise ValueError("Provided content is too large.")

    source_dir, record, scratch_dir, config_src, icp_src = _prepare_single_record_job(
        source_run_id, record_id
    )

    url = record_adapter.normalize_homepage_url(record.get("website_url", ""))
    input_path = _write_single_record_csv(scratch_dir, record, url)

    # Save each page into the scratch dir as .html or .txt and pass one
    # --manual-content-path flag per page so the pipeline concatenates them.
    extra_flags: list[str] = []
    total_bytes = 0
    for idx, (content_bytes, content_filename) in enumerate(usable, start=1):
        looks_html = (
            (content_filename or "").lower().endswith((".html", ".htm"))
            or b"<html" in content_bytes[:4000].lower()
            or b"<body" in content_bytes[:4000].lower()
            or b"<div" in content_bytes[:4000].lower()
        )
        suffix = ".html" if looks_html else ".txt"
        content_path = scratch_dir / f"manual_content_{idx}{suffix}"
        content_path.write_bytes(content_bytes)
        extra_flags += ["--manual-content-path", str(content_path)]
        total_bytes += len(content_bytes)

    process = spawn_pipeline(
        source_run_id,
        input_path,
        "manual",
        scratch_dir,
        config_src,
        icp_src,
        extra_flags=extra_flags,
    )
    logger.info(
        "In-place manual-content enrich of record %s in run %s started by '%s' (%d page(s), %d bytes)",
        record_id, source_run_id, operator, len(usable), total_bytes,
    )
    await _run_inplace_update(source_run_id, scratch_dir, record_id, process, "manual content")
    return source_run_id


async def _run_inplace_update(
    source_run_id: str,
    scratch_dir: Path,
    record_id: str,
    process: subprocess.Popen,
    kind: str,
) -> None:
    """Wait for a single-record pipeline run, then merge its result in place.

    On a clean exit, merges the updated record into the source run; on any
    failure, leaves the source run untouched. The scratch dir is always removed.
    """
    loop = asyncio.get_event_loop()
    try:
        _, stderr_bytes = await loop.run_in_executor(None, process.communicate)
        if process.returncode != 0:
            error_text = stderr_bytes.decode("utf-8", errors="replace")[:2000]
            logger.error(
                "In-place %s of record %s in run %s failed (exit %d): %.500s",
                kind, record_id, source_run_id, process.returncode, error_text,
            )
            return
        _merge_recrawled_record(source_run_id, scratch_dir, record_id, kind)
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def _recompute_counts_from_records(records: list[dict]) -> dict:
    """Derive status.json tier/exclusion counts from the records list itself.

    Keeps every count consistent with the authoritative enriched_targets.json
    after an in-place record swap, rather than the per-run run_log.json (which a
    single-record scratch run would not have updated for the whole run).
    """
    return {
        "bullseye_count": sum(1 for r in records if r.get("target_tier") == "Bullseye"),
        "needs_verification_count": sum(
            1 for r in records if r.get("target_tier") == "Needs Verification"),
        "watchlist_count": sum(1 for r in records if r.get("target_tier") == "Watchlist"),
        "excluded_count": sum(1 for r in records if r.get("exclusion_status") == "EXCLUDED"),
        "error_count": sum(1 for r in records if r.get("enrichment_status") == "failed"),
    }


def _merge_recrawled_record(
    source_run_id: str,
    scratch_dir: Path,
    record_id: str,
    kind: str,
) -> None:
    """Merge a re-enriched single record back into its source run, in place.

    Validates the scratch output, replaces the matching record (by stable id) in
    the source enriched_targets.json via an atomic write, stamps the analyst
    review note, and recomputes the source run's status counts. The source run
    stays 'complete'. On any error the source run is left untouched.
    """
    failure = _validate_output_dir(scratch_dir)
    if failure:
        logger.error(
            "In-place %s of record %s in run %s: scratch output invalid: %s",
            kind, record_id, source_run_id, failure,
        )
        return

    try:
        with open(scratch_dir / "enriched_targets.json", "r", encoding="utf-8") as f:
            new_records = record_adapter.normalize_records_payload(json.load(f))
        updated = next(
            (r for r in new_records if record_adapter.get_record_id(r) == record_id),
            new_records[0] if new_records else None,
        )
        if updated is None:
            logger.error(
                "In-place %s of record %s in run %s: scratch produced no record.",
                kind, record_id, source_run_id,
            )
            return

        source_dir = runs.run_dir(source_run_id)
        enriched_path = source_dir / "enriched_targets.json"
        with open(enriched_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        records = record_adapter.normalize_records_payload(payload)

        idx = next(
            (i for i, r in enumerate(records) if record_adapter.get_record_id(r) == record_id),
            None,
        )
        if idx is None:
            logger.error(
                "In-place %s: record %s vanished from run %s; leaving run untouched.",
                kind, record_id, source_run_id,
            )
            return

        records[idx] = updated

        # Preserve the wrapper shape; refresh the generation timestamp only.
        if isinstance(payload, dict):
            payload["records"] = records
            payload["generated_at"] = datetime.now(timezone.utc).isoformat()
            payload["record_count"] = len(records)
            out_payload = payload
        else:
            out_payload = records
        reviews._atomic_write(enriched_path, out_payload)

        # Keep the analyst's decision; flag that the data changed under it.
        reviews.stamp_reenriched(source_run_id, record_id, source_dir, kind)

        # Recompute counts from the merged file. records_output is unchanged
        # (one record replaced one record), so it is preserved as-is.
        counts = _recompute_counts_from_records(records)
        runs.update_run_status(source_run_id, status="complete", **counts)

        logger.info(
            "In-place %s merged: record %s updated in run %s (tier now %s, score %s).",
            kind, record_id, source_run_id,
            updated.get("target_tier"), updated.get("bullseye_score"),
        )
    except Exception:
        logger.exception(
            "In-place %s of record %s in run %s failed during merge; run untouched.",
            kind, record_id, source_run_id,
        )


async def resume_run(run_id: str, background_tasks) -> int:
    """Re-spawn the pipeline for a failed run, resuming from its Step 4 checkpoint.

    Returns the number of records already in the checkpoint.
    Raises FileNotFoundError if the run doesn't exist.
    Raises ValueError if the run is not failed or has no checkpoint.
    """
    status = runs.get_run(run_id)
    if status is None:
        raise FileNotFoundError(f"Run '{run_id}' not found")
    if status.status != "failed":
        raise ValueError(
            f"Run '{run_id}' cannot be resumed — status is {status.status!r}, expected 'failed'"
        )
    if not runs.has_step4_checkpoint(run_id):
        raise ValueError(
            f"Run '{run_id}' has no Step 4 checkpoint. "
            "The run may have failed before signal extraction began — restart instead."
        )

    rd = runs.run_dir(run_id)
    process = spawn_pipeline(
        run_id=run_id,
        input_path=rd / "input.csv",
        source_type=status.source_type,
        run_dir=rd,
        config_path=rd / PROJECT_CONFIG_SNAPSHOT_FILENAME,
        icp_path=rd / ICP_SNAPSHOT_FILENAME,
    )
    runs.update_run_status(run_id, status="running", error_summary=None, completed_at=None)
    background_tasks.add_task(monitor_pipeline, run_id, process)
    checkpoint_count = runs.step4_checkpoint_count(run_id)
    logger.info("Resumed run %s from Step 4 checkpoint (%d records)", run_id, checkpoint_count)
    return checkpoint_count


def _write_json(path: Path, data: dict) -> None:
    """Write a snapshot JSON file into a freshly created run directory."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _validate_output_files(run_id: str) -> str | None:
    """
    Verify that both required output files exist, parse, and have expected shape.

    Returns None on success, or a human-readable error string on failure.
    A non-None return means the run should be marked failed even if exit code was 0.
    """
    return _validate_output_dir(runs.run_dir(run_id))


def _validate_output_dir(directory: Path) -> str | None:
    """
    Verify that both required output files exist, parse, and have expected shape
    in an arbitrary output directory (a run dir or a scratch dir).

    Returns None on success, or a human-readable error string on failure.
    """
    enriched_path = directory / "enriched_targets.json"
    if not enriched_path.exists():
        return "Pipeline exited 0 but enriched_targets.json was not written."
    try:
        with open(enriched_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        records = record_adapter.normalize_records_payload(payload)
        if not isinstance(records, list):
            return "enriched_targets.json parsed but 'records' is not a list."
    except json.JSONDecodeError as e:
        return f"enriched_targets.json is malformed JSON: {e}"
    except Exception as e:
        return f"enriched_targets.json could not be read: {e}"

    log_path = directory / "run_log.json"
    if not log_path.exists():
        return "Pipeline exited 0 but run_log.json was not written."
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            log = json.load(f)
        if not isinstance(log, dict):
            return "run_log.json parsed but is not a JSON object."
    except json.JSONDecodeError as e:
        return f"run_log.json is malformed JSON: {e}"

    return None


def _read_completion_counts(run_id: str) -> dict:
    """
    Read post-run statistics from run_log.json and enriched_targets.json.

    Returns a dict of status.json count fields.
    Falls back to zeros for any field that cannot be read.
    """
    directory = runs.run_dir(run_id)
    counts: dict = {}

    log_path = directory / "run_log.json"
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                log = json.load(f)
            counts["records_output"] = log.get("records_output", 0)
            counts["excluded_count"] = log.get("records_excluded", 0)
            counts["error_count"] = log.get("records_failed", 0)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Could not parse run_log.json for run %s: %s", run_id, e)
    else:
        logger.warning("run_log.json missing for run %s", run_id)

    results_path = directory / "enriched_targets.json"
    if results_path.exists():
        try:
            with open(results_path, "r", encoding="utf-8") as f:
                records = record_adapter.normalize_records_payload(json.load(f))
            counts["bullseye_count"] = sum(
                1 for r in records if r.get("target_tier") == "Bullseye"
            )
            counts["needs_verification_count"] = sum(
                1 for r in records if r.get("target_tier") == "Needs Verification"
            )
            counts["watchlist_count"] = sum(
                1 for r in records if r.get("target_tier") == "Watchlist"
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(
                "Could not parse enriched_targets.json for run %s: %s", run_id, e
            )
    else:
        logger.warning("enriched_targets.json missing for run %s", run_id)

    return counts
