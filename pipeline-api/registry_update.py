"""
registry_update.py
Explicit, operator-triggered update of the master practice registry from an
enrichment run.

The registry (master_practice_registry.json) is the platform's cross-run memory.
It is NEVER mutated automatically — not during discovery, not during the
discovery→enrichment handoff, and not when an enrichment run completes. The only
way a record reaches the registry is this explicit action:

    POST /enrichment-runs/{run_id}/update-registry

Matching uses the same priority as discovery (google_place_id → website domain →
phone → name+address; NPI is a supporting identifier only, never a match key).
Normalization and matching helpers are imported from `practice_matching.py` —
the single API-side source of truth for all normalization + match logic.
See `pipeline-api/MATCHING_NOTES.md`.

Every update writes an auditable registry_update_log.json into the run folder and
stamps registry_updated_at / registry_update_count / registry_update_log_path onto
the run's status.json. Updates are idempotent: re-running does not duplicate
change_history.
"""

import json
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse

import auth
import record_adapter
import runs
# Matching/normalization is shared with discovery via practice_matching — the
# single source of truth (MATCHING_NOTES.md). These aliases keep the existing
# private call sites in this module unchanged.
from practice_matching import (
    normalize_domain as _normalize_domain,
    normalize_phone as _normalize_phone,
    normalize_name as _normalize_name,
    normalize_address as _normalize_address,
    name_address_key as _name_address_key,
    build_match_indexes as _build_indexes,
    match_with_ambiguity as match_entry,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class RegistryLoadError(Exception):
    """Raised when an existing registry file is present but cannot be read.

    A missing file is a valid bootstrap state (empty registry). A present-but-
    corrupt file is not — silently treating it as empty would wipe platform
    memory on the next write, so callers must abort instead.
    """

REGISTRY_VERSION = "1"
REGISTRY_FILENAME = "master_practice_registry.json"
REGISTRY_UPDATE_LOG_FILENAME = "registry_update_log.json"
ENRICHED_FILENAME = "enriched_targets.json"

# selection_mode → predicate over an enriched record (before include-flag gates).
SELECTION_MODES: frozenset[str] = frozenset({"bullseye_only", "clear_only", "all_reviewable"})

# Identity/contact fields whose change is "meaningful" and recorded in
# change_history. Score/tier fields change every run and are tracked as current
# values only, so idempotency stays meaningful.
HISTORY_FIELDS: tuple[str, ...] = (
    "practice_name", "website_url", "phone",
    "address_full", "address_city", "address_state", "address_zip", "specialty",
)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def registry_path() -> Path:
    """Return the platform-level registry path (sibling of the runs/ directory)."""
    return runs.OUTPUT_RUNS_PATH.parent / REGISTRY_FILENAME


# Normalization + name_address_key are imported from practice_matching (above)
# under their existing private names, so all call sites in this module are unchanged.


# ---------------------------------------------------------------------------
# Registry I/O (atomic)
# ---------------------------------------------------------------------------

def load_registry(path: Path) -> dict:
    """Load the registry from *path*.

    A missing file returns an empty registry (valid bootstrap). A present-but-
    unreadable file raises RegistryLoadError — never silently emptied, or the
    next write would erase platform memory.
    """
    if not path.exists():
        return _empty_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Registry %s exists but is unreadable: %s", path, exc)
        raise RegistryLoadError(
            f"Registry file at {path} exists but could not be read ({exc}). "
            "Update aborted to protect platform memory — no changes were written."
        ) from exc
    if not isinstance(data, dict):
        raise RegistryLoadError(
            f"Registry file at {path} is not a valid registry object. "
            "Update aborted to protect platform memory — no changes were written."
        )
    if not isinstance(data.get("entries"), dict):
        data["entries"] = {}
    return data


def _empty_registry() -> dict:
    return {"version": REGISTRY_VERSION, "updated_at": "", "entry_count": 0, "entries": {}}


def save_registry(registry: dict, path: Path) -> None:
    """Atomically write *registry* (tmp file + os.replace).

    On any failure the temp file is removed and the original registry is left
    untouched — a crash mid-write never corrupts platform memory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    registry["updated_at"] = datetime.now(timezone.utc).isoformat()
    registry["entry_count"] = len(registry.get("entries") or {})
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Indexing (_build_indexes) and matching (match_entry) are imported from
# practice_matching (above). _build_indexes = build_match_indexes;
# match_entry = match_with_ambiguity. See MATCHING_NOTES.md.


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def _fields_from_record(rec: dict) -> dict:
    """Pull and normalize registry-relevant fields from an enriched record."""
    name = rec.get("practice_name") or ""
    url = rec.get("website_url") or ""
    phone = rec.get("phone") or ""
    city = rec.get("address_city") or ""
    state = rec.get("address_state") or ""
    zip_ = rec.get("address_zip") or ""
    # Enriched output parses the address into parts; compose a display full address.
    full = ", ".join(p for p in (city, state) if p)
    if zip_:
        full = f"{full} {zip_}".strip()
    return {
        # Enriched output carries google_place_id end-to-end (mapped on ingest,
        # defaulted by the scorer). Read here as the priority-1 registry match key.
        "google_place_id": rec.get("google_place_id") or "",
        "practice_name": name,
        "name_normalized": _normalize_name(name),
        "website_url": url,
        "website_domain": _normalize_domain(url),
        "phone": phone,
        "phone_digits": _normalize_phone(phone),
        "address_full": full,
        "address_city": city,
        "address_state": state,
        "address_zip": zip_,
        "address_normalized": _normalize_address("", city, state, zip_),
        "specialty": rec.get("specialty") or "",
        "npi": rec.get("npi_optional") or rec.get("npi") or "",
    }


def _has_min_identity(fields: dict) -> bool:
    """A record is registrable only with a name plus at least one match key.

    google_place_id is the priority-1 match key (practice_matching) and is
    treated as sufficient identity by discovery, so a name + place_id record is
    registrable on its own — consistent with how it is matched downstream.
    """
    if not fields["practice_name"]:
        return False
    return bool(
        fields.get("google_place_id")
        or fields["website_domain"]
        or len(fields["phone_digits"]) >= 10
        or (fields["name_normalized"] and fields["address_normalized"])
    )


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _rejection_reason(
    rec: dict,
    fields: dict,
    *,
    include_needs_review: bool,
    include_excluded: bool,
) -> Optional[str]:
    """Return a rejection reason for a record, or None if it is eligible.

    Rules applied to every candidate (explicit or mode-selected): failed records
    are always rejected; EXCLUDED needs include_excluded; needs_review needs
    include_needs_review; records missing identity are always rejected.
    """
    enrichment_status = rec.get("enrichment_status") or ""
    if enrichment_status == "failed":
        return "enrichment_status is 'failed'"
    if (rec.get("exclusion_status") or "") == "EXCLUDED" and not include_excluded:
        return "record is EXCLUDED (set include_excluded to override)"
    if enrichment_status == "needs_review" and not include_needs_review:
        return "record is needs_review (set include_needs_review to override)"
    if not _has_min_identity(fields):
        return "missing minimum identity fields (name + a match key)"
    return None


def _mode_allows(rec: dict, mode: str) -> bool:
    """Return True if a record qualifies for the given selection_mode."""
    clear = (rec.get("exclusion_status") or "") == "CLEAR"
    if mode == "bullseye_only":
        return clear and (rec.get("target_tier") or "") == "Bullseye"
    if mode == "clear_only":
        return clear
    if mode == "all_reviewable":
        return clear and (rec.get("enrichment_status") or "") not in ("failed", "needs_review")
    return False


# ---------------------------------------------------------------------------
# Registry record build / merge
# ---------------------------------------------------------------------------

def _new_entry(
    registry_id: str,
    fields: dict,
    rec: dict,
    run_id: str,
    now: str,
    discovery_run_id: str,
    evidence_path: str,
) -> dict:
    """Construct a fresh registry entry from an enriched record."""
    return {
        "practice_registry_id": registry_id,
        "google_place_id": fields["google_place_id"],
        "website_domain": fields["website_domain"],
        "phone_digits": fields["phone_digits"],
        "name_normalized": fields["name_normalized"],
        "address_normalized": fields["address_normalized"],
        "npi": fields["npi"],
        "practice_name": fields["practice_name"],
        "website_url": fields["website_url"],
        "phone": fields["phone"],
        "address_full": fields["address_full"],
        "address_city": fields["address_city"],
        "address_state": fields["address_state"],
        "address_zip": fields["address_zip"],
        "specialty": fields["specialty"],
        "first_seen_at": now,
        "last_seen_at": now,
        "first_discovery_run_id": discovery_run_id,
        "last_discovery_run_id": discovery_run_id,
        "last_enrichment_run_id": run_id,
        "last_reviewed_at": now,
        "current_tier": rec.get("target_tier") or "",
        "bullseye_score": rec.get("bullseye_score") or 0,
        "exclusion_status": rec.get("exclusion_status") or "",
        "enrichment_status": rec.get("enrichment_status") or "",
        "source_pipeline_version": rec.get("source_pipeline_version") or "",
        "evidence_path": evidence_path,
        "change_history": [],
    }


def _apply_update(
    entry: dict,
    fields: dict,
    rec: dict,
    run_id: str,
    now: str,
    discovery_run_id: str,
    evidence_path: str,
) -> list[dict]:
    """Merge an enriched record into an existing entry. Return appended history.

    A change_history entry is appended only for HISTORY_FIELDS whose current
    value actually changed — so an identical re-run appends nothing (idempotent).
    """
    appended: list[dict] = []
    incoming = {
        "practice_name": fields["practice_name"],
        "website_url": fields["website_url"],
        "phone": fields["phone"],
        "address_full": fields["address_full"],
        "address_city": fields["address_city"],
        "address_state": fields["address_state"],
        "address_zip": fields["address_zip"],
        "specialty": fields["specialty"],
    }
    for field in HISTORY_FIELDS:
        new_val = incoming.get(field, "")
        old_val = entry.get(field, "")
        # Only record a change when there is a real new value that differs.
        if new_val and new_val != old_val:
            change = {
                "field": field, "old": old_val, "new": new_val,
                "changed_at": now, "enrichment_run_id": run_id,
            }
            entry.setdefault("change_history", []).append(change)
            appended.append(change)
            entry[field] = new_val

    # Refresh match keys + current (non-history) fields.
    if fields["website_domain"]:
        entry["website_domain"] = fields["website_domain"]
    if fields["phone_digits"]:
        entry["phone_digits"] = fields["phone_digits"]
    if fields["name_normalized"]:
        entry["name_normalized"] = fields["name_normalized"]
    if fields["address_normalized"]:
        entry["address_normalized"] = fields["address_normalized"]
    if fields["google_place_id"]:
        entry["google_place_id"] = fields["google_place_id"]
    if fields["npi"]:
        entry["npi"] = fields["npi"]
    entry["last_seen_at"] = now
    entry["last_enrichment_run_id"] = run_id
    entry["last_reviewed_at"] = now
    entry["current_tier"] = rec.get("target_tier") or entry.get("current_tier", "")
    entry["bullseye_score"] = rec.get("bullseye_score", entry.get("bullseye_score", 0))
    entry["exclusion_status"] = rec.get("exclusion_status") or entry.get("exclusion_status", "")
    entry["enrichment_status"] = rec.get("enrichment_status") or entry.get("enrichment_status", "")
    if rec.get("source_pipeline_version"):
        entry["source_pipeline_version"] = rec["source_pipeline_version"]
    if discovery_run_id:
        entry["last_discovery_run_id"] = discovery_run_id
        entry.setdefault("first_discovery_run_id", discovery_run_id)
    if evidence_path:
        entry["evidence_path"] = evidence_path
    entry.setdefault("practice_registry_id", entry.get("entry_id", ""))
    return appended


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _load_status_raw(run_id: str) -> Optional[dict]:
    """Read a run's status.json as a raw dict (no schema validation)."""
    if not runs.is_valid_run_id(run_id):
        return None
    path = runs.run_dir(run_id) / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _evidence_path(run_dir: Path, record_id: str) -> str:
    """Return the absolute evidence dir for a record if it exists, else ''."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", record_id or "")
    if not safe:
        return ""
    d = run_dir / "evidence" / safe
    return str(d) if d.is_dir() else ""


def update_registry_from_run(
    run_id: str,
    *,
    selected_record_ids: Optional[list] = None,
    selection_mode: Optional[str] = None,
    include_needs_review: bool = False,
    include_excluded: bool = False,
    require_source_discovery_run_id: bool = False,
) -> dict:
    """Apply an enrichment run's selected records to the master registry.

    Returns an auditable result dict. Raises LookupError (run/output missing),
    ValueError (bad selection input / run not eligible), or RegistryLoadError
    (existing registry file present but unreadable — update aborted, nothing written).
    """
    status_raw = _load_status_raw(run_id)
    if status_raw is None:
        raise LookupError(f"Run '{run_id}' not found.")
    if status_raw.get("run_type", "enrichment") != "enrichment":
        raise ValueError(f"Run '{run_id}' is not an enrichment run.")
    if status_raw.get("status") != "complete":
        raise ValueError(
            f"Run '{run_id}' has status '{status_raw.get('status')}', not complete."
        )

    discovery_run_id = status_raw.get("source_discovery_run_id") or ""
    if require_source_discovery_run_id and not discovery_run_id:
        raise ValueError(
            "require_source_discovery_run_id is set but this run has no "
            "source_discovery_run_id."
        )

    run_directory = runs.run_dir(run_id)
    enriched_path = run_directory / ENRICHED_FILENAME
    if not enriched_path.exists():
        raise LookupError(f"Run '{run_id}' has no enrichment output.")
    try:
        raw = json.loads(enriched_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise LookupError(f"Could not read enrichment output: {exc}") from exc
    records = record_adapter.normalize_records_payload(raw)

    chosen = _choose_records(records, selected_record_ids, selection_mode)

    now = datetime.now(timezone.utc).isoformat()
    registry = load_registry(registry_path())
    entries = registry.setdefault("entries", {})
    indexes = _build_indexes(entries)

    inserted: list[str] = []
    updated: list[dict] = []
    rejected: list[dict] = []
    needs_manual_merge: list[dict] = []

    for rec in chosen:
        rid = record_adapter.get_record_id(rec) or "(no id)"
        fields = _fields_from_record(rec)
        reason = _rejection_reason(
            rec, fields,
            include_needs_review=include_needs_review,
            include_excluded=include_excluded,
        )
        if reason:
            rejected.append({"record_id": rid, "reason": reason})
            continue

        entry_id, ambiguous = match_entry(fields, indexes)
        if ambiguous:
            needs_manual_merge.append({
                "record_id": rid,
                "reason": "matches multiple existing registry entries",
            })
            continue

        evidence = _evidence_path(run_directory, rid)
        if entry_id is None:
            new_id = uuid.uuid4().hex
            entries[new_id] = _new_entry(
                new_id, fields, rec, run_id, now, discovery_run_id, evidence)
            _index_entry(indexes, new_id, entries[new_id])
            inserted.append(rid)
        else:
            changes = _apply_update(
                entries[entry_id], fields, rec, run_id, now, discovery_run_id, evidence)
            updated.append({"record_id": rid, "changes": changes})

    update_count = len(inserted) + len(updated)
    if update_count:
        save_registry(registry, registry_path())

    log = {
        "run_id": run_id,
        "updated_at": now,
        "selection_mode": selection_mode or "",
        "selected_record_ids": selected_record_ids or [],
        "include_needs_review": include_needs_review,
        "include_excluded": include_excluded,
        "registry_update_count": update_count,
        "inserted": inserted,
        "updated": updated,
        "rejected": rejected,
        "needs_manual_merge": needs_manual_merge,
    }
    log_path = run_directory / REGISTRY_UPDATE_LOG_FILENAME
    _write_json_atomic(log_path, log)

    runs.update_run_status(
        run_id,
        registry_updated_at=now,
        registry_update_count=update_count,
        registry_update_log_path=str(log_path),
    )

    return {
        "run_id": run_id,
        "registry_updated_at": now,
        "registry_update_count": update_count,
        "inserted_count": len(inserted),
        "updated_count": len(updated),
        "rejected": rejected,
        "needs_manual_merge": needs_manual_merge,
        "registry_update_log_path": str(log_path),
    }


def _choose_records(
    records: list[dict],
    selected_record_ids: Optional[list],
    selection_mode: Optional[str],
) -> list[dict]:
    """Resolve the candidate record set from explicit ids or a selection_mode.

    Eligibility (rejection rules) is applied later; this only resolves *which*
    records the operator asked to consider. Raises ValueError on bad input.
    """
    has_ids = selected_record_ids is not None and len(selected_record_ids) > 0
    has_mode = bool(selection_mode)
    if has_ids and has_mode:
        raise ValueError("Provide either selected_record_ids or selection_mode, not both.")
    if not has_ids and not has_mode:
        raise ValueError("Provide selected_record_ids or selection_mode.")

    if has_ids:
        by_id = {record_adapter.get_record_id(r): r for r in records}
        chosen = []
        for rid in selected_record_ids:
            rec = by_id.get(rid)
            if rec is None:
                raise ValueError(f"Record id {rid!r} is not in this run's output.")
            chosen.append(rec)
        return chosen

    if selection_mode not in SELECTION_MODES:
        raise ValueError(
            f"Unknown selection_mode {selection_mode!r}. Allowed: {sorted(SELECTION_MODES)}."
        )
    return [r for r in records if _mode_allows(r, selection_mode)]


def _index_entry(indexes: dict, entry_id: str, entry: dict) -> None:
    """Register a freshly inserted entry in the in-memory indexes."""
    if entry["google_place_id"]:
        indexes["place_id"][entry["google_place_id"]] = entry_id
    if entry["website_domain"]:
        indexes["domain"][entry["website_domain"]] = entry_id
    if entry["phone_digits"]:
        indexes["phone"][entry["phone_digits"]] = entry_id
    na = _name_address_key(entry["name_normalized"], entry["address_normalized"])
    if na:
        indexes["name_address"][na] = entry_id


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write a JSON file atomically (tmp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post("/enrichment-runs/{run_id}/update-registry")
async def update_registry_route(
    run_id: str,
    payload: dict,
    background_tasks: BackgroundTasks,
    username: str = Depends(auth.require_session),
):
    """Push selected enrichment records into the master practice registry.

    Body: {selected_record_ids? | selection_mode?, include_needs_review?,
    include_excluded?, require_source_discovery_run_id?}.
    """
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"detail": "JSON body required."})
    try:
        result = await _run_update(run_id, payload)
    except LookupError as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})
    except RegistryLoadError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return JSONResponse(status_code=200, content=result)


async def _run_update(run_id: str, payload: dict) -> dict:
    """Run the (blocking) registry update in the threadpool."""
    from fastapi.concurrency import run_in_threadpool
    return await run_in_threadpool(
        update_registry_from_run,
        run_id,
        selected_record_ids=payload.get("selected_record_ids"),
        selection_mode=payload.get("selection_mode"),
        include_needs_review=bool(payload.get("include_needs_review", False)),
        include_excluded=bool(payload.get("include_excluded", False)),
        require_source_discovery_run_id=bool(
            payload.get("require_source_discovery_run_id", False)
        ),
    )
