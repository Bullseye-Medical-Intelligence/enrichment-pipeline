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

Selected NEW/CHANGED records from a completed discovery run can be handed off to
a normal enrichment run via POST /discovery-runs/{run_id}/send-to-enrichment.
The handoff writes an enrichment_handoff.csv into the discovery run folder and
creates the enrichment run through the existing runner — discovery never imports
or reimplements enrichment, and the registry is never mutated by the handoff.

Routes:
    POST /discovery-runs               — create + run a discovery comparison
    GET  /discovery-runs/{run_id}      — status + summary
    GET  /discovery-runs/{run_id}/results — full classified record list
    POST /discovery-runs/{run_id}/send-to-enrichment — hand off selected records
"""

import csv
import io
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

import auth
import config
import runner
import runs
import validator
from schema import DiscoveryRunSummary

logger = logging.getLogger(__name__)

router = APIRouter()

# Discovery classifications. NEW and CHANGED are actionable (worth enriching);
# POSSIBLE_DUPLICATE is actionable only via explicit record selection; KNOWN and
# INSUFFICIENT_DATA are never sent to enrichment.
NEW = "NEW"
CHANGED = "CHANGED"
POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
KNOWN = "KNOWN"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

DEFAULT_ACTIONABLE: frozenset[str] = frozenset({NEW, CHANGED})

# selection_mode → the classifications it selects. POSSIBLE_DUPLICATE is never
# included by a mode; it requires explicit record IDs.
SELECTION_MODES: dict[str, frozenset[str]] = {
    "new_only": frozenset({NEW}),
    "new_and_changed": frozenset({NEW, CHANGED}),
    "all_actionable": frozenset({NEW, CHANGED}),
}

HANDOFF_CSV_FILENAME = "enrichment_handoff.csv"

# Handoff CSV columns. Outscraper-compatible names first (so the existing
# enrichment ingestion maps them with no change), then discovery traceability
# columns (ignored by ingestion, preserved for audit).
HANDOFF_FIELDNAMES: list[str] = [
    "name", "site", "phone", "full_address", "city", "state", "postal_code",
    "type", "place_id", "npi",
    "discovery_run_id", "discovery_status", "discovery_reason",
    "matched_existing_record_id", "changed_fields",
]

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
# Send selected discovery records to enrichment
# ---------------------------------------------------------------------------

def select_records(
    records: list[dict],
    selected_record_ids: Optional[list],
    selection_mode: Optional[str],
) -> list[dict]:
    """Choose discovery records to enrich, enforcing the actionable-status rules.

    Exactly one of selected_record_ids / selection_mode must be provided. Explicit
    IDs may additionally include POSSIBLE_DUPLICATE; a selection_mode never does.
    KNOWN and INSUFFICIENT_DATA are always rejected. Raises ValueError with a
    clear message on any rule violation or empty selection.
    """
    has_ids = selected_record_ids is not None and len(selected_record_ids) > 0
    has_mode = bool(selection_mode)
    if has_ids and has_mode:
        raise ValueError(
            "Provide either selected_record_ids or selection_mode, not both."
        )
    if not has_ids and not has_mode:
        raise ValueError(
            "Provide selected_record_ids or selection_mode to choose records."
        )

    by_idx = {rec.get("row_idx"): rec for rec in records}

    if has_ids:
        chosen: list[dict] = []
        for rid in selected_record_ids:
            rec = by_idx.get(rid)
            if rec is None:
                raise ValueError(f"Record id {rid!r} is not in this discovery run.")
            status = rec.get("classification")
            if status in (KNOWN, INSUFFICIENT_DATA):
                raise ValueError(
                    f"Record {rid!r} is {status} and cannot be sent to enrichment. "
                    f"Only NEW, CHANGED, or explicitly-selected POSSIBLE_DUPLICATE "
                    f"records are actionable."
                )
            chosen.append(rec)
        if not chosen:
            raise ValueError("No actionable records selected.")
        return chosen

    if selection_mode not in SELECTION_MODES:
        raise ValueError(
            f"Unknown selection_mode {selection_mode!r}. "
            f"Allowed: {sorted(SELECTION_MODES)}."
        )
    wanted = SELECTION_MODES[selection_mode]
    chosen = [r for r in records if r.get("classification") in wanted]
    if not chosen:
        raise ValueError(
            f"No actionable records matched selection_mode '{selection_mode}'."
        )
    return chosen


def _discovery_reason(rec: dict) -> str:
    """Build a human-readable reason string for why a record is actionable."""
    status = rec.get("classification")
    basis = rec.get("match_basis") or ""
    if status == NEW:
        return "New practice — no registry match"
    if status == CHANGED:
        return f"Changed vs registry (matched by {basis})" if basis else "Changed vs registry"
    if status == POSSIBLE_DUPLICATE:
        dup = rec.get("duplicate_of_row_idx")
        return f"Possible duplicate of row {dup}" if dup is not None else "Possible duplicate"
    return status or ""


def _changed_fields_text(rec: dict) -> str:
    """Flatten a record's changed_fields list into a single summary string."""
    changes = rec.get("changed_fields") or []
    return "; ".join(
        f"{c.get('label', c.get('field', '?'))}: {c.get('old', '')!r} → {c.get('new', '')!r}"
        for c in changes
    )


def build_handoff_csv(records: list[dict], discovery_run_id: str) -> bytes:
    """Build an Outscraper-compatible CSV (plus traceability columns) for enrichment."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HANDOFF_FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        writer.writerow({
            "name": rec.get("practice_name", ""),
            "site": rec.get("website_url", ""),
            "phone": rec.get("phone", ""),
            "full_address": rec.get("address_full", ""),
            "city": rec.get("address_city", ""),
            "state": rec.get("address_state", ""),
            "postal_code": rec.get("address_zip", ""),
            "type": rec.get("google_category", ""),
            "place_id": rec.get("google_place_id", ""),
            "npi": rec.get("npi", ""),
            "discovery_run_id": discovery_run_id,
            "discovery_status": rec.get("classification", ""),
            "discovery_reason": _discovery_reason(rec),
            "matched_existing_record_id": rec.get("entry_id", "") or "",
            "changed_fields": _changed_fields_text(rec),
        })
    return buf.getvalue().encode("utf-8")


class _MemFile:
    """In-memory upload object duck-typed for the enrichment runner (.filename, .read())."""

    def __init__(self, content: bytes, filename: str):
        self._content = content
        self.filename = filename

    async def read(self) -> bytes:
        return self._content


async def send_to_enrichment(
    discovery_run_id: str,
    project_id: str,
    operator: str,
    background_tasks,
    selected_record_ids: Optional[list] = None,
    selection_mode: Optional[str] = None,
) -> dict:
    """Hand off selected discovery records to a new enrichment run.

    Validates the discovery run, selects actionable records, writes the handoff
    CSV into the discovery run folder, creates the enrichment run via the existing
    runner, and stamps discovery → enrichment traceability onto the new run's
    status.json. The registry is never touched. Raises ValueError on bad input.
    """
    summary = get_discovery_summary(discovery_run_id)
    if summary is None:
        raise LookupError(f"Discovery run '{discovery_run_id}' not found.")
    if summary.status != "complete":
        raise ValueError(
            f"Discovery run '{discovery_run_id}' is '{summary.status}', not complete."
        )

    results = read_discovery_results(discovery_run_id)
    if results is None:
        raise ValueError("Discovery results are not available for this run.")

    records = results.get("records") or []
    chosen = select_records(records, selected_record_ids, selection_mode)

    handoff_bytes = build_handoff_csv(chosen, discovery_run_id)
    handoff_path = runs.run_dir(discovery_run_id) / HANDOFF_CSV_FILENAME
    handoff_path.write_bytes(handoff_bytes)

    # Create the enrichment run through the existing runner — no enrichment
    # internals are imported, and the registry is not pre-registered (that would
    # mutate it, which this handoff must not do).
    mem_file = _MemFile(handoff_bytes, HANDOFF_CSV_FILENAME)
    enrichment_run_id, _row_count = await runner.orchestrate_ingest(
        mem_file, "outscraper", project_id, operator, background_tasks
    )

    # Stamp traceability + run_type onto the enrichment run's status.json.
    enrichment_status = runs.update_run_status(
        enrichment_run_id,
        run_type="enrichment",
        source_discovery_run_id=discovery_run_id,
        source_discovery_selection_count=len(chosen),
        source_discovery_selection_mode=selection_mode or "",
    )

    return {
        "enrichment_run_id": enrichment_run_id,
        "discovery_run_id": discovery_run_id,
        "selected_count": len(chosen),
        "handoff_csv_path": str(handoff_path),
        "status": enrichment_status.status,
    }


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


@router.post("/discovery-runs/{run_id}/send-to-enrichment")
async def send_to_enrichment_route(
    run_id: str,
    payload: dict,
    background_tasks: BackgroundTasks,
    username: str = Depends(auth.require_session),
):
    """Create an enrichment run from selected NEW/CHANGED discovery records.

    Body: {project_id, selected_record_ids?, selection_mode?}. Exactly one of
    selected_record_ids / selection_mode must be supplied.
    """
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"detail": "JSON body required."})
    project_id = (payload.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(status_code=400, content={"detail": "project_id is required."})

    try:
        result = await send_to_enrichment(
            discovery_run_id=run_id,
            project_id=project_id,
            operator=username,
            background_tasks=background_tasks,
            selected_record_ids=payload.get("selected_record_ids"),
            selection_mode=payload.get("selection_mode"),
        )
    except LookupError as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return JSONResponse(status_code=201, content=result)
