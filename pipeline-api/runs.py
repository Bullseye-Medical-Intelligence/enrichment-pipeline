"""
runs.py
Run state management: create, read, update, and list runs via status.json.
All run state lives on the filesystem. No in-memory state.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import MAX_RUNS_RETURNED, OUTPUT_RUNS_PATH, STATUS_FILENAME
from schema import RunStatus, RunSummary

logger = logging.getLogger(__name__)


def run_dir(run_id: str) -> Path:
    """Return the filesystem directory for a given run."""
    return OUTPUT_RUNS_PATH / run_id


def create_run(
    run_id: str,
    project_id: str,
    source_type: str,
    input_filename: str,
    operator: str,
    records_input: int,
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
    )
    _write_status(run_id, status)
    return status


def get_run(run_id: str) -> Optional[RunStatus]:
    """
    Read and return status.json for the given run.

    Returns:
        RunStatus if found, None if the run directory does not exist.
    """
    status_path = run_dir(run_id) / STATUS_FILENAME
    if not status_path.exists():
        return None
    with open(status_path, "r", encoding="utf-8") as f:
        return RunStatus(**json.load(f))


def update_run_status(run_id: str, **fields) -> RunStatus:
    """
    Merge field updates into the existing status.json and write it back.

    Args:
        run_id: The run to update.
        **fields: Any RunStatus fields to overwrite.

    Returns:
        The updated RunStatus.

    Raises:
        FileNotFoundError if the run directory does not exist.
    """
    current = get_run(run_id)
    if current is None:
        raise FileNotFoundError(f"Run '{run_id}' not found in {OUTPUT_RUNS_PATH}")
    updated = current.model_copy(update=fields)
    _write_status(run_id, updated)
    return updated


def list_runs(max_runs: int = MAX_RUNS_RETURNED) -> list[RunSummary]:
    """
    Return up to max_runs RunSummary objects sorted newest-first.

    Skips any run directory whose status.json is missing or malformed.
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
            summaries.append(
                RunSummary(
                    run_id=data["run_id"],
                    status=data["status"],
                    source_type=data["source_type"],
                    records_input=data.get("records_input", 0),
                    bullseye_count=data.get("bullseye_count", 0),
                    watchlist_count=data.get("watchlist_count", 0),
                    excluded_count=data.get("excluded_count", 0),
                    error_count=data.get("error_count", 0),
                    created_at=data["created_at"],
                    completed_at=data.get("completed_at"),
                )
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Skipping malformed status.json in '%s': %s", entry.name, e)

    summaries.sort(key=lambda s: s.created_at, reverse=True)
    return summaries[:max_runs]


def _write_status(run_id: str, status: RunStatus) -> None:
    """Write status.json to disk, overwriting any existing file."""
    status_path = run_dir(run_id) / STATUS_FILENAME
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(status.model_dump(), f, indent=2)
