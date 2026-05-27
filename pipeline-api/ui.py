"""
ui.py
Server-rendered HTML routes for the internal operator dashboard.
All business logic lives in runs.py, runner.py, reviews.py, and validator.py.
This module handles only: request parsing, template rendering, redirects, and
simple orchestration calls.
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

import auth
import reviews
import runner
import runs
from schema import ReviewEdit

logger = logging.getLogger(__name__)

router = APIRouter()

_jinja_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)


def _render(name: str, status_code: int = 200, **context) -> HTMLResponse:
    """Render a Jinja2 template and return an HTMLResponse."""
    return HTMLResponse(_jinja_env.get_template(name).render(**context), status_code=status_code)

# Tiers known to the UI (pipeline tiers + analyst override tiers)
_ALL_TIERS = {"Bullseye", "Strong", "Warm", "Cold", "Watchlist", "Excluded"}


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Render the login form."""
    return _render("login.html", error=None)


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Validate credentials, set session cookie, redirect to main menu."""
    if not auth.validate_credentials(username, password):
        logger.warning("Failed login attempt for username '%s'", username)
        return _render("login.html", error="Incorrect username or password.", status_code=401)
    response = RedirectResponse(url="/", status_code=303)
    auth.create_session_cookie(response, username)
    return response


@router.get("/logout")
async def logout():
    """Clear session cookie and redirect to login."""
    response = RedirectResponse(url="/login", status_code=303)
    auth.clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def menu(request: Request, username: str = Depends(auth.require_session)):
    """Render the main menu with the 5 most recent runs."""
    recent = runs.list_runs(max_runs=5)
    return _render("menu.html", username=username, recent_runs=recent)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request, username: str = Depends(auth.require_session)):
    """Render the CSV upload form."""
    return _render("upload.html", username=username, error=None)


# ---------------------------------------------------------------------------
# Run list
# ---------------------------------------------------------------------------

@router.get("/dashboard", response_class=HTMLResponse)
async def runs_page(request: Request, username: str = Depends(auth.require_session)):
    """Render the full run list."""
    all_runs = runs.list_runs()
    return _render("runs.html", username=username, runs=all_runs)


# ---------------------------------------------------------------------------
# Results / review dashboard
# ---------------------------------------------------------------------------

@router.get("/dashboard/{run_id}", response_class=HTMLResponse)
async def results_page(
    request: Request,
    run_id: str,
    username: str = Depends(auth.require_session),
):
    """Render enriched records for a run with inline review controls."""
    status = runs.get_run(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    merged_records = []
    if status.status == "complete":
        run_directory = runs.run_dir(run_id)
        results_path = run_directory / "enriched_targets.json"
        if results_path.exists():
            with open(results_path, "r", encoding="utf-8") as f:
                raw_records = json.load(f)
            all_reviews = reviews.get_reviews(run_id, run_directory)
            for record in raw_records:
                record_id = record.get("record_id", "")
                review = all_reviews.get(record_id, reviews.default_review())
                merged_records.append({
                    **record,
                    "review": review,
                    "displayed_tier": review.get("override_tier") or record.get("target_tier", ""),
                })

    stats = _calculate_stats(merged_records)

    return _render(
        "results.html",
        username=username,
        run_id=run_id,
        status=status,
        records=merged_records,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# JSON API endpoints for UI actions (session auth)
# ---------------------------------------------------------------------------

@router.get("/runs/{run_id}/download/json")
async def download_json(run_id: str, username: str = Depends(auth.require_session)):
    """Download enriched_targets.json for a completed run."""
    path = runs.run_dir(run_id) / "enriched_targets.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="enriched_targets.json not found")
    return FileResponse(str(path), filename=f"{run_id}_enriched_targets.json",
                        media_type="application/json")


@router.get("/runs/{run_id}/download/csv")
async def download_csv(run_id: str, username: str = Depends(auth.require_session)):
    """Download enriched_targets.csv for a completed run."""
    path = runs.run_dir(run_id) / "enriched_targets.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="enriched_targets.csv not found")
    return FileResponse(str(path), filename=f"{run_id}_enriched_targets.csv",
                        media_type="text/csv")


@router.post("/api/ui/runs")
async def ui_create_run(
    background_tasks: BackgroundTasks,
    request: Request,
    file: UploadFile,
    source_type: str = Form(...),
    project_id: str = Form(...),
    operator: str = Form(...),
    username: str = Depends(auth.require_session),
):
    """
    Create a run from the web upload form.
    Uses the same orchestration logic as POST /runs (no duplication).
    """
    try:
        run_id, row_count = await runner.orchestrate_run(
            file, source_type, project_id, operator, background_tasks
        )
    except ValueError as e:
        return _render("upload.html", status_code=400, username=username, error=str(e))
    return RedirectResponse(url=f"/dashboard/{run_id}", status_code=303)


@router.post("/api/ui/reviews/{run_id}/{record_id}")
async def save_review(
    run_id: str,
    record_id: str,
    edit: ReviewEdit,
    username: str = Depends(auth.require_session),
):
    """Save an analyst review edit for a single record."""
    run_directory = runs.run_dir(run_id)
    if not run_directory.exists():
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    try:
        saved = reviews.save_review(run_id, record_id, edit, username, run_directory)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})

    return JSONResponse(content={"ok": True, "review": saved})


# ---------------------------------------------------------------------------
# Helper: presentation-layer stats (not business logic)
# ---------------------------------------------------------------------------

def _calculate_stats(records: list[dict]) -> dict:
    """Count records by displayed tier and QC status for the results header."""
    stats = {
        "total": len(records),
        "bullseye": 0,
        "strong": 0,
        "warm": 0,
        "cold": 0,
        "watchlist": 0,
        "excluded": 0,
        "pending_review": 0,
        "approved": 0,
        "rejected": 0,
    }
    for r in records:
        tier = (r.get("displayed_tier") or "").lower()
        if tier in stats:
            stats[tier] += 1
        qc = (r.get("review") or {}).get("qc_status", "pending")
        if qc in stats:
            stats[qc] += 1
    return stats
