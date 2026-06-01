"""
sales_export.py
Internal Sales Handoff HTML generation for a completed, fully-reviewed run.

Counterpart to client_exports.py — that module is client-safe (Bullseye only,
scores stripped, no analyst notes, client ZIP only). This module is
internal-audience: every record and tier, with analyst notes, call briefs, and
QC metadata. Never included in the client ZIP.
"""

import json
import logging
from pathlib import Path

import icp_profiles
import projects
import record_adapter
import reviews

logger = logging.getLogger(__name__)

# Records are grouped and rendered in this order.
_TIER_ORDER = ["Bullseye", "Needs Verification", "Contender", "Manual Review", "Excluded"]


def build_sales_handoff(run_id: str, run_directory: Path, status) -> bytes:
    """Build the internal Sales Handoff HTML and return UTF-8 bytes."""
    records = _load_records(run_directory)
    all_reviews = reviews.get_reviews(run_id, run_directory)
    project = projects.read_config_snapshot(run_directory) or {}
    icp = icp_profiles.read_snapshot(run_directory) or {}

    grouped = _group_by_tier(records, all_reviews)

    from reports import pdf_report
    return pdf_report.build_sales_handoff_html(
        run_id=run_id,
        status=status,
        project=project,
        icp=icp,
        grouped_records=grouped,
        all_reviews=all_reviews,
        screened=len(records),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_records(run_directory: Path) -> list[dict]:
    """Read and normalise enriched_targets.json into a list of records."""
    path = run_directory / "enriched_targets.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return record_adapter.normalize_records_payload(json.load(f))


def _group_by_tier(records: list[dict], all_reviews: dict) -> dict:
    """Partition records into tier groups ordered by _TIER_ORDER, each sorted by score desc."""
    groups: dict[str, list[dict]] = {tier: [] for tier in _TIER_ORDER}
    for rec in records:
        tier = record_adapter.effective_tier(rec, all_reviews)
        if tier not in groups:
            tier = "Excluded"
        groups[tier].append(rec)
    for tier in groups:
        groups[tier].sort(key=lambda r: r.get("bullseye_score") or 0, reverse=True)
    return groups
