"""
runner.py
Pipeline subprocess management. Spawns pipeline.py and monitors it
asynchronously, updating status.json when the process exits.
"""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import icp_profiles
import projects
import record_adapter
import runs
from config import (
    ICP_SNAPSHOT_FILENAME,
    MAX_CONCURRENT_RUNS,
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


async def monitor_pipeline(run_id: str, process: subprocess.Popen) -> None:
    """
    Wait for the pipeline subprocess to exit and update status.json.

    Runs as a FastAPI BackgroundTask. Uses run_in_executor so process.wait()
    does not block the event loop.
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
                    status="complete",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    **counts,
                )
                logger.info("Run %s completed successfully", run_id)
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

    Used by both the Bearer-auth API route and the session-auth UI route
    to avoid duplicating this orchestration logic.

    Args:
        file: UploadFile from the HTTP request.
        source_type: 'outscraper' or 'manual'.
        project_id: Client project identifier (must reference an existing project).
        operator: Name of the user triggering the run.
        background_tasks: FastAPI BackgroundTasks instance.

    Returns:
        (run_id, row_count)

    Raises:
        ValueError if the project/ICP cannot be resolved or CSV validation fails.
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

    logger.info(
        "Run %s started by '%s' (%d rows, project=%s, icp=%s)",
        run_id, operator, row_count, project_id, project_config["icp_profile_id"],
    )
    return run_id, row_count


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
    directory = runs.run_dir(run_id)

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
