"""
discovery_runs.py
API support for Discovery Runs.

A discovery run uploads an Outscraper CSV, compares it against the master
practice registry, and writes the discovery output files into a standard
/output/runs/{run_id}/ directory. It is additive and isolated from enrichment:
no scoring, no LLM, no crawl, and the registry source is never mutated (only an
updated_registry_preview.json is written).

The discovery engine lives in the repo-root `discovery` package. This module
never imports it; it invokes discovery_cli.py as a subprocess (the simulate_icp
pattern), keeping the API/engine boundary identical to the rest of the system.

Routes:
    POST /discovery-runs               — create + run a discovery comparison
    GET  /discovery-runs/{run_id}      — status + summary
    GET  /discovery-runs/{run_id}/results — full classified record list
"""

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

import auth
import config
import runs
import validator
from schema import DiscoveryRunSummary

logger = logging.getLogger(__name__)

router = APIRouter()

# Discovery runs carry this run_type in status.json so the enrichment run
# listing can exclude them — they have no enriched_targets.json and must not be
# mixed into the enrichment dashboard.
RUN_TYPE = "discovery"

DISCOVERY_TIMEOUT_SECONDS = 300

# Artifact filenames written by the discovery engine into the run directory.
OUTPUT_FILENAMES: dict[str, str] = {
    "results_json": "discovery_results.json",
    "results_csv": "discovery_results.csv",
    "run_log": "discovery_run_log.json",
    "registry_preview": "updated_registry_preview.json",
}

# Discovery compares against the same registry the rest of the system maintains:
# a sibling of the runs/ directory (matches pipeline-api/discovery.py). Resolved
# at call time so tests that repoint runs.OUTPUT_RUNS_PATH are honored.
_REGISTRY_FILENAME = "master_practice_registry.json"


def registry_path() -> Path:
    """Return the path to the master practice registry (registry source)."""
    return runs.OUTPUT_RUNS_PATH.parent / _REGISTRY_FILENAME


def _status_path(run_id: str) -> Path:
    """Return the status.json path for a discovery run (validates run_id)."""
    return runs.run_dir(run_id) / config.STATUS_FILENAME


def _write_status(run_id: str, summary: DiscoveryRunSummary) -> None:
    """Write status.json atomically (temp file + os.replace)."""
    path = _status_path(run_id)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(summary.model_dump(), f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _spawn_discovery(
    input_csv: Path,
    registry: Path,
    output_dir: Path,
    run_id: str,
) -> subprocess.CompletedProcess:
    """Run discovery_cli.py as a subprocess and return the completed process.

    Isolated in its own function so the orchestration can be unit-tested without
    spawning a real process.
    """
    return subprocess.run(
        [
            config.PYTHON_EXECUTABLE,
            str(config.PIPELINE_REPO_PATH / "discovery_cli.py"),
            "--input", str(input_csv),
            "--registry", str(registry),
            "--output-dir", str(output_dir),
            "--run-id", run_id,
        ],
        capture_output=True,
        cwd=str(config.PIPELINE_REPO_PATH),
        timeout=DISCOVERY_TIMEOUT_SECONDS,
    )


def create_discovery_run(
    content: bytes,
    input_filename: str,
    operator: str,
) -> DiscoveryRunSummary:
    """Create a discovery run directory, run the comparison, and write status.

    The CSV must already be validated by the caller. On engine failure a failed
    status.json is written (preserving input.csv for debugging) and ValueError
    is raised. Returns the summary on success.
    """
    run_id = runs.generate_run_id()
    run_directory = runs.run_dir(run_id)
    run_directory.mkdir(parents=True, exist_ok=True)

    input_csv = run_directory / "input.csv"
    input_csv.write_bytes(content)

    created_at = datetime.now(timezone.utc).isoformat()

    try:
        proc = _spawn_discovery(input_csv, registry_path(), run_directory, run_id)
    except subprocess.TimeoutExpired:
        return _fail(run_id, created_at, operator, input_filename,
                     "Discovery timed out.")

    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace")[:500]
        logger.error("Discovery run %s exited %d: %s", run_id, proc.returncode, stderr)
        return _fail(run_id, created_at, operator, input_filename,
                     _engine_error(proc) or "Discovery engine failed.")

    try:
        engine_summary = json.loads((proc.stdout or b"").decode("utf-8"))
    except json.JSONDecodeError:
        logger.error("Discovery run %s returned non-JSON output", run_id)
        return _fail(run_id, created_at, operator, input_filename,
                     "Discovery produced unreadable output.")

    summary = DiscoveryRunSummary(
        run_id=run_id,
        status="complete",
        created_at=created_at,
        completed_at=datetime.now(timezone.utc).isoformat(),
        operator=operator,
        input_filename=input_filename,
        total_imported=engine_summary.get("total_imported", 0),
        new_count=engine_summary.get("new_count", 0),
        changed_count=engine_summary.get("changed_count", 0),
        known_count=engine_summary.get("known_count", 0),
        possible_duplicate_count=engine_summary.get("possible_duplicate_count", 0),
        insufficient_data_count=engine_summary.get("insufficient_data_count", 0),
        output_paths=dict(OUTPUT_FILENAMES),
    )
    _write_status(run_id, summary)
    logger.info(
        "Discovery run %s complete by '%s' (%d imported)",
        run_id, operator, summary.total_imported,
    )
    return summary


def _engine_error(proc: subprocess.CompletedProcess) -> str:
    """Extract the engine's {"error": ...} message from stdout, if present."""
    try:
        payload = json.loads((proc.stdout or b"").decode("utf-8"))
        return str(payload.get("error", ""))
    except (json.JSONDecodeError, ValueError):
        return ""


def _fail(
    run_id: str,
    created_at: str,
    operator: str,
    input_filename: str,
    reason: str,
) -> DiscoveryRunSummary:
    """Write a failed status.json and raise ValueError with the reason."""
    summary = DiscoveryRunSummary(
        run_id=run_id,
        status="failed",
        created_at=created_at,
        completed_at=datetime.now(timezone.utc).isoformat(),
        operator=operator,
        input_filename=input_filename,
        error_summary=reason,
    )
    _write_status(run_id, summary)
    raise ValueError(reason)


def get_discovery_summary(run_id: str) -> Optional[DiscoveryRunSummary]:
    """Read a discovery run's status.json, or None if absent or not a discovery run."""
    if not runs.is_valid_run_id(run_id):
        return None
    path = _status_path(run_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("run_type") != RUN_TYPE:
        return None
    return DiscoveryRunSummary(**data)


def read_discovery_results(run_id: str) -> Optional[dict]:
    """Read a discovery run's discovery_results.json, or None if absent."""
    if not runs.is_valid_run_id(run_id):
        return None
    path = runs.run_dir(run_id) / OUTPUT_FILENAMES["results_json"]
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/discovery-runs")
async def create_discovery_run_route(
    file: UploadFile = File(...),
    username: str = Depends(auth.require_session),
):
    """Upload an Outscraper CSV and run a discovery comparison.

    Validates the CSV, then runs the (blocking) discovery subprocess in the
    threadpool so the event loop is not blocked. Returns the run summary.
    """
    try:
        content, _row_count = await validator.validate_csv_upload(
            file, "outscraper", RUN_TYPE
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    input_filename = getattr(file, "filename", None) or "upload.csv"
    try:
        summary = await run_in_threadpool(
            create_discovery_run, content, input_filename, username
        )
    except ValueError as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return JSONResponse(status_code=201, content=summary.model_dump())


@router.get("/discovery-runs/{run_id}")
async def get_discovery_run_route(
    run_id: str,
    username: str = Depends(auth.require_session),
):
    """Return a discovery run's status and summary."""
    summary = get_discovery_summary(run_id)
    if summary is None:
        return JSONResponse(status_code=404, content={"detail": "Discovery run not found."})
    return JSONResponse(content=summary.model_dump())


@router.get("/discovery-runs/{run_id}/results")
async def get_discovery_results_route(
    run_id: str,
    username: str = Depends(auth.require_session),
):
    """Return the full classified record list for a discovery run."""
    if get_discovery_summary(run_id) is None:
        return JSONResponse(status_code=404, content={"detail": "Discovery run not found."})
    results = read_discovery_results(run_id)
    if results is None:
        return JSONResponse(
            status_code=404, content={"detail": "Discovery results not available."}
        )
    return JSONResponse(content=results)
