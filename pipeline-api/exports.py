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

# Rep-facing column derived from the pipeline's call_brief (a nested object, so
# it is not picked up by the scalar-field column scan).
_BRIEF_COLUMNS = ["why_contact"]

# Numeric scores are internal-only. Client-facing exports carry the qualitative
# confidence_band and the tier, never the raw fit/confidence/bullseye numbers.
_HIDDEN_SCORE_COLUMNS = {
    "bullseye_score",
    "fit_signal_score",
    "confidence_score",
    "fit_confidence_status",
}

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


def is_approved(rec: dict, rev: dict) -> bool:
    """Return True when a record passes the approved-export gate.

    Gate rules (all must hold):
    - qc_status == "approved"
    - effective displayed_tier != "excluded"
    - without an analyst override_tier, the pipeline tier is exportable:
      not "EXCLUDED", not "Needs Verification", and not "Manual Review"
      (unconfirmed / no-evidence accounts ship only after an analyst confirms
      them with an override)
    """
    if rev.get("qc_status") != "approved":
        return False
    if record_adapter.displayed_tier(rec, rev).lower() == "excluded":
        return False
    if not rev.get("override_tier"):
        if rec.get("exclusion_status") == "EXCLUDED":
            return False
        if rec.get("target_tier") in ("Needs Verification", "Manual Review"):
            return False
    return True


def build_approved_csv(run_id, run_directory, records=None, all_reviews=None) -> io.BytesIO:
    """Return a BytesIO CSV of approved records (all tiers)."""
    return _build_csv(run_id, run_directory, is_approved, records, all_reviews)


def build_bullseye_csv(run_id, run_directory, records=None, all_reviews=None) -> io.BytesIO:
    """Return a BytesIO CSV of approved Bullseye-tier records only."""
    def _bullseye(rec: dict, rev: dict) -> bool:
        return is_approved(rec, rev) and record_adapter.displayed_tier(rec, rev).lower() == "bullseye"

    return _build_csv(run_id, run_directory, _bullseye, records, all_reviews)


def build_contender_csv(run_id, run_directory, records=None, all_reviews=None) -> io.BytesIO:
    """Return a BytesIO CSV of approved Contender-tier records."""
    def _contender(rec: dict, rev: dict) -> bool:
        return is_approved(rec, rev) and record_adapter.displayed_tier(rec, rev).lower() == "contender"

    return _build_csv(run_id, run_directory, _contender, records, all_reviews)


def build_excluded_csv(run_id, run_directory, records=None, all_reviews=None) -> io.BytesIO:
    """Return a BytesIO CSV of records whose effective tier is Excluded."""
    def _excluded(rec: dict, rev: dict) -> bool:
        return record_adapter.displayed_tier(rec, rev).lower() == "excluded"

    return _build_csv(run_id, run_directory, _excluded, records, all_reviews)


def build_retry_csv(run_id: str, run_directory: Path) -> io.BytesIO:
    """Return a BytesIO manual-format CSV of records that failed to crawl.

    Includes records with source_confidence 'limited' or 'failed' — i.e.
    the pipeline could not extract meaningful web content. The CSV is in the
    Bullseye manual format so it can be uploaded as a new run for re-crawling.
    """
    results_path = run_directory / "enriched_targets.json"
    if not results_path.exists():
        return io.BytesIO()
    with open(results_path, "r", encoding="utf-8") as f:
        records = record_adapter.normalize_records_payload(json.load(f))

    crawl_failed = [
        r for r in records
        if r.get("source_confidence") in ("limited", "failed")
    ]
    if not crawl_failed:
        return io.BytesIO()

    fieldnames = ["practice_name", "website_url", "phone",
                  "address_city", "address_state", "address_zip", "specialty"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for rec in crawl_failed:
        writer.writerow({
            "practice_name": rec.get("practice_name", ""),
            "website_url": record_adapter.normalize_homepage_url(rec.get("website_url", "")),
            "phone": rec.get("phone", ""),
            "address_city": rec.get("address_city", ""),
            "address_state": rec.get("address_state", ""),
            "address_zip": rec.get("address_zip", ""),
            "specialty": rec.get("specialty", ""),
        })
    return io.BytesIO(buf.getvalue().encode("utf-8"))


def _build_csv(
    run_id: str,
    run_directory: Path,
    filter_fn: Callable[[dict, dict], bool],
    records: list[dict] | None = None,
    all_reviews: dict | None = None,
) -> io.BytesIO:
    """Load, merge, filter records and return a UTF-8 encoded BytesIO CSV.

    filter_fn receives (record, review) and returns True to include the row.
    Callers that already hold the records/reviews (e.g. the client-package
    builder) may pass them in to avoid re-reading the same files.
    """
    if records is None:
        results_path = run_directory / "enriched_targets.json"
        if not results_path.exists():
            return io.BytesIO()
        with open(results_path, "r", encoding="utf-8") as f:
            records = record_adapter.normalize_records_payload(json.load(f))

    if not records:
        return io.BytesIO()

    if all_reviews is None:
        all_reviews = reviews.get_reviews(run_id, run_directory)

    # Derive column order from first record (scalar fields only, numeric scores
    # excluded) then append review overlay. confidence_band rides along as a
    # normal scalar field, so the client sees the band, not the number.
    first = records[0]
    record_columns = [
        k for k, v in first.items()
        if not isinstance(v, (dict, list)) and k not in _HIDDEN_SCORE_COLUMNS
    ]
    all_columns = record_columns + _BRIEF_COLUMNS + _REVIEW_COLUMNS

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_columns, extrasaction="ignore")
    writer.writeheader()

    for rec in records:
        rid = record_adapter.get_record_id(rec)
        review = all_reviews.get(rid, reviews.default_review())

        if not filter_fn(rec, review):
            continue

        row = {k: (v if not isinstance(v, (dict, list)) else "") for k, v in rec.items()}
        row["why_contact"] = (rec.get("call_brief") or {}).get("why_contact", "")
        row.update({
            "displayed_tier": record_adapter.displayed_tier(rec, review),
            "qc_status": review.get("qc_status", "pending"),
            "analyst_note": review.get("analyst_note") or "",
            "override_tier": review.get("override_tier") or "",
            "override_reason": review.get("override_reason") or "",
            "reviewed_by": review.get("reviewed_by") or "",
            "reviewed_at": review.get("reviewed_at") or "",
        })
        writer.writerow(row)

    return io.BytesIO(buf.getvalue().encode("utf-8"))
