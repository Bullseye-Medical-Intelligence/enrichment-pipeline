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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from jinja2 import select_autoescape as _jinja_autoescape

import runs
from config import HOST, PORT

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_error_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=_jinja_autoescape(["html"]),
)

# Content-Security-Policy for the operator UI.
# Templates use inline <script> blocks throughout; 'unsafe-inline' is required.
# The operator dashboard is not a public-facing surface — restricting frame
# embedding, MIME sniffing, and form actions still provides meaningful defence.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def _render_error(code: int, message: str) -> str:
    """Render error.html with the given HTTP status code and message."""
    return _error_env.get_template("error.html").render(code=code, message=message)


def _wants_html(request: Request) -> bool:
    """Return True if the client prefers an HTML response (browser requests)."""
    return "text/html" in request.headers.get("accept", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

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


def _seed_version(path: Path) -> str:
    """Return the 'version' string of an ICP profile JSON, or '' if unreadable."""
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("version", ""))
    except Exception:
        return ""


def _seed_icp_profiles() -> None:
    """Upsert bundled seed ICP profiles into ICP_PROFILES_PATH on startup.

    Per-file, version-aware: a seed is copied when its destination is missing or
    when the bundled seed's `version` differs from the installed copy's. This lets
    a corrected seed (e.g. one with a redundant inverse signal removed) propagate
    to deployments by bumping its version, without the all-or-nothing "only when
    empty" gate that left stale seeds in place.

    Only filenames present in the seed bundle are ever touched. Operator-authored
    and imported profiles use their own icp_id and are never overwritten — to
    customize a seeded profile, save it under a new icp_id rather than editing the
    managed seed in place.
    """
    import shutil
    from config import ICP_PROFILES_PATH

    seeds_dir = Path(__file__).parent / "seeds" / "icp_profiles"
    if not seeds_dir.exists():
        return
    ICP_PROFILES_PATH.mkdir(parents=True, exist_ok=True)
    for seed in seeds_dir.glob("*.json"):
        dest = ICP_PROFILES_PATH / seed.name
        if dest.exists() and _seed_version(dest) == _seed_version(seed):
            continue
        action = "Updated" if dest.exists() else "Seeded"
        shutil.copy2(seed, dest)
        logger.info("%s ICP profile from seed: %s", action, seed.name)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """On startup, fail any run orphaned by a prior server instance."""
    count = runs.reconcile_orphaned_runs()
    if count:
        logger.warning("Reconciled %d orphaned run(s) on startup", count)
    _seed_icp_profiles()
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


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Attach security and CSP headers to every response."""
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "0"
    return response


# Register UI routes (login, dashboard, upload, results)
from ui import router as ui_router  # noqa: E402 — after app creation
app.include_router(ui_router)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Return a styled HTML error page for browser requests, JSON for API clients.

    Redirects (3xx) are passed through as RedirectResponse to preserve Location.
    """
    if 300 <= exc.status_code < 400:
        location = (exc.headers or {}).get("Location") or "/"
        return RedirectResponse(url=location, status_code=exc.status_code)
    if _wants_html(request):
        return HTMLResponse(_render_error(exc.status_code, exc.detail), status_code=exc.status_code)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
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
    message = f"Internal server error. Run ID: {run_id}"
    if _wants_html(request):
        return HTMLResponse(_render_error(500, message), status_code=500)
    return JSONResponse(status_code=500, content={"detail": message})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
