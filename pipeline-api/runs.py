"""
runs.py
Run state management: create, read, update, and list runs via status.json.
All run state lives on the filesystem. No in-memory state.
"""

import json
import logging
import os
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import locking
from config import MAX_RUNS_RETURNED, OUTPUT_RUNS_PATH, STATUS_FILENAME
from schema import RunStatus, RunSummary

logger = logging.getLogger(__name__)

# Accepts current IDs (RUN-YYYYMMDD-HHMMSS-xxxx) and legacy suffix-less ones.
_RUN_ID_PATTERN = re.compile(r"^RUN-\d{8}-\d{6}(?:-[a-f0-9]{4})?$")


def is_valid_run_id(run_id: str) -> bool:
    """Return True if run_id matches the canonical RUN-ID format."""
    return bool(_RUN_ID_PATTERN.match(run_id))


def run_dir(run_id: str) -> Path:
    """Return the filesystem directory for a given run.

    Rejects any run_id that is not a canonical RUN-ID, blocking path
    traversal (e.g. '../../etc') before it reaches the filesystem.
    """
    if not is_valid_run_id(run_id):
        raise ValueError(f"Invalid run_id: {run_id!r}")
    return OUTPUT_RUNS_PATH / run_id


def create_run(
    run_id: str,
    project_id: str,
    source_type: str,
    input_filename: str,
    operator: str,
    records_input: int,
    metadata: Optional[dict] = None,
) -> RunStatus:
    """
    Create the run directory and write an initial status.json with status 'pending'.

    Args:
        run_id: Unique run identifier (RUN-YYYYMMDD-HHMMSS).
        project_id: Client project identifier.
        source_type: 'outscraper' or 'manual'.
        input_filename: Original filename of the uploaded CSV.
        operator: Name/email of the operator who triggered the run.
        records_input: Number of CSV rows (excluding header).
        metadata: Optional project/ICP context fields (client_name,
            product_name, target_specialty, target_geography, icp_profile_id,
            icp_profile_name, icp_profile_version).

    Returns:
        The initial RunStatus object.
    """
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)

    status = RunStatus(
        run_id=run_id,
        project_id=project_id,
        source_type=source_type,
        input_filename=input_filename,
        status="pending",
        created_at=datetime.now(timezone.utc).isoformat(),
        operator=operator,
        output_path=str(directory / "enriched_targets.json"),
        records_input=records_input,
        **(metadata or {}),
    )
    _write_status(run_id, status)
    return status


def get_run(run_id: str) -> Optional[RunStatus]:
    """
    Read and return status.json for the given run.

    Returns:
        RunStatus if found, None if the run directory does not exist.
    """
    if not is_valid_run_id(run_id):
        return None
    status_path = run_dir(run_id) / STATUS_FILENAME
    if not status_path.exists():
        return None
    with open(status_path, "r", encoding="utf-8") as f:
        return RunStatus(**json.load(f))


def update_run_status(run_id: str, **fields) -> RunStatus:
    """
    Merge field updates into the existing status.json and write it back.

    The read-modify-write runs under the per-run lock so a threadpool writer
    (e.g. the registry-update stamp) and an on-loop writer (monitors, operator
    actions) can never interleave and silently drop each other's fields —
    every update rewrites the whole file.

    Args:
        run_id: The run to update.
        **fields: Any RunStatus fields to overwrite.

    Returns:
        The updated RunStatus.

    Raises:
        FileNotFoundError if the run directory does not exist.
        locking.LockTimeout if another writer holds the run's lock too long.
    """
    with locking.run_lock(run_dir(run_id)):
        current = get_run(run_id)
        if current is None:
            raise FileNotFoundError(f"Run '{run_id}' not found in {OUTPUT_RUNS_PATH}")
        updated = current.model_copy(update=fields)
        _write_status(run_id, updated)
    return updated


def list_runs(
    max_runs: int | None = MAX_RUNS_RETURNED,
    include_archived: bool = False,
) -> list[RunSummary]:
    """Return up to max_runs RunSummary objects sorted newest-first.

    max_runs=None returns every run — required for internal accounting
    (count_active_runs, reconcile_orphaned_runs) which must never be
    display-paginated, or stuck runs older than the UI page silently
    escape the concurrency cap and orphan recovery.
    Skips any run directory whose status.json is missing or malformed.
    Archived runs are excluded by default; pass include_archived=True to include them.
    """
    if not OUTPUT_RUNS_PATH.exists():
        return []

    summaries: list[RunSummary] = []
    for entry in OUTPUT_RUNS_PATH.iterdir():
        if not entry.is_dir():
            continue
        status_path = entry / STATUS_FILENAME
        if not status_path.exists():
            continue
        try:
            with open(status_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Discovery runs share the runs/ directory but are not enrichment
            # runs (no enriched_targets.json, different status.json shape). Keep
            # them out of the enrichment run listing so the dashboard is unaffected.
            if data.get("run_type") == "discovery":
                continue
            archived = bool(data.get("archived", False))
            if archived and not include_archived:
                continue
            summaries.append(
                RunSummary(
                    run_id=data["run_id"],
                    status=data["status"],
                    source_type=data["source_type"],
                    records_input=data.get("records_input", 0),
                    bullseye_count=data.get("bullseye_count", 0),
                    # contender_count is the renamed watchlist_count; fall back to
                    # the old key so historical runs still report a value.
                    contender_count=data.get("contender_count", data.get("watchlist_count", 0)),
                    manual_review_count=data.get("manual_review_count", 0),
                    excluded_count=data.get("excluded_count", 0),
                    error_count=data.get("error_count", 0),
                    created_at=data["created_at"],
                    completed_at=data.get("completed_at"),
                    project_id=data.get("project_id"),
                    client_name=data.get("client_name"),
                    icp_profile_id=data.get("icp_profile_id"),
                    error_summary=data.get("error_summary", ""),
                    archived=archived,
                )
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Skipping malformed status.json in '%s': %s", entry.name, e)

    summaries.sort(key=lambda s: s.created_at, reverse=True)
    return summaries[:max_runs]


def count_active_runs() -> int:
    """Return the number of runs currently in 'pending' or 'running' state."""
    return sum(
        1 for s in list_runs(max_runs=None, include_archived=True)
        if s.status in ("pending", "running")
    )


def archive_run(run_id: str) -> None:
    """Mark a run as archived so it is hidden from the default run list.

    Archived runs are preserved on disk. They can be unarchived or deleted.
    Raises ValueError if the run does not exist or is actively running.
    """
    status = get_run(run_id)
    if status is None:
        raise ValueError(f"Run '{run_id}' does not exist.")
    if status.status in ("pending", "running"):
        raise ValueError(f"Cannot archive an active run (status: {status.status}).")
    update_run_status(run_id, archived=True)
    logger.info("Archived run: %s", run_id)


def unarchive_run(run_id: str) -> None:
    """Restore an archived run to the default run list."""
    status = get_run(run_id)
    if status is None:
        raise ValueError(f"Run '{run_id}' does not exist.")
    update_run_status(run_id, archived=False)
    logger.info("Unarchived run: %s", run_id)


def delete_run(run_id: str) -> None:
    """Permanently remove a run directory and all its files.

    Raises ValueError if the run is currently active (pending/running) or does
    not exist. The run_id format is validated by run_dir() before any filesystem
    access.
    """
    directory = run_dir(run_id)
    if not directory.exists():
        raise ValueError(f"Run '{run_id}' does not exist.")
    status = get_run(run_id)
    if status and status.status in ("pending", "running"):
        raise ValueError(f"Cannot delete an active run (status: {status.status}).")
    shutil.rmtree(directory)
    logger.info("Deleted run directory: %s", directory)


def reconcile_orphaned_runs() -> int:
    """Mark any run still 'running'/'pending' as failed.

    Called on server startup: monitors do not survive a restart, so any run
    still in an active state has been orphaned and will never complete.
    Returns the number of runs reconciled.
    """
    reconciled = 0
    for summary in list_runs(max_runs=None, include_archived=True):
        if summary.status in ("pending", "running"):
            try:
                update_run_status(
                    summary.run_id,
                    status="failed",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    error_summary="Interrupted by server restart (run did not survive).",
                )
                reconciled += 1
            except FileNotFoundError:
                logger.warning("Could not reconcile orphaned run %s", summary.run_id)
    return reconciled


def generate_run_id() -> str:
    """Generate a collision-resistant run ID. Appends a 4-char hex suffix."""
    return f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def has_step4_checkpoint(run_id: str) -> bool:
    """Return True if the run has a partial Step 4 checkpoint that can be resumed."""
    return (run_dir(run_id) / "step4_checkpoint.ndjson").exists()


def step4_checkpoint_count(run_id: str) -> int:
    """Return the number of records already written to the Step 4 checkpoint."""
    path = run_dir(run_id) / "step4_checkpoint.ndjson"
    if not path.exists():
        return 0
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def read_progress(run_id: str) -> Optional[dict]:
    """Read progress.json from a run directory, or None if absent/unreadable."""
    try:
        path = run_dir(run_id) / "progress.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def _write_status(run_id: str, status: RunStatus) -> None:
    """Write status.json atomically (temp file + os.replace).

    A process crash mid-write must never leave a truncated status.json — a
    half-written file breaks the run listing for every page that reads it.
    """
    status_path = run_dir(run_id) / STATUS_FILENAME
    fd, tmp_path = tempfile.mkstemp(dir=status_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(status.model_dump(), f, indent=2)
        os.replace(tmp_path, status_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
