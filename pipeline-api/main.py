"""
main.py
FastAPI application: route registration, startup, and global exception handling.
Route handlers contain only orchestration calls — no business rules.
"""

import json
import logging
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import auth
import exports
import runner
import runs
from config import HOST, PORT
from schema import RunCreateResponse, RunListResponse, RunStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

@asynccontextmanager
def _log_pipeline_python() -> None:
    """Log which Python runs the pipeline and whether it has a browser.

    Browser re-crawl runs in the spawned pipeline subprocess, not this server.
    Logging the interpreter and its Playwright/Chromium availability at startup
    makes a silently-broken re-crawl diagnosable from the server log.
    """
    import subprocess
    from config import PYTHON_EXECUTABLE

    logger.info("Pipeline subprocess Python: %s", PYTHON_EXECUTABLE)
    probe = (
        "from playwright.sync_api import sync_playwright; "
        "p=sync_playwright().start(); "
        "b=p.chromium.launch(headless=True); b.close(); print('ok')"
    )
    try:
        result = subprocess.run(
            [PYTHON_EXECUTABLE, "-c", probe],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            logger.info("Browser re-crawl ready: Chromium launches in pipeline Python")
        else:
            logger.warning(
                "Browser re-crawl UNAVAILABLE in pipeline Python — re-crawl will "
                "produce no data. Fix: %s -m playwright install chromium. Detail: %s",
                PYTHON_EXECUTABLE, (result.stderr or "").strip()[:200],
            )
    except Exception as e:  # never block startup on the probe
        logger.warning("Could not probe browser availability: %s", str(e)[:160])


async def _lifespan(app: FastAPI):
    """On startup, fail any run orphaned by a prior server instance."""
    count = runs.reconcile_orphaned_runs()
    if count:
        logger.warning("Reconciled %d orphaned run(s) on startup", count)
    _log_pipeline_python()
    yield


app = FastAPI(
    title="BEMI Pipeline API",
    description=(
        "Process manager and file bridge between the BEMI dashboard "
        "and the Bullseye enrichment pipeline."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
    lifespan=_lifespan,
)

# Mount static files for the web UI
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Register UI routes (login, dashboard, upload, results)
from ui import router as ui_router  # noqa: E402 — after app creation
app.include_router(ui_router)


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
# API Routes — Bearer token auth, JSON only
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
    """Validate the CSV, launch the pipeline subprocess, and return immediately."""
    try:
        run_id, _ = await runner.orchestrate_run(
            file, source_type, project_id, operator, background_tasks
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
    return _require_run(run_id)


@app.get(
    "/runs/{run_id}/log",
    dependencies=[Depends(auth.verify_api_key)],
    summary="Get the run log for a completed run",
)
async def get_run_log(run_id: str):
    """Return run_log.json for a run that has exited (complete or failed)."""
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
    """Return enriched_targets.json for a run with status 'complete'.
    Response shape: {run_id, generated_at, record_count, records: [...]}.
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


@app.get(
    "/runs/{run_id}/export/approved",
    dependencies=[Depends(auth.verify_api_key)],
    summary="Download a CSV of approved, non-excluded records",
)
async def api_export_approved(run_id: str):
    """Return a CSV of approved non-excluded records with analyst review overlay."""
    _require_complete(_require_run(run_id))
    run_directory = runs.run_dir(run_id)
    buf = exports.build_approved_csv(run_id, run_directory)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_approved.csv"'},
    )


@app.get(
    "/runs/{run_id}/export/excluded",
    dependencies=[Depends(auth.verify_api_key)],
    summary="Download a CSV of excluded records",
)
async def api_export_excluded(run_id: str):
    """Return a CSV of all records whose effective tier is Excluded."""
    _require_complete(_require_run(run_id))
    run_directory = runs.run_dir(run_id)
    buf = exports.build_excluded_csv(run_id, run_directory)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_excluded.csv"'},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
