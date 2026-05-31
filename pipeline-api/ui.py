"""
ui.py
Server-rendered HTML routes for the internal operator dashboard.
All business logic lives in the service modules (runs, runner, reviews,
validator, projects, icp_profiles). This module handles only: request parsing,
template rendering, redirects, and simple orchestration calls.
"""

import json
import logging
import re
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

import auth
import client_exports
import config
import exports
import icp_profiles
import projects
import record_adapter
import reviews
import runner
import runs
import validator
from crawl_compressor import compress_crawl
from narrative_generator import generate_narrative
from schema import ReviewEdit
from signal_generator import generate_signals

logger = logging.getLogger(__name__)

router = APIRouter()

_jinja_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)
# Expose presentation helpers to all templates as globals.
_jinja_env.globals["friendly_error"] = lambda raw: _friendly_error(raw)


def _render(name: str, status_code: int = 200, **context) -> HTMLResponse:
    """Render a Jinja2 template and return an HTMLResponse."""
    return HTMLResponse(_jinja_env.get_template(name).render(**context), status_code=status_code)


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
    """Render the main menu with recent runs and project/ICP counts."""
    recent = runs.list_runs(max_runs=5)
    return _render(
        "menu.html",
        username=username,
        recent_runs=recent,
        project_count=len(projects.list_projects()),
        icp_count=len(icp_profiles.list_icp_profiles()),
    )


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@router.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request, username: str = Depends(auth.require_session)):
    """List all configured projects."""
    return _render("projects.html", username=username, projects=projects.list_projects())


@router.get("/projects/new", response_class=HTMLResponse)
async def new_project_page(request: Request, username: str = Depends(auth.require_session)):
    """Render the create-project form with the available ICP profiles."""
    return _render(
        "project_new.html",
        username=username,
        icp_profiles=icp_profiles.list_icp_profiles(),
        error=None,
        form={},
    )


@router.post("/projects")
async def create_project_submit(
    request: Request,
    project_id: str = Form(...),
    client_name: str = Form(...),
    target_specialty: str = Form(...),
    target_geography: str = Form(""),
    icp_profile_id: str = Form(...),
    client_website: str = Form(""),
    product_name: str = Form(""),
    active_exclusion_rules: str = Form(""),
    subpage_keywords: str = Form(""),
    bullseye_min_score: str = Form(""),
    max_pages_per_practice: str = Form(""),
    request_timeout_seconds: str = Form(""),
    request_retries: str = Form(""),
    io_concurrency: str = Form(""),
    notes: str = Form(""),
    username: str = Depends(auth.require_session),
):
    """Create a project from the form, then redirect to its detail page.

    Blank advanced fields fall back to the service's generic defaults.
    """
    form = {
        "project_id": project_id, "client_name": client_name,
        "target_specialty": target_specialty, "target_geography": target_geography,
        "icp_profile_id": icp_profile_id, "client_website": client_website,
        "product_name": product_name, "active_exclusion_rules": active_exclusion_rules,
        "subpage_keywords": subpage_keywords, "bullseye_min_score": bullseye_min_score,
        "max_pages_per_practice": max_pages_per_practice,
        "request_timeout_seconds": request_timeout_seconds,
        "request_retries": request_retries, "io_concurrency": io_concurrency,
        "notes": notes,
    }
    try:
        project_data = _parse_project_form(form, created_by=username)
        projects.create_project(project_data)
    except ValueError as e:
        return _render(
            "project_new.html",
            status_code=400,
            username=username,
            icp_profiles=icp_profiles.list_icp_profiles(),
            error=str(e),
            form=form,
        )
    return RedirectResponse(url=f"/projects/{project_data['project_id']}", status_code=303)


@router.get("/projects/{project_id}/edit", response_class=HTMLResponse)
async def project_edit_page(
    request: Request,
    project_id: str,
    username: str = Depends(auth.require_session),
):
    """Render the edit-project form pre-populated with existing values."""
    project = projects.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return _render(
        "project_edit.html",
        username=username,
        project=project,
        icp_profiles=icp_profiles.list_icp_profiles(),
        error=None,
        form=_project_to_form(project),
    )


@router.post("/projects/{project_id}")
async def project_update_submit(
    request: Request,
    project_id: str,
    client_name: str = Form(...),
    target_specialty: str = Form(...),
    target_geography: str = Form(""),
    icp_profile_id: str = Form(...),
    client_website: str = Form(""),
    product_name: str = Form(""),
    active_exclusion_rules: str = Form(""),
    subpage_keywords: str = Form(""),
    bullseye_min_score: str = Form(""),
    max_pages_per_practice: str = Form(""),
    request_timeout_seconds: str = Form(""),
    request_retries: str = Form(""),
    io_concurrency: str = Form(""),
    notes: str = Form(""),
    username: str = Depends(auth.require_session),
):
    """Update an existing project from the edit form."""
    form = {
        "project_id": project_id, "client_name": client_name,
        "target_specialty": target_specialty, "target_geography": target_geography,
        "icp_profile_id": icp_profile_id, "client_website": client_website,
        "product_name": product_name, "active_exclusion_rules": active_exclusion_rules,
        "subpage_keywords": subpage_keywords, "bullseye_min_score": bullseye_min_score,
        "max_pages_per_practice": max_pages_per_practice,
        "request_timeout_seconds": request_timeout_seconds,
        "request_retries": request_retries, "io_concurrency": io_concurrency,
        "notes": notes,
    }
    project = projects.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    try:
        project_data = _parse_project_form(form, created_by=None)
        projects.update_project(project_id, project_data)
    except ValueError as e:
        return _render(
            "project_edit.html",
            status_code=400,
            username=username,
            project=project,
            icp_profiles=icp_profiles.list_icp_profiles(),
            error=str(e),
            form=form,
        )
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail_page(
    request: Request,
    project_id: str,
    username: str = Depends(auth.require_session),
):
    """Show a single project's configuration."""
    project = projects.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return _render("project_detail.html", username=username, project=project)


# ---------------------------------------------------------------------------
# ICP profiles
# ---------------------------------------------------------------------------

@router.get("/icp-profiles/new", response_class=HTMLResponse)
async def icp_build_page(request: Request, username: str = Depends(auth.require_session)):
    """Render the AI-assisted ICP profile builder form."""
    return _render("icp_build.html", username=username, error=None, form={})


@router.post("/icp-profiles/generate", response_class=HTMLResponse)
async def icp_generate(
    request: Request,
    username: str = Depends(auth.require_session),
    company_name: str = Form(""),
    company_url: str = Form(""),
    product_name: str = Form(...),
    product_type: str = Form(""),
    product_url: str = Form(""),
    description: str = Form(...),
    specialty: str = Form(...),
    focus_areas: str = Form(""),
    exclusion_notes: str = Form(""),
    icp_id: str = Form(...),
    icp_name: str = Form(...),
    version: str = Form("1.0"),
):
    """Stage 1 + Stage 2: crawl and compress product context, then generate signals from anchor template."""
    form = {
        "company_name": company_name, "company_url": company_url,
        "product_name": product_name, "product_type": product_type,
        "product_url": product_url, "description": description,
        "specialty": specialty, "focus_areas": focus_areas,
        "exclusion_notes": exclusion_notes, "icp_id": icp_id,
        "icp_name": icp_name, "version": version,
    }
    if not config.ANTHROPIC_API_KEY:
        return _render("icp_build.html", username=username, error=(
            "ANTHROPIC_API_KEY is not configured. Add it to .env and restart the server."
        ), form=form)
    crawl_notes: list[str] = []
    company_page_text = _fetch_page_text(company_url)
    if company_url:
        crawl_notes.append(
            f"Crawled company page ({len(company_page_text):,} chars)."
            if company_page_text else "Company URL could not be fetched — proceeding without it."
        )
    product_page_text = _fetch_page_text(product_url)
    if product_url:
        crawl_notes.append(
            f"Crawled product page ({len(product_page_text):,} chars)."
            if product_page_text else "Product URL could not be fetched — proceeding without it."
        )
    try:
        brief = compress_crawl(
            company_name=company_name,
            product_name=product_name,
            specialty=specialty,
            description=description,
            company_page_text=company_page_text,
            product_page_text=product_page_text,
        )
        signal_set = generate_signals(brief=brief, specialty=specialty)
    except Exception as exc:
        logger.warning("ICP draft generation failed: %s", exc)
        return _render("icp_build.html", username=username, error=(
            "Signal generation failed. Try again, or adjust your description."
        ), form=form)
    return _render(
        "icp_review.html",
        username=username,
        error=None,
        signals=signal_set.signals,
        hypothesis={},
        icp_description="",
        demo_accounts=[],
        crawl_notes=crawl_notes,
        product_brief_json=brief.model_dump_json(),
        icp_id=icp_id,
        icp_name=icp_name,
        version=version,
        company_name=company_name,
        company_url=company_url,
        product_name=product_name,
        product_type=product_type,
        product_url=product_url,
        specialty=specialty,
    )


@router.post("/icp-profiles/regenerate", response_class=HTMLResponse)
async def icp_regenerate_signals(
    request: Request,
    username: str = Depends(auth.require_session),
    product_brief_json: str = Form(...),
    specialty: str = Form(...),
    icp_id: str = Form(...),
    icp_name: str = Form(...),
    version: str = Form("1.0"),
    company_name: str = Form(""),
    company_url: str = Form(""),
    product_name: str = Form(""),
    product_type: str = Form(""),
    product_url: str = Form(""),
    demo_accounts_json: str = Form(""),
):
    """Stage 2 only: re-run signal generation from the cached brief. No crawl."""
    form_data = await request.form()
    try:
        from crawl_compressor import ProductBrief
        brief = ProductBrief.model_validate_json(product_brief_json)
        signal_set = generate_signals(brief=brief, specialty=specialty)
    except Exception as exc:
        logger.warning("Signal regeneration failed: %s", exc)
        return _render(
            "icp_review.html",
            username=username,
            error="Signal regeneration failed. Try again.",
            signals=[],
            hypothesis=_parse_hypothesis_from_form(form_data),
            icp_description=(form_data.get("icp_description") or "").strip(),
            demo_accounts=_parse_demo_accounts(demo_accounts_json),
            crawl_notes=[],
            product_brief_json=product_brief_json,
            icp_id=icp_id, icp_name=icp_name, version=version,
            company_name=company_name, company_url=company_url,
            product_name=product_name, product_type=product_type,
            product_url=product_url, specialty=specialty,
        )
    return _render(
        "icp_review.html",
        username=username,
        error=None,
        signals=signal_set.signals,
        hypothesis=_parse_hypothesis_from_form(form_data),
        icp_description=(form_data.get("icp_description") or "").strip(),
        demo_accounts=_parse_demo_accounts(demo_accounts_json),
        crawl_notes=["Signals regenerated from cached product brief. No re-crawl."],
        product_brief_json=product_brief_json,
        icp_id=icp_id, icp_name=icp_name, version=version,
        company_name=company_name, company_url=company_url,
        product_name=product_name, product_type=product_type,
        product_url=product_url, specialty=specialty,
    )


@router.post("/icp-profiles/approve", response_class=HTMLResponse)
async def icp_approve(
    request: Request,
    username: str = Depends(auth.require_session),
    product_brief_json: str = Form(...),
    specialty: str = Form(...),
    signal_count: int = Form(...),
    icp_id: str = Form(...),
    icp_name: str = Form(...),
    version: str = Form("1.0"),
    company_name: str = Form(""),
    company_url: str = Form(""),
    product_name: str = Form(""),
    product_type: str = Form(""),
    product_url: str = Form(""),
    icp_description: str = Form(""),
):
    """Stage 3: generate narrative and demo accounts from the approved signal set."""
    form_data = await request.form()
    approved_signals = _parse_signals_from_form(form_data, signal_count)
    try:
        from crawl_compressor import ProductBrief
        brief = ProductBrief.model_validate_json(product_brief_json)
        narrative = generate_narrative(brief=brief, approved_signals=approved_signals)
    except Exception as exc:
        logger.warning("Narrative generation failed: %s", exc)
        return _render(
            "icp_review.html",
            username=username,
            error="Demo brief generation failed. Try again.",
            signals=approved_signals,
            hypothesis=_parse_hypothesis_from_form(form_data),
            icp_description=icp_description.strip(),
            demo_accounts=[],
            crawl_notes=[],
            product_brief_json=product_brief_json,
            icp_id=icp_id, icp_name=icp_name, version=version,
            company_name=company_name, company_url=company_url,
            product_name=product_name, product_type=product_type,
            product_url=product_url, specialty=specialty,
        )
    return _render(
        "icp_review.html",
        username=username,
        error=None,
        signals=approved_signals,
        hypothesis=narrative.hypothesis,
        icp_description=narrative.description,
        demo_accounts=narrative.demo_accounts,
        crawl_notes=["Demo brief generated from approved signals."],
        product_brief_json=product_brief_json,
        icp_id=icp_id, icp_name=icp_name, version=version,
        company_name=company_name, company_url=company_url,
        product_name=product_name, product_type=product_type,
        product_url=product_url, specialty=specialty,
    )


@router.post("/icp-profiles/save")
async def icp_save(
    request: Request,
    username: str = Depends(auth.require_session),
    icp_id: str = Form(...),
    icp_name: str = Form(...),
    version: str = Form("1.0"),
    signal_count: int = Form(...),
    company_name: str = Form(""),
    company_url: str = Form(""),
    product_name: str = Form(""),
    product_type: str = Form(""),
    product_url: str = Form(""),
    specialty: str = Form(""),
    icp_description: str = Form(""),
    demo_accounts_json: str = Form(""),
    product_brief_json: str = Form(""),
):
    """Validate and persist the edited ICP profile to disk."""
    form_data = await request.form()
    signals = _parse_signals_from_form(form_data, signal_count)
    hypothesis = _parse_hypothesis_from_form(form_data)
    demo_accounts = _parse_demo_accounts(demo_accounts_json)
    try:
        product_brief = json.loads(product_brief_json) if product_brief_json.strip() else {}
        if not isinstance(product_brief, dict):
            product_brief = {}
    except (ValueError, TypeError):
        product_brief = {}
    profile = {
        "icp_id": icp_id.strip(),
        "name": icp_name.strip(),
        "version": version.strip() or "1.0",
        "description": icp_description.strip(),
        "hypothesis": hypothesis,
        "demo_accounts": demo_accounts,
        "product_brief": product_brief,
        "source_urls": {"company_url": company_url.strip(), "product_url": product_url.strip()},
        "signals": signals,
    }
    try:
        icp_profiles.save_icp_profile(profile)
    except ValueError as exc:
        return _render(
            "icp_review.html",
            username=username,
            error=str(exc),
            signals=signals,
            hypothesis=hypothesis,
            icp_description=icp_description,
            demo_accounts=demo_accounts,
            crawl_notes=[],
            product_brief_json=product_brief_json,
            icp_id=icp_id,
            icp_name=icp_name,
            version=version,
            company_name=company_name,
            company_url=company_url,
            product_name=product_name,
            product_type=product_type,
            product_url=product_url,
            specialty=specialty,
        )
    return RedirectResponse("/icp-profiles", status_code=303)


@router.get("/icp-profiles", response_class=HTMLResponse)
async def icp_profiles_page(request: Request, username: str = Depends(auth.require_session)):
    """List the ICP profiles available on disk."""
    return _render(
        "icp_profiles.html",
        username=username,
        profiles=icp_profiles.list_icp_profiles(),
    )


@router.get("/icp-profiles/{icp_id}/demo", response_class=HTMLResponse)
async def icp_demo_brief_page(
    request: Request, icp_id: str, username: str = Depends(auth.require_session)
):
    """Render the prospect-facing demo brief for a saved ICP profile."""
    try:
        profile = icp_profiles.get_icp_profile(icp_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _render("icp_demo_brief.html", username=username, profile=profile, pdf_mode=False)


@router.get("/icp-profiles/{icp_id}/demo.pdf")
async def icp_demo_brief_pdf(
    request: Request, icp_id: str, username: str = Depends(auth.require_session)
):
    """Return a WeasyPrint PDF of the demo brief for a saved ICP profile."""
    try:
        profile = icp_profiles.get_icp_profile(icp_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    pdf_bytes = _build_demo_brief_pdf(profile)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", icp_id)
    filename = f"bemi-demo-brief-{safe_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@router.get("/upload", response_class=HTMLResponse)
async def upload_page(
    request: Request,
    project_id: str = "",
    username: str = Depends(auth.require_session),
):
    """Render the CSV upload form. The project is chosen from existing projects.

    An optional ?project_id= preselects a project and shows its read-only
    client/ICP context.
    """
    all_projects = projects.list_projects()
    selected = projects.get_project(project_id) if project_id else None
    return _render(
        "upload.html",
        username=username,
        error=None,
        projects=all_projects,
        selected_project=selected,
        selected_context=_project_upload_context(selected),
    )


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

def _load_merged_records(run_id: str, status) -> list[dict]:
    """Load a run's records merged with their review overlay.

    Each record gains record_id, review, and displayed_tier. Returns an empty
    list unless the run is 'complete' (enriched) or 'ingested' (roster loaded,
    not yet enriched) — both have a written enriched_targets.json to render.
    """
    if status.status not in ("complete", "ingested"):
        return []
    run_directory = runs.run_dir(run_id)
    results_path = run_directory / "enriched_targets.json"
    if not results_path.exists():
        return []
    with open(results_path, "r", encoding="utf-8") as f:
        raw_records = record_adapter.normalize_records_payload(json.load(f))
    all_reviews = reviews.get_reviews(run_id, run_directory)
    merged = []
    for record in raw_records:
        record_id = record_adapter.get_record_id(record)
        review = all_reviews.get(record_id, reviews.default_review())
        merged.append({
            **record,
            "website_url": record_adapter.normalize_homepage_url(record.get("website_url", "")),
            "record_id": record_id,
            "review": review,
            "displayed_tier": record_adapter.displayed_tier(record, review),
        })
    return merged


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

    merged_records = _load_merged_records(run_id, status)

    stats = _calculate_stats(merged_records)
    project_context = _build_project_context(run_id)
    progress = runs.read_progress(run_id) if status.status in ("running", "pending") else None
    readiness = _compute_readiness(merged_records) if status.status == "complete" else None

    has_checkpoint = runs.has_step4_checkpoint(run_id)
    checkpoint_count = runs.step4_checkpoint_count(run_id) if has_checkpoint else 0

    return _render(
        "results.html",
        username=username,
        run_id=run_id,
        status=status,
        records=merged_records,
        stats=stats,
        project_context=project_context,
        progress=progress,
        readiness=readiness,
        friendly_error=_friendly_error(status.error_summary),
        has_checkpoint=has_checkpoint,
        checkpoint_count=checkpoint_count,
    )


@router.get("/dashboard/{run_id}/queue", response_class=HTMLResponse)
async def contact_queue_page(
    request: Request,
    run_id: str,
    show_all: bool = False,
    username: str = Depends(auth.require_session),
):
    """Render the rep-facing Contact Queue: a call sheet sorted by priority.

    Pure presentation of the existing records — relabels the tier as Contact
    Priority and sorts by (priority, confidence, score). "Do Not Pursue" records
    are hidden unless show_all is set.
    """
    status = runs.get_run(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    merged_records = _load_merged_records(run_id, status)
    for record in merged_records:
        review = record["review"]
        record["contact_priority"] = record_adapter.contact_priority(record, review)
        record["contact_priority_rank"] = record_adapter.contact_priority_rank(record, review)

    if not show_all:
        merged_records = [r for r in merged_records if r["contact_priority"] != "Do Not Pursue"]

    merged_records.sort(
        key=lambda r: (
            r["contact_priority_rank"],
            r.get("confidence_score", 0),
            r.get("bullseye_score", 0),
        ),
        reverse=True,
    )

    project_context = _build_project_context(run_id)

    return _render(
        "contact_queue.html",
        username=username,
        run_id=run_id,
        status=status,
        records=merged_records,
        project_context=project_context,
        show_all=show_all,
    )


# ---------------------------------------------------------------------------
# Run actions (resume, etc.)
# ---------------------------------------------------------------------------

@router.post("/runs/{run_id}/resume")
async def run_resume(
    run_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    username: str = Depends(auth.require_session),
):
    """Re-spawn a failed run from its Step 4 checkpoint."""
    try:
        await runner.resume_run(run_id, background_tasks)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(f"/dashboard/{run_id}", status_code=303)


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


@router.get("/runs/{run_id}/export/approved")
async def export_approved(run_id: str, username: str = Depends(auth.require_session)):
    """Download a CSV of approved, non-excluded records with analyst review overlay."""
    status = runs.get_run(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    if status.status != "complete":
        raise HTTPException(status_code=425,
            detail=f"Run is not complete (status: {status.status}). Wait for it to finish.")
    buf = exports.build_approved_csv(run_id, runs.run_dir(run_id))
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_approved.csv"'},
    )


@router.get("/runs/{run_id}/export/excluded")
async def export_excluded(run_id: str, username: str = Depends(auth.require_session)):
    """Download a CSV of all records whose effective tier is Excluded."""
    status = runs.get_run(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    if status.status != "complete":
        raise HTTPException(status_code=425,
            detail=f"Run is not complete (status: {status.status}). Wait for it to finish.")
    buf = exports.build_excluded_csv(run_id, runs.run_dir(run_id))
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_excluded.csv"'},
    )


@router.get("/runs/{run_id}/export/retry-crawl")
async def export_retry_crawl(run_id: str, username: str = Depends(auth.require_session)):
    """Download a manual-format CSV of records that failed to crawl, ready for re-upload."""
    status = runs.get_run(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    if status.status != "complete":
        raise HTTPException(status_code=425,
            detail=f"Run is not complete (status: {status.status}).")
    buf = exports.build_retry_csv(run_id, runs.run_dir(run_id))
    if not buf.getvalue():
        raise HTTPException(status_code=404, detail="No failed-crawl records found in this run.")
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_retry_crawl.csv"'},
    )


@router.post("/runs/{run_id}/records/{record_id}/recrawl")
async def recrawl_single_record(
    run_id: str,
    record_id: str,
    request: Request,
    website_url: str = Form(""),
    username: str = Depends(auth.require_session),
):
    """Re-crawl one practice with a headless browser and update its run in place."""
    try:
        await runner.orchestrate_single_recrawl(
            source_run_id=run_id,
            record_id=record_id,
            website_url_override=website_url,
            operator=username,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=f"/dashboard/{run_id}", status_code=303)


@router.post("/runs/{run_id}/records/{record_id}/manual-content")
async def manual_content_recrawl(
    run_id: str,
    record_id: str,
    request: Request,
    html_file: UploadFile | None = File(None),
    pasted_text: str = Form(""),
    username: str = Depends(auth.require_session),
):
    """Enrich one record from operator-provided page content, updating in place.

    For CAPTCHA-blocked sites the crawler cannot reach: the operator uploads a
    saved .html file or pastes the page text, signal extraction runs on it, and
    the updated record is merged back into the same run.
    """
    content_bytes = b""
    content_filename = "pasted.txt"
    if html_file is not None:
        content_bytes = await html_file.read()
        content_filename = html_file.filename or "uploaded.html"
    if not content_bytes.strip() and pasted_text.strip():
        content_bytes = pasted_text.encode("utf-8")
        content_filename = "pasted.txt"
    if not content_bytes.strip():
        raise HTTPException(
            status_code=400,
            detail="Provide an HTML file or paste page content.",
        )
    try:
        await runner.orchestrate_manual_content_recrawl(
            source_run_id=run_id,
            record_id=record_id,
            content_bytes=content_bytes,
            content_filename=content_filename,
            operator=username,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=f"/dashboard/{run_id}", status_code=303)


@router.post("/runs/{run_id}/retry-with-browser")
async def retry_with_browser(
    run_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    username: str = Depends(auth.require_session),
):
    """Start a Playwright re-crawl run for records that failed web extraction."""
    try:
        new_run_id, row_count = await runner.orchestrate_playwright_retry(
            source_run_id=run_id,
            operator=username,
            background_tasks=background_tasks,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=f"/dashboard/{new_run_id}", status_code=303)


@router.post("/runs/{run_id}/enrich-all")
async def enrich_all(
    run_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    auto_browser_retry: str = Form(default=""),
    username: str = Depends(auth.require_session),
):
    """Enrich an ingested roster: run the full pipeline over the loaded list.

    When the operator ticks "auto browser retry", blocked/thin sites are
    re-crawled once with headless Chromium before signal extraction.
    """
    try:
        await runner.orchestrate_enrich_all(
            run_id, username, background_tasks,
            auto_browser_retry=bool(auto_browser_retry),
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=f"/dashboard/{run_id}", status_code=303)


@router.get("/runs/{run_id}/client-package")
async def client_package(run_id: str, username: str = Depends(auth.require_session)):
    """Download a client deliverable ZIP for a completed, fully-reviewed run."""
    status = runs.get_run(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    if status.status != "complete":
        raise HTTPException(
            status_code=425,
            detail=f"Run '{run_id}' has not completed (current status: {status.status}).",
        )
    run_directory = runs.run_dir(run_id)
    pending = _pending_review_count(run_id, run_directory)
    if pending > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{pending} record{'s' if pending != 1 else ''} still pending review. "
                "Complete labeling before downloading the client package."
            ),
        )
    buf = client_exports.build_client_package(run_id, run_directory, status)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_client_package.zip"'},
    )


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

    Ingests the roster only (no crawl/LLM spend); the operator reviews the list
    and then triggers enrichment from the results page via POST
    /runs/{run_id}/enrich-all.
    """
    try:
        run_id, row_count = await runner.orchestrate_ingest(
            file, source_type, project_id, operator, background_tasks
        )
    except ValueError as e:
        selected = projects.get_project(project_id) if project_id else None
        return _render(
            "upload.html",
            status_code=400,
            username=username,
            error=str(e),
            projects=projects.list_projects(),
            selected_project=selected,
            selected_context=_project_upload_context(selected),
        )
    return RedirectResponse(url=f"/dashboard/{run_id}", status_code=303)


@router.post("/api/ui/runs/preview")
async def ui_preview_run(
    file: UploadFile,
    source_type: str = Form(...),
    username: str = Depends(auth.require_session),
):
    """Validate an upload and return an import summary without starting a run."""
    content = await file.read()
    try:
        summary = validator.preflight_summary(content, source_type)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return JSONResponse(content=summary)


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
# Helpers: presentation-layer context (not business logic)
# ---------------------------------------------------------------------------

def _build_project_context(run_id: str) -> dict | None:
    """Read a run's config + ICP snapshots for the results header.

    Returns None when no snapshot exists (e.g. runs created before projects),
    so the template degrades gracefully.
    """
    run_directory = runs.run_dir(run_id)
    cfg = projects.read_config_snapshot(run_directory)
    icp = icp_profiles.read_snapshot(run_directory)
    if cfg is None and icp is None:
        return None
    cfg = cfg or {}
    icp = icp or {}
    geography = cfg.get("target_geography")
    if isinstance(geography, list):
        geography = ", ".join(geography)
    return {
        "project_id": cfg.get("project_id"),
        "client_name": cfg.get("client_name"),
        "product_name": cfg.get("product_name"),
        "target_specialty": cfg.get("target_specialty"),
        "target_geography": geography,
        "icp_name": icp.get("name"),
        "icp_version": icp.get("version"),
    }


def _project_upload_context(project: dict | None) -> dict | None:
    """Build the read-only project context shown on the upload page.

    Resolves the project's ICP name/version for display. Returns None if no
    project is selected.
    """
    if not project:
        return None
    icp_name = icp_version = None
    icp_id = project.get("icp_profile_id")
    if icp_id:
        try:
            profile = icp_profiles.get_icp_profile(icp_id)
            icp_name, icp_version = profile.get("name"), profile.get("version")
        except ValueError:
            pass  # surfaced as a validation error when the run is started
    geography = project.get("target_geography") or []
    if isinstance(geography, list):
        geography = ", ".join(geography)
    return {
        "client_name": project.get("client_name"),
        "product_name": project.get("product_name"),
        "target_specialty": project.get("target_specialty"),
        "target_geography": geography,
        "icp_profile_id": icp_id,
        "icp_name": icp_name,
        "icp_version": icp_version,
    }


def _parse_project_form(form: dict, created_by: str | None = None) -> dict:
    """Parse create/edit-project form strings into a typed project_data dict.

    Lists are comma-separated; blank numeric fields are omitted so the service
    applies its generic defaults. created_by is omitted when None (edit path
    preserves the original creator stored in the existing project config).
    """
    def _csv_list(value: str) -> list[str]:
        return [item.strip() for item in (value or "").replace("\n", ",").split(",") if item.strip()]

    def _opt_int(value: str, field: str) -> int | None:
        value = (value or "").strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{field} must be a whole number.")

    data: dict = {
        "project_id": (form.get("project_id") or "").strip(),
        "client_name": (form.get("client_name") or "").strip(),
        "client_website": (form.get("client_website") or "").strip(),
        "product_name": (form.get("product_name") or "").strip(),
        "target_specialty": (form.get("target_specialty") or "").strip(),
        "target_geography": _csv_list(form.get("target_geography")),
        "icp_profile_id": (form.get("icp_profile_id") or "").strip(),
        "notes": (form.get("notes") or "").strip(),
    }
    if created_by is not None:
        data["created_by"] = created_by
    if form.get("active_exclusion_rules", "").strip():
        data["active_exclusion_rules"] = _csv_list(form.get("active_exclusion_rules"))
    if form.get("subpage_keywords", "").strip():
        data["subpage_keywords"] = _csv_list(form.get("subpage_keywords"))
    for field in (
        "bullseye_min_score", "max_pages_per_practice", "request_timeout_seconds",
        "request_retries", "io_concurrency",
    ):
        parsed = _opt_int(form.get(field), field)
        if parsed is not None:
            data[field] = parsed
    return data


def _project_to_form(project: dict) -> dict:
    """Flatten a project config dict to form-ready strings for the edit template.

    Converts list fields to comma-separated strings and numeric fields to str
    so Jinja2 can populate input values directly.
    """
    def _join(v) -> str:
        return ", ".join(v) if isinstance(v, list) else (v or "")

    return {
        "project_id": project.get("project_id", ""),
        "client_name": project.get("client_name", ""),
        "client_website": project.get("client_website", ""),
        "product_name": project.get("product_name", ""),
        "target_specialty": project.get("target_specialty", ""),
        "target_geography": _join(project.get("target_geography")),
        "icp_profile_id": project.get("icp_profile_id", ""),
        "active_exclusion_rules": _join(project.get("active_exclusion_rules")),
        "subpage_keywords": _join(project.get("subpage_keywords")),
        "bullseye_min_score": str(project.get("bullseye_min_score", "")),
        "max_pages_per_practice": str(project.get("max_pages_per_practice", "")),
        "request_timeout_seconds": str(project.get("request_timeout_seconds", "")),
        "request_retries": str(project.get("request_retries", "")),
        "io_concurrency": str(project.get("io_concurrency", "")),
        "notes": project.get("notes", ""),
    }


_ERROR_PATTERNS: list[tuple] = [
    ("enriched_targets.json was not written",
     "Run ended before results were written. Try re-running."),
    ("run_log.json was not written",
     "Run ended before the output summary was written."),
    ("malformed json",
     "Pipeline output file was corrupted. Try re-running."),
    ("unicodeencodeerror", "Character encoding error — check that the input CSV has no unusual characters."),
    ("charmap", "Character encoding error — check that the input CSV has no unusual characters."),
    ("codec can't encode", "Character encoding error — check that the input CSV has no unusual characters."),
    ("no module named",
     "Pipeline environment error: a required package is missing. Contact support."),
    ("syntaxerror",
     "Pipeline code error. Contact support."),
    ("interrupted by server restart",
     "The server was restarted while this run was in progress."),
]

# These messages are already operator-readable; pass through unchanged.
_PASS_THROUGH_PATTERNS = ("missing required columns", "too many runs in progress")


def _friendly_error(raw: str | None) -> str | None:
    """Translate a raw pipeline error_summary into an operator-readable message.

    Returns None when raw is empty. Matches are case-insensitive.
    Falls back to the first 300 chars of the raw text when no pattern matches.
    """
    if not raw:
        return None
    lower = raw.lower()
    for prefix in _PASS_THROUGH_PATTERNS:
        if prefix in lower:
            return raw[:300]
    for pattern, message in _ERROR_PATTERNS:
        if pattern in lower:
            return message
    return raw[:300]


def _pending_review_count(run_id: str, run_directory: Path) -> int:
    """Count records in a completed run that still have qc_status='pending'."""
    results_path = run_directory / "enriched_targets.json"
    if not results_path.exists():
        return 0
    with open(results_path, "r", encoding="utf-8") as f:
        all_records = record_adapter.normalize_records_payload(json.load(f))
    all_reviews = reviews.get_reviews(run_id, run_directory)
    return sum(
        1 for r in all_records
        if all_reviews.get(record_adapter.get_record_id(r), {}).get("qc_status", "pending") == "pending"
    )


def _compute_readiness(merged_records: list) -> dict:
    """Compute client package readiness from the merged records list.

    Returns a dict with keys: state ('needs_review'|'no_approved'|'ready'),
    pending_count, approved_count.
    """
    pending = sum(
        1 for r in merged_records
        if r.get("review", {}).get("qc_status", "pending") == "pending"
    )
    approved = sum(
        1 for r in merged_records
        if r.get("review", {}).get("qc_status") == "approved"
        and r.get("displayed_tier", "").lower() != "excluded"
    )
    if pending > 0:
        return {"state": "needs_review", "pending_count": pending, "approved_count": approved}
    if approved == 0:
        return {"state": "no_approved", "pending_count": 0, "approved_count": 0}
    return {"state": "ready", "pending_count": 0, "approved_count": approved}


class _TagStripper(HTMLParser):
    """Strip HTML tags and return readable text, skipping chrome elements."""

    _SKIP_TAGS = frozenset({"script", "style", "nav", "footer", "head", "noscript", "svg"})

    def __init__(self) -> None:
        super().__init__()
        self._skip = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP_TAGS:
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            text = data.strip()
            if text:
                self._parts.append(text)

    def get_text(self) -> str:
        """Return all extracted text joined by spaces."""
        return " ".join(self._parts)


def _parse_signals_from_form(form_data: dict, signal_count: int) -> list[dict]:
    """Extract and normalize signal rows from a review form submission."""
    signals = []
    for i in range(signal_count):
        sig: dict = {
            "signal_id": (form_data.get(f"signal_id_{i}") or "").strip(),
            "signal_label": (form_data.get(f"signal_label_{i}") or "").strip(),
            "prompt_instruction": (form_data.get(f"prompt_instruction_{i}") or "").strip(),
            "positive_weight": int(form_data.get(f"positive_weight_{i}") or 0),
        }
        if form_data.get(f"required_for_bullseye_{i}") == "on":
            sig["required_for_bullseye"] = True
        raw_no_weight = (form_data.get(f"no_weight_{i}") or "").strip()
        if raw_no_weight:
            try:
                sig["no_weight"] = int(raw_no_weight)
            except ValueError:
                pass
        signals.append(sig)
    return signals


def _parse_hypothesis_from_form(form_data: dict) -> dict:
    """Extract hypothesis fields from a review form submission."""
    return {
        "ideal_practice_profile": (form_data.get("hyp_ideal_practice_profile") or "").strip(),
        "commercial_fit_reasoning": (form_data.get("hyp_commercial_fit_reasoning") or "").strip(),
        "fast_close_indicators": (form_data.get("hyp_fast_close_indicators") or "").strip(),
        "common_objections": (form_data.get("hyp_common_objections") or "").strip(),
    }


def _parse_demo_accounts(demo_accounts_json: str) -> list:
    """Parse demo_accounts_json from a form field, returning an empty list on failure."""
    try:
        accounts = json.loads(demo_accounts_json) if (demo_accounts_json or "").strip() else []
        return accounts if isinstance(accounts, list) else []
    except (ValueError, TypeError):
        return []


def _fetch_page_text(url: str, max_chars: int = 5000) -> str:
    """Fetch a URL and return stripped plain text, truncated to max_chars.

    Uses stdlib only (urllib + html.parser). Returns empty string on any error.
    """
    if not url:
        return ""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; BEMI-ICP-Builder/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            raw_bytes = resp.read(300_000)
        raw_html = raw_bytes.decode("utf-8", errors="replace")
        parser = _TagStripper()
        parser.feed(raw_html)
        return parser.get_text()[:max_chars]
    except Exception as exc:
        logger.debug("URL fetch failed for %s: %s", url, exc)
        return ""


def _build_demo_brief_pdf(profile: dict) -> bytes:
    """Render the PDF-specific demo brief template and convert via WeasyPrint.

    Falls back to a minimal stub on failure so the download never 500s.
    """
    import weasyprint
    try:
        rendered = _jinja_env.get_template("icp_demo_brief_pdf.html").render(profile=profile)
        return weasyprint.HTML(string=rendered).write_pdf()
    except Exception as exc:
        logger.error("Demo brief PDF generation failed: %s", exc)
        return b"%PDF-1.4\n%%EOF\n"


def _calculate_stats(records: list[dict]) -> dict:
    """Count records by displayed tier and QC status for the results header."""
    stats = {
        "total": len(records),
        "bullseye": 0,
        "needs_verification": 0,
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
        tier = (r.get("displayed_tier") or "").lower().replace(" ", "_")
        if tier in stats:
            stats[tier] += 1
        qc = (r.get("review") or {}).get("qc_status", "pending")
        if qc == "pending":
            stats["pending_review"] += 1
        elif qc in stats:
            stats[qc] += 1
    return stats
