"""
discovery.py
Market Radar / Discovery: delta detection against the master practice registry.

LEGACY / NON-RUNTIME MODULE — nothing imports this at runtime anymore. The
persistent discovery flow (the one wired into the API and UI) lives in
`discovery_runs.py` (which drives the repo-root `discovery` package via
`discovery_cli.py`). This file is retained only because:
  - tests/test_matching_parity.py loads it by path to assert its normalization
    and match priority stay identical to registry_update.py (see MATCHING_NOTES.md);
  - deleting/refactoring it is out of scope for the current work.

Do NOT add new runtime behavior here. New discovery/registry behavior belongs in
`discovery_runs.py` / `registry_update.py`. If the matching helpers below change,
update the parity test and the other copies per MATCHING_NOTES.md.

Compares an uploaded Outscraper CSV against master_practice_registry.json to
classify each row as new, changed, or known. Selected rows flow into the existing
enrichment subprocess path unchanged.

Matching priority (highest to lowest):
  1. google_place_id   — Google's stable listing anchor
  2. website_domain    — normalized domain (strips www, scheme, trailing slashes)
  3. phone_digits      — last 10 digits of the phone number
  4. name_normalized + address_normalized — composite fallback
  NPI is preserved as a supplemental identifier but never used as a match key.
"""

import csv
import io
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import OUTPUT_RUNS_PATH
# Matching/normalization is shared with registry_update via practice_matching —
# the single source of truth (MATCHING_NOTES.md). Imported under the existing
# private names so the call sites below are unchanged.
import practice_matching
from practice_matching import (
    normalize_domain as _normalize_domain,
    normalize_phone as _normalize_phone,
    normalize_name as _normalize_name,
    normalize_address as _normalize_address,
    name_address_key as _name_address_key,
    build_match_indexes as _build_indexes,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Registry lives alongside the runs/ directory (a sibling of runs/)
REGISTRY_PATH: Path = OUTPUT_RUNS_PATH.parent / "master_practice_registry.json"
DISCOVERY_TEMP_PATH: Path = OUTPUT_RUNS_PATH.parent / "discovery"

_REGISTRY_VERSION = "1"

# ---------------------------------------------------------------------------
# Outscraper column names (mirrors outscraper_adapter.py conventions)
# ---------------------------------------------------------------------------

_URL_COLS = ("site", "website", "website_url", "url", "web", "web_url", "website_address")
_PLACE_ID_COLS = ("place_id",)
_PHONE_COLS = ("phone",)
_NAME_COLS = ("name",)
_ADDRESS_COLS = ("full_address", "address")
_CITY_COLS = ("city", "locality", "address_city", "city_name")
_STATE_COLS = ("state", "region", "address_state", "state_code", "state_name")
_ZIP_COLS = ("postal_code", "zip", "zip_code")
_CATEGORY_COLS = ("type", "category", "business_type")
_NPI_COLS = ("npi",)

# Human-readable labels for change-detection fields shown in the delta UI
CHANGE_LABELS: dict[str, str] = {
    "website_domain": "Website",
    "phone_digits": "Phone",
    "practice_name": "Practice name",
    "address_normalized": "Address",
    "google_category": "Google category",
}


def _first(row: dict, cols: tuple[str, ...]) -> str:
    """Return first non-empty value across column names (keys already lowercased)."""
    for col in cols:
        val = (row.get(col) or "").strip()
        if val:
            return val
    return ""


# Normalization helpers (_normalize_*) are imported from practice_matching
# (above) under their existing private names. See MATCHING_NOTES.md.


# ---------------------------------------------------------------------------
# Registry I/O
# ---------------------------------------------------------------------------

def _empty_registry() -> dict:
    """Return a fresh, empty registry structure."""
    return {
        "version": _REGISTRY_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "entry_count": 0,
        "entries": {},
    }


def load_registry() -> dict:
    """Load master_practice_registry.json; return an empty registry if absent."""
    if not REGISTRY_PATH.exists():
        return _empty_registry()
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data.get("entries"), dict):
            data["entries"] = {}
        return data
    except Exception:
        logger.exception("Failed to load registry — returning empty")
        return _empty_registry()


def save_registry(registry: dict) -> None:
    """Atomically write the registry to disk."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    registry["updated_at"] = datetime.now(timezone.utc).isoformat()
    registry["entry_count"] = len(registry.get("entries") or {})
    tmp = str(REGISTRY_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REGISTRY_PATH)


# In-memory indexes (_build_indexes) and name_address_key are imported from
# practice_matching (above). See MATCHING_NOTES.md.


# ---------------------------------------------------------------------------
# Row field extraction from a raw Outscraper CSV row
# ---------------------------------------------------------------------------

def _extract_row_fields(row: dict) -> dict:
    """Pull and normalize every match-relevant field from a lowercased CSV row."""
    url_raw = _first(row, _URL_COLS)
    phone_raw = _first(row, _PHONE_COLS)
    name_raw = _first(row, _NAME_COLS)
    full_address = _first(row, _ADDRESS_COLS)
    city = _first(row, _CITY_COLS)
    state = _first(row, _STATE_COLS)
    zip_ = _first(row, _ZIP_COLS)
    return {
        "google_place_id": _first(row, _PLACE_ID_COLS),
        "practice_name": name_raw,
        "name_normalized": _normalize_name(name_raw),
        "website_url": url_raw,
        "website_domain": _normalize_domain(url_raw),
        "phone": phone_raw,
        "phone_digits": _normalize_phone(phone_raw),
        "address_normalized": _normalize_address(full_address, city, state, zip_),
        "google_category": _first(row, _CATEGORY_COLS),
        "npi": _first(row, _NPI_COLS),
        "address_city": city,
        "address_state": state,
        "address_zip": zip_,
    }


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def find_match(
    row_fields: dict,
    indexes: dict,
    entries: dict,
) -> tuple[Optional[str], Optional[str]]:
    """Return (entry_id, match_basis) for the best registry match, or (None, None).

    Legacy 3-arg shim → practice_matching.find_match (the shared source of truth).
    `entries` is unused (kept for signature compatibility with old callers).
    """
    return practice_matching.find_match(row_fields, indexes)


def _detect_changes(row_fields: dict, entry: dict) -> list[dict]:
    """Compare CSV row fields against a registry entry; return a list of change dicts."""
    changes = []
    for field in ("website_domain", "phone_digits", "practice_name",
                  "address_normalized", "google_category"):
        new_val = (row_fields.get(field) or "").strip()
        old_val = (entry.get(field) or "").strip()
        if new_val and old_val and new_val != old_val:
            changes.append({
                "field": field,
                "label": CHANGE_LABELS.get(field, field),
                "old": old_val,
                "new": new_val,
            })
    return changes


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------

def compute_delta(
    csv_bytes: bytes,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Classify each row in an Outscraper CSV as new, changed, or known.

    Returns (new_rows, changed_rows, known_rows). Each row dict retains all
    original CSV columns plus internal discovery annotations:
      _row_idx      — 0-based position in the CSV (used as the form checkbox value)
      _match_basis  — which identifier matched (or empty string)
      _entry_id     — matched registry entry_id (or empty string)
      _last_tier    — tier from the last enrichment run (known/changed only)
      _last_run_id  — run_id of the last enrichment (known/changed only)
      _last_seen_at — ISO timestamp of last enrichment (known/changed only)
      _changes      — list of {field, label, old, new} dicts (changed rows only)
    """
    registry = load_registry()
    entries = registry.get("entries") or {}
    indexes = _build_indexes(entries)

    text = csv_bytes.decode("utf-8-sig")
    raw_rows = list(csv.DictReader(io.StringIO(text)))

    new_rows: list[dict] = []
    changed_rows: list[dict] = []
    known_rows: list[dict] = []

    for idx, row in enumerate(raw_rows):
        lower_row = {k.lower(): v for k, v in row.items()}
        fields = _extract_row_fields(lower_row)
        entry_id, match_basis = find_match(fields, indexes, entries)

        annotated = dict(row)
        annotated["_row_idx"] = idx
        annotated["_match_basis"] = match_basis or ""
        annotated["_entry_id"] = entry_id or ""
        annotated["_last_tier"] = ""
        annotated["_last_run_id"] = ""
        annotated["_last_seen_at"] = ""
        annotated["_changes"] = []

        if entry_id is None:
            new_rows.append(annotated)
        else:
            entry = entries[entry_id]
            annotated["_last_tier"] = entry.get("last_tier") or ""
            annotated["_last_run_id"] = entry.get("last_seen_run_id") or ""
            annotated["_last_seen_at"] = entry.get("last_seen_at") or ""
            changes = _detect_changes(fields, entry)
            annotated["_changes"] = changes
            if changes:
                changed_rows.append(annotated)
            else:
                known_rows.append(annotated)

    return new_rows, changed_rows, known_rows


def display_fields(row: dict) -> dict:
    """Extract display-friendly values from an annotated discovery row."""
    lower = {k.lower(): v for k, v in row.items()}
    city = _first(lower, _CITY_COLS)
    state = _first(lower, _STATE_COLS)
    location = ", ".join(p for p in (city, state) if p)
    return {
        "name": _first(lower, _NAME_COLS),
        "location": location,
        "website": _first(lower, _URL_COLS),
        "phone": _first(lower, _PHONE_COLS),
        "category": _first(lower, _CATEGORY_COLS),
        "last_tier": row.get("_last_tier") or "",
        "last_run_id": row.get("_last_run_id") or "",
        "last_seen_at": (row.get("_last_seen_at") or "")[:10],  # date only
        "changes": row.get("_changes") or [],
        "match_basis": row.get("_match_basis") or "",
        "row_idx": row.get("_row_idx", ""),
    }


# ---------------------------------------------------------------------------
# Discovery temp CSV session
# ---------------------------------------------------------------------------

def save_discovery_csv(content: bytes) -> str:
    """Save an uploaded CSV to a session temp file; return a discovery_id."""
    DISCOVERY_TEMP_PATH.mkdir(parents=True, exist_ok=True)
    discovery_id = uuid.uuid4().hex
    (DISCOVERY_TEMP_PATH / f"{discovery_id}.csv").write_bytes(content)
    _cleanup_stale_discovery_csvs()
    return discovery_id


def read_discovery_csv(discovery_id: str) -> Optional[bytes]:
    """Return bytes for a saved discovery CSV, or None if expired or missing."""
    safe = re.sub(r"[^a-f0-9]", "", (discovery_id or ""))[:32]
    if not safe:
        return None
    path = DISCOVERY_TEMP_PATH / f"{safe}.csv"
    return path.read_bytes() if path.exists() else None


def parse_discovery_rows(csv_bytes: bytes) -> list[dict]:
    """Parse a discovery CSV back into a list of row dicts."""
    text = csv_bytes.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def build_enrich_csv(rows: list[dict]) -> bytes:
    """Build an Outscraper-compatible CSV from selected discovery rows (strips _annotations)."""
    if not rows:
        return b""
    fieldnames = [k for k in rows[0].keys() if not k.startswith("_")]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _cleanup_stale_discovery_csvs(max_age_hours: int = 24) -> None:
    """Delete temp CSVs older than max_age_hours (best-effort)."""
    import time
    cutoff = time.time() - max_age_hours * 3600
    try:
        for path in DISCOVERY_TEMP_PATH.glob("*.csv"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pre-registration (preserves google_place_id before enrichment discards it)
# ---------------------------------------------------------------------------

def preregister_discovery_rows(csv_rows: list[dict]) -> int:
    """Add new registry entries from raw Outscraper rows before an enrichment run.

    Called just before spawning the pipeline subprocess so that google_place_id
    (which the pipeline adapter discards) is persisted. Only creates entries for
    rows that don't already match an existing entry. Returns the count created.
    """
    registry = load_registry()
    entries = registry.setdefault("entries", {})
    indexes = _build_indexes(entries)
    now = datetime.now(timezone.utc).isoformat()
    created = 0

    for row in csv_rows:
        lower = {k.lower(): v for k, v in row.items()}
        fields = _extract_row_fields(lower)
        if find_match(fields, indexes, entries)[0] is not None:
            continue  # Already in registry

        new_id = uuid.uuid4().hex
        entries[new_id] = {
            "entry_id": new_id,
            "google_place_id": fields["google_place_id"],
            "website_domain": fields["website_domain"],
            "phone_digits": fields["phone_digits"],
            "name_normalized": fields["name_normalized"],
            "address_normalized": fields["address_normalized"],
            "practice_name": fields["practice_name"],
            "website_url": fields["website_url"],
            "phone": fields["phone"],
            "address_city": fields["address_city"],
            "address_state": fields["address_state"],
            "address_zip": fields["address_zip"],
            "google_category": fields["google_category"],
            "npi": fields["npi"],
            "first_seen_run_id": "",
            "first_seen_at": now,
            "last_seen_run_id": "",
            "last_seen_at": now,
            "last_tier": "",
            "last_score": 0,
            "runs_seen": [],
            "change_log": [],
        }
        # Keep indexes current for remaining rows in this batch
        e = entries[new_id]
        if e["google_place_id"]:
            indexes["place_id"][e["google_place_id"]] = new_id
        if e["website_domain"]:
            indexes["domain"][e["website_domain"]] = new_id
        if e["phone_digits"]:
            indexes["phone"][e["phone_digits"]] = new_id
        na = _name_address_key(e["name_normalized"], e["address_normalized"])
        if na:
            indexes["name_address"][na] = new_id
        created += 1

    if created:
        save_registry(registry)
    return created


# ---------------------------------------------------------------------------
# Post-run upsert (called from monitor_pipeline after each successful run)
# ---------------------------------------------------------------------------

def upsert_from_run(run_id: str, enriched_json_path: Path) -> int:
    """Update the registry from a completed run's enriched_targets.json.

    Matches records by domain / phone / name+address (place_id is absent from
    pipeline output). Creates new entries for practices not yet in the registry.
    Returns the number of entries upserted.
    """
    if not enriched_json_path.exists():
        return 0
    try:
        raw = json.loads(enriched_json_path.read_text(encoding="utf-8"))
        records = raw.get("records", raw) if isinstance(raw, dict) else raw
    except Exception:
        logger.exception("upsert_from_run: failed to load %s", enriched_json_path)
        return 0

    registry = load_registry()
    entries = registry.setdefault("entries", {})
    indexes = _build_indexes(entries)
    now = datetime.now(timezone.utc).isoformat()
    upserted = 0

    for record in records:
        if not isinstance(record, dict):
            continue

        fields = {
            "google_place_id": "",
            "practice_name": record.get("practice_name") or "",
            "name_normalized": _normalize_name(record.get("practice_name") or ""),
            "website_url": record.get("website_url") or "",
            "website_domain": _normalize_domain(record.get("website_url") or ""),
            "phone": record.get("phone") or "",
            "phone_digits": _normalize_phone(record.get("phone") or ""),
            "address_normalized": _normalize_address(
                "",
                record.get("address_city") or "",
                record.get("address_state") or "",
                record.get("address_zip") or "",
            ),
            "google_category": "",
            "npi": record.get("npi_optional") or "",
            "address_city": record.get("address_city") or "",
            "address_state": record.get("address_state") or "",
            "address_zip": record.get("address_zip") or "",
        }

        tier = record.get("target_tier") or ""
        score = record.get("bullseye_score") or 0
        entry_id, _ = find_match(fields, indexes, entries)

        if entry_id:
            entry = entries[entry_id]
            changes = _detect_changes(fields, entry)
            for ch in changes:
                ch["detected_at"] = now
                ch["detected_in_run_id"] = run_id
                entry.setdefault("change_log", []).append(ch)
            entry.update({
                "website_domain": fields["website_domain"] or entry.get("website_domain", ""),
                "phone_digits": fields["phone_digits"] or entry.get("phone_digits", ""),
                "practice_name": fields["practice_name"] or entry.get("practice_name", ""),
                "name_normalized": fields["name_normalized"] or entry.get("name_normalized", ""),
                "address_normalized": fields["address_normalized"] or entry.get("address_normalized", ""),
                "last_seen_run_id": run_id,
                "last_seen_at": now,
                "last_tier": tier,
                "last_score": score,
            })
            if run_id not in entry.get("runs_seen", []):
                entry.setdefault("runs_seen", []).append(run_id)
            if not entry.get("first_seen_run_id"):
                entry["first_seen_run_id"] = run_id
        else:
            new_id = uuid.uuid4().hex
            entries[new_id] = {
                "entry_id": new_id,
                "google_place_id": "",
                "website_domain": fields["website_domain"],
                "phone_digits": fields["phone_digits"],
                "name_normalized": fields["name_normalized"],
                "address_normalized": fields["address_normalized"],
                "practice_name": fields["practice_name"],
                "website_url": fields["website_url"],
                "phone": fields["phone"],
                "address_city": fields["address_city"],
                "address_state": fields["address_state"],
                "address_zip": fields["address_zip"],
                "google_category": "",
                "npi": fields["npi"],
                "first_seen_run_id": run_id,
                "first_seen_at": now,
                "last_seen_run_id": run_id,
                "last_seen_at": now,
                "last_tier": tier,
                "last_score": score,
                "runs_seen": [run_id],
                "change_log": [],
            }
            e = entries[new_id]
            if e["website_domain"]:
                indexes["domain"][e["website_domain"]] = new_id
            if e["phone_digits"]:
                indexes["phone"][e["phone_digits"]] = new_id
            na = _name_address_key(e["name_normalized"], e["address_normalized"])
            if na:
                indexes["name_address"][na] = new_id

        upserted += 1

    save_registry(registry)
    logger.info("upsert_from_run: upserted %d entries from run %s", upserted, run_id)
    return upserted
