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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import runs
from config import HOST, PORT

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
