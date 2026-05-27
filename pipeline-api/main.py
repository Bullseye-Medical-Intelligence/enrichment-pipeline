"""
main.py
FastAPI application: route registration, startup validation, and global
exception handling. Route handlers contain only orchestration logic —
no business rules.
"""

import json
import logging
import traceback
from datetime import datetime, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

import auth
import runner
import runs
import validator
from config import HOST, OUTPUT_RUNS_PATH, PORT
from schema import ErrorResponse, RunCreateResponse, RunListResponse, RunStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="BEMI Pipeline API",
    description=(
        "Process manager and file bridge between the BEMI dashboard "
        "and the Bullseye enrichment pipeline."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch all unhandled exceptions, log full context, return clean 500."""
    run_id = request.path_params.get("run_id", "unknown")
    logger.error(
        "Unhandled exception on %s %s (run_id=%s): %s\n%s",
        request.method,
        request.url.path,
        run_id,
        exc,
        traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error. Run ID: {run_id}"},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post(
    "/runs",
    response_model=RunCreateResponse,
    status_code=202,
    dependencies=[Depends(auth.verify_api_key)],
    summary="Upload a CSV and start an enrichment run",
)
async def create_run(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    source_type: str = Form(...),
    project_id: str = Form(...),
    operator: str = Form(...),
) -> RunCreateResponse:
    """
    Validate the CSV, create a run directory, launch the pipeline subprocess,
    and return immediately. The pipeline runs in the background.
    """
    try:
        content, row_count = await validator.validate_csv_upload(
            file, source_type, project_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    run_id = _generate_run_id()
    run_dir = OUTPUT_RUNS_PATH / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    input_path = run_dir / "input.csv"
    input_path.write_bytes(content)

    runs.create_run(
        run_id=run_id,
        project_id=project_id,
        source_type=source_type,
        input_filename=file.filename or "upload.csv",
        operator=operator,
        records_input=row_count,
    )

    process = runner.spawn_pipeline(run_id, input_path, source_type, run_dir)
    runs.update_run_status(run_id, status="running")

    background_tasks.add_task(runner.monitor_pipeline, run_id, process)

    logger.info("Run %s started by '%s' (%d rows)", run_id, operator, row_count)
    return RunCreateResponse(run_id=run_id, status="running")


@app.get(
    "/runs",
    response_model=RunListResponse,
    dependencies=[Depends(auth.verify_api_key)],
    summary="List all runs, newest first",
)
async def list_runs() -> RunListResponse:
    """Return up to 50 runs sorted by creation time, newest first."""
    summaries = runs.list_runs()
    return RunListResponse(runs=summaries, total=len(summaries))


@app.get(
    "/runs/{run_id}",
    response_model=RunStatus,
    dependencies=[Depends(auth.verify_api_key)],
    summary="Get full status for a single run",
)
async def get_run(run_id: str) -> RunStatus:
    """Return the full status.json content for the given run."""
    status = _require_run(run_id)
    return status


@app.get(
    "/runs/{run_id}/log",
    dependencies=[Depends(auth.verify_api_key)],
    summary="Get the run log for a completed run",
)
async def get_run_log(run_id: str):
    """
    Return run_log.json for a run that has exited (complete or failed).
    Returns 425 if the run is still pending or running.
    """
    status = _require_run(run_id)
    _require_exited(status)

    log_path = runs.run_dir(run_id) / "run_log.json"
    if not log_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"run_log.json not found for run '{run_id}'",
        )

    with open(log_path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get(
    "/runs/{run_id}/results",
    dependencies=[Depends(auth.verify_api_key)],
    summary="Get enriched targets for a successfully completed run",
)
async def get_run_results(run_id: str):
    """
    Return enriched_targets.json for a run with status 'complete'.
    Returns 425 if the run has not reached 'complete'.
    """
    status = _require_run(run_id)
    _require_complete(status)

    results_path = runs.run_dir(run_id) / "enriched_targets.json"
    if not results_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"enriched_targets.json not found for run '{run_id}'",
        )

    with open(results_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_run_id() -> str:
    """Generate a unique run ID in RUN-YYYYMMDD-HHMMSS format."""
    return f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


def _require_run(run_id: str) -> RunStatus:
    """Return RunStatus or raise HTTP 404."""
    status = runs.get_run(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return status


def _require_exited(status: RunStatus) -> None:
    """Raise HTTP 425 if the run has not yet exited."""
    if status.status in ("pending", "running"):
        raise HTTPException(
            status_code=425,
            detail=f"Run '{status.run_id}' is still {status.status}. Try again later.",
        )


def _require_complete(status: RunStatus) -> None:
    """Raise HTTP 425 if the run has not reached 'complete' status."""
    if status.status != "complete":
        raise HTTPException(
            status_code=425,
            detail=(
                f"Run '{status.run_id}' has not completed successfully "
                f"(current status: {status.status})"
            ),
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
