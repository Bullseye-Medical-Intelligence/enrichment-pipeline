"""
exports.py
Filtered CSV export logic for completed runs.
Reads enriched_targets.json + reviews.json, applies review overlay, and
streams a filtered CSV. Never writes to disk — returns BytesIO.
"""

import csv
import io
import json
import logging
from pathlib import Path
from typing import Callable

import record_adapter
import reviews

logger = logging.getLogger(__name__)

# Review-overlay columns appended to every export row
_REVIEW_COLUMNS = [
    "displayed_tier",
    "qc_status",
    "analyst_note",
    "override_tier",
    "override_reason",
    "reviewed_by",
    "reviewed_at",
]


def _is_approved(rec: dict, rev: dict) -> bool:
    """Return True when a record passes the approved-export gate.

    Gate rules (all must hold):
    - qc_status == "approved"
    - effective displayed_tier != "excluded"
    - without an analyst override_tier, the pipeline tier is exportable:
      not "EXCLUDED" and not "Needs Verification" (unconfirmed accounts ship
      only after an analyst confirms them with an override)
    """
    if rev.get("qc_status") != "approved":
        return False
    displayed = (rev.get("override_tier") or rec.get("target_tier", "")).lower()
    if displayed == "excluded":
        return False
    if not rev.get("override_tier"):
        if rec.get("exclusion_status") == "EXCLUDED":
            return False
        if rec.get("target_tier") == "Needs Verification":
            return False
    return True


def build_approved_csv(run_id: str, run_directory: Path) -> io.BytesIO:
    """Return a BytesIO CSV of approved records (all tiers)."""
    return _build_csv(run_id, run_directory, _is_approved)


def build_bullseye_csv(run_id: str, run_directory: Path) -> io.BytesIO:
    """Return a BytesIO CSV of approved Bullseye-tier records only."""
    def _bullseye(rec: dict, rev: dict) -> bool:
        if not _is_approved(rec, rev):
            return False
        displayed = (rev.get("override_tier") or rec.get("target_tier", "")).lower()
        return displayed == "bullseye"

    return _build_csv(run_id, run_directory, _bullseye)


def build_warm_csv(run_id: str, run_directory: Path) -> io.BytesIO:
    """Return a BytesIO CSV of approved Strong/Warm-tier records."""
    _WARM_TIERS = {"strong", "warm"}

    def _warm(rec: dict, rev: dict) -> bool:
        if not _is_approved(rec, rev):
            return False
        displayed = (rev.get("override_tier") or rec.get("target_tier", "")).lower()
        return displayed in _WARM_TIERS

    return _build_csv(run_id, run_directory, _warm)


def build_excluded_csv(run_id: str, run_directory: Path) -> io.BytesIO:
    """Return a BytesIO CSV of records whose effective tier is Excluded."""
    def _excluded(rec: dict, rev: dict) -> bool:
        displayed = (rev.get("override_tier") or rec.get("target_tier", "")).lower()
        return displayed == "excluded"

    return _build_csv(run_id, run_directory, _excluded)


def _build_csv(
    run_id: str,
    run_directory: Path,
    filter_fn: Callable[[dict, dict], bool],
) -> io.BytesIO:
    """Load, merge, filter records and return a UTF-8 encoded BytesIO CSV.

    filter_fn receives (record, review) and returns True to include the row.
    """
    results_path = run_directory / "enriched_targets.json"
    if not results_path.exists():
        return io.BytesIO()

    with open(results_path, "r", encoding="utf-8") as f:
        raw_records = record_adapter.normalize_records_payload(json.load(f))

    if not raw_records:
        return io.BytesIO()

    all_reviews = reviews.get_reviews(run_id, run_directory)

    # Derive column order from first record (scalar fields only) then append review overlay
    first = raw_records[0]
    record_columns = [k for k, v in first.items() if not isinstance(v, (dict, list))]
    all_columns = record_columns + _REVIEW_COLUMNS

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_columns, extrasaction="ignore")
    writer.writeheader()

    for rec in raw_records:
        rid = record_adapter.get_record_id(rec)
        review = all_reviews.get(rid, reviews.default_review())

        if not filter_fn(rec, review):
            continue

        displayed_tier = review.get("override_tier") or rec.get("target_tier", "")
        row = {k: (v if not isinstance(v, (dict, list)) else "") for k, v in rec.items()}
        row.update({
            "displayed_tier": displayed_tier,
            "qc_status": review.get("qc_status", "pending"),
            "analyst_note": review.get("analyst_note") or "",
            "override_tier": review.get("override_tier") or "",
            "override_reason": review.get("override_reason") or "",
            "reviewed_by": review.get("reviewed_by") or "",
            "reviewed_at": review.get("reviewed_at") or "",
        })
        writer.writerow(row)

    return io.BytesIO(buf.getvalue().encode("utf-8"))
