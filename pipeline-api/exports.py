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


def build_approved_csv(run_id: str, run_directory: Path) -> io.BytesIO:
    """Return a BytesIO CSV of approved, non-excluded records."""
    return _build_csv(
        run_id,
        run_directory,
        lambda tier, qc: qc == "approved" and tier.lower() != "excluded",
    )


def build_excluded_csv(run_id: str, run_directory: Path) -> io.BytesIO:
    """Return a BytesIO CSV of records whose effective tier is Excluded."""
    return _build_csv(
        run_id,
        run_directory,
        lambda tier, _qc: tier.lower() == "excluded",
    )


def _build_csv(
    run_id: str,
    run_directory: Path,
    filter_fn: Callable[[str, str], bool],
) -> io.BytesIO:
    """Load, merge, filter records and return a UTF-8 encoded BytesIO CSV."""
    results_path = run_directory / "enriched_targets.json"
    if not results_path.exists():
        return io.BytesIO()

    with open(results_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    raw_records = data.get("records", data) if isinstance(data, dict) else data

    if not raw_records:
        return io.BytesIO()

    all_reviews = reviews.get_reviews(run_id, run_directory)

    # Derive column order from first record (scalar fields first, then review overlay)
    first = raw_records[0]
    record_columns = [k for k, v in first.items() if not isinstance(v, (dict, list))]
    all_columns = record_columns + _REVIEW_COLUMNS

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_columns, extrasaction="ignore")
    writer.writeheader()

    for record in raw_records:
        record_id = record.get("record_id", "")
        review = all_reviews.get(record_id, reviews.default_review())
        displayed_tier = review.get("override_tier") or record.get("target_tier", "")
        qc_status = review.get("qc_status", "pending")

        if not filter_fn(displayed_tier, qc_status):
            continue

        row = {k: (v if not isinstance(v, (dict, list)) else "") for k, v in record.items()}
        row.update({
            "displayed_tier": displayed_tier,
            "qc_status": qc_status,
            "analyst_note": review.get("analyst_note") or "",
            "override_tier": review.get("override_tier") or "",
            "override_reason": review.get("override_reason") or "",
            "reviewed_by": review.get("reviewed_by") or "",
            "reviewed_at": review.get("reviewed_at") or "",
        })
        writer.writerow(row)

    return io.BytesIO(buf.getvalue().encode("utf-8"))
