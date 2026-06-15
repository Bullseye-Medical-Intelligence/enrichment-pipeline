"""
classifier.py
Row classification logic: NEW / CHANGED / KNOWN / POSSIBLE_DUPLICATE / INSUFFICIENT_DATA.

Classification order:
  1. INSUFFICIENT_DATA  — row lacks any reliable identifier
  2. KNOWN / CHANGED    — row matches an existing registry entry
  3. POSSIBLE_DUPLICATE — row matches another row already seen in this upload
  4. NEW                — no registry match and no intra-upload duplicate
"""

from typing import Optional
from discovery.matcher import find_match, name_address_key

# ---------------------------------------------------------------------------
# Classification constants
# ---------------------------------------------------------------------------

NEW = "NEW"
CHANGED = "CHANGED"
KNOWN = "KNOWN"
POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

ALL_CLASSIFICATIONS = (NEW, CHANGED, KNOWN, POSSIBLE_DUPLICATE, INSUFFICIENT_DATA)

# Fields compared against registry entries for change detection.
# Only fields present in both the CSV and the registry entry are compared.
CHANGE_FIELDS: tuple[str, ...] = (
    "website_domain",
    "phone_digits",
    "practice_name",
    "address_normalized",
    "google_category",
)

CHANGE_FIELD_LABELS: dict[str, str] = {
    "website_domain": "Website",
    "phone_digits": "Phone",
    "practice_name": "Practice name",
    "address_normalized": "Address",
    "google_category": "Google category",
}


# ---------------------------------------------------------------------------
# Data sufficiency gate
# ---------------------------------------------------------------------------

def has_sufficient_data(row_fields: dict) -> bool:
    """
    Return True when the row has at least one reliable identifier.

    Requires one of:
    - google_place_id (any non-empty string)
    - website_domain (any non-empty string)
    - 10-digit phone_digits
    - both name_normalized AND address_normalized non-empty
    """
    return bool(
        row_fields.get("google_place_id")
        or row_fields.get("website_domain")
        or (row_fields.get("phone_digits") and len(row_fields["phone_digits"]) >= 10)
        or (row_fields.get("name_normalized") and row_fields.get("address_normalized"))
    )


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def detect_changes(row_fields: dict, entry: dict) -> list[dict]:
    """
    Compare CSV row fields against a registry entry.

    Returns a list of change dicts — one per differing field.  A change is
    only recorded when both the old value (from registry) and new value (from
    CSV) are non-empty; a missing new value is not treated as a deletion.
    """
    changes = []
    for field in CHANGE_FIELDS:
        new_val = (row_fields.get(field) or "").strip()
        old_val = (entry.get(field) or "").strip()
        if new_val and old_val and new_val != old_val:
            changes.append({
                "field": field,
                "label": CHANGE_FIELD_LABELS.get(field, field),
                "old": old_val,
                "new": new_val,
            })
    return changes


# ---------------------------------------------------------------------------
# Intra-upload duplicate tracking
# ---------------------------------------------------------------------------

def _find_intra_upload_match(
    row_fields: dict,
    seen_in_upload: dict,
) -> tuple[Optional[str], Optional[int]]:
    """
    Check whether any of this row's identifiers already appeared in this upload.

    Returns (match_basis, row_idx_of_first_occurrence) or (None, None).
    seen_in_upload maps {"place_id": {val: row_idx}, "domain": {...}, ...}.
    """
    if pid := row_fields.get("google_place_id"):
        if pid in seen_in_upload.get("place_id", {}):
            return "google_place_id", seen_in_upload["place_id"][pid]

    if dom := row_fields.get("website_domain"):
        if dom in seen_in_upload.get("domain", {}):
            return "website_domain", seen_in_upload["domain"][dom]

    ph = row_fields.get("phone_digits") or ""
    if len(ph) >= 10 and ph in seen_in_upload.get("phone", {}):
        return "phone", seen_in_upload["phone"][ph]

    na = name_address_key(
        row_fields.get("name_normalized") or "",
        row_fields.get("address_normalized") or "",
    )
    if na and na in seen_in_upload.get("name_address", {}):
        return "name_address", seen_in_upload["name_address"][na]

    return None, None


def _register_seen(row_idx: int, row_fields: dict, seen_in_upload: dict) -> None:
    """Record this row's identifiers in seen_in_upload for subsequent-row dedup."""
    if pid := row_fields.get("google_place_id"):
        seen_in_upload.setdefault("place_id", {})[pid] = row_idx
    if dom := row_fields.get("website_domain"):
        seen_in_upload.setdefault("domain", {})[dom] = row_idx
    ph = row_fields.get("phone_digits") or ""
    if len(ph) >= 10:
        seen_in_upload.setdefault("phone", {})[ph] = row_idx
    na = name_address_key(
        row_fields.get("name_normalized") or "",
        row_fields.get("address_normalized") or "",
    )
    if na:
        seen_in_upload.setdefault("name_address", {})[na] = row_idx


# ---------------------------------------------------------------------------
# Single-row classification
# ---------------------------------------------------------------------------

def classify(
    row_idx: int,
    row_fields: dict,
    indexes: dict,
    entries: dict,
    seen_in_upload: dict,
) -> dict:
    """
    Classify a single Outscraper row.

    Parameters
    ----------
    row_idx:
        0-based position in the uploaded CSV — used as the stable row reference.
    row_fields:
        Normalized fields from outscraper_discovery_adapter.extract_fields().
    indexes:
        Registry lookup indexes from matcher.build_indexes().
    entries:
        The registry "entries" dict (entry_id → entry).
    seen_in_upload:
        Mutable dict tracking identifiers of rows already classified in this
        batch.  Updated in place by this function.

    Returns a classification record dict with keys:
        row_idx, classification, match_basis, entry_id,
        changed_fields, duplicate_of_row_idx
    """
    base: dict = {
        "row_idx": row_idx,
        "classification": None,
        "match_basis": None,
        "entry_id": None,
        "changed_fields": [],
        "duplicate_of_row_idx": None,
    }

    # Gate 1: insufficient data — no reliable identifier
    if not has_sufficient_data(row_fields):
        return {**base, "classification": INSUFFICIENT_DATA}

    # Gate 2: match against the persistent registry (highest priority)
    entry_id, match_basis = find_match(row_fields, indexes)
    if entry_id is not None:
        entry = entries[entry_id]
        changes = detect_changes(row_fields, entry)
        classification = CHANGED if changes else KNOWN
        _register_seen(row_idx, row_fields, seen_in_upload)
        return {
            **base,
            "classification": classification,
            "match_basis": match_basis,
            "entry_id": entry_id,
            "changed_fields": changes,
        }

    # Gate 3: intra-upload duplicate (same CSV, different row)
    dup_basis, dup_row_idx = _find_intra_upload_match(row_fields, seen_in_upload)
    _register_seen(row_idx, row_fields, seen_in_upload)
    if dup_basis is not None:
        return {
            **base,
            "classification": POSSIBLE_DUPLICATE,
            "match_basis": dup_basis,
            "duplicate_of_row_idx": dup_row_idx,
        }

    # Gate 4: genuinely new
    return {**base, "classification": NEW}
