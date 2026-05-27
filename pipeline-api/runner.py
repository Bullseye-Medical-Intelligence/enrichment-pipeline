"""
runner.py
Pipeline subprocess management. Spawns pipeline.py and monitors it
asynchronously, updating status.json when the process exits.
"""

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import runs
from config import PIPELINE_REPO_PATH, PIPELINE_SCRIPT, PYTHON_EXECUTABLE

logger = logging.getLogger(__name__)


def spawn_pipeline(
    run_id: str,
    input_path: Path,
    source_type: str,
    run_dir: Path,
) -> subprocess.Popen:
    """
    Launch pipeline.py as a non-blocking subprocess.

    Args:
        run_id: Run identifier for logging.
        input_path: Absolute path to the saved input.csv.
        source_type: 'outscraper' or 'manual'.
        run_dir: Absolute path to the run output directory.

    Returns:
        The running Popen object.
    """
    cmd = [
        PYTHON_EXECUTABLE,
        str(PIPELINE_REPO_PATH / PIPELINE_SCRIPT),
        "--input", str(input_path),
        "--source", source_type,
        "--output-dir", str(run_dir),
    ]
    logger.info("Spawning pipeline for run %s: %s", run_id, " ".join(cmd))

    process = subprocess.Popen(
        cmd,
        cwd=str(PIPELINE_REPO_PATH),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
        await loop.run_in_executor(None, process.wait)

        _, stderr_bytes = process.stdout.read(), process.stderr.read()

        if process.returncode == 0:
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
                records = json.load(f)
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
