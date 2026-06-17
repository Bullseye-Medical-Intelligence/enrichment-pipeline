"""
client_exports.py
Client deliverable package generation for a completed run.

Builds an in-memory ZIP containing:
  Bullseye_Target_Report.html   — per-account intelligence briefs for Bullseye tier
  Sales_Handoff.html            — rep-facing sales handoff (handoff_renderer)
  bullseye_accounts.csv         — Bullseye-tier approved records
  contender_accounts.csv        — Contender-tier approved records
  excluded_targets.csv          — all excluded records

No internal/debug artifacts (run_log.json, reviews.json, raw enriched JSON) are
included in the package. The run manifest (build_run_manifest) is an
internal-only provenance summary and is deliberately NOT shipped to the client.
"""

import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import exports
import icp_profiles
import projects
import record_adapter
import reviews
import sales_export

logger = logging.getLogger(__name__)

METHODOLOGY = (
    "Bullseye Medical Intelligence reviewed publicly available physician and "
    "practice-level online signals to identify accounts that appear commercially "
    "relevant for outreach. The review focused on observable fit indicators, "
    "service-line alignment, exclusion risks, evidence strength, and rep-facing "
    "sales actionability. Bullseye does not use PHI, patient records, claims files, "
    "appointment data, EMR access, login-gated systems, or private patient-level "
    "datasets."
)


def build_client_package(run_id: str, run_directory: Path, status) -> io.BytesIO:
    """Build the client deliverable ZIP and return it as a BytesIO.

    Args:
        run_id: The run identifier.
        run_directory: The run's output directory.
        status: The run's RunStatus (for project/ICP/count context).

    Returns:
        BytesIO positioned at 0, containing the ZIP archive.
    """
    records = _load_records(run_directory)
    all_reviews = reviews.get_reviews(run_id, run_directory)
    project = projects.read_config_snapshot(run_directory) or {}
    icp = icp_profiles.read_snapshot(run_directory) or {}

    approved = _approved_records(records, all_reviews)
    excluded_count = sum(
        1 for r in records
        if record_adapter.effective_tier(r, all_reviews).lower() == "excluded"
    )

    # Reuse the records/reviews already loaded above — the CSV builders accept
    # them so the same files are not re-read three more times.
    bullseye_csv = exports.build_bullseye_csv(run_id, run_directory, records, all_reviews).getvalue()
    contender_csv = exports.build_contender_csv(run_id, run_directory, records, all_reviews).getvalue()
    excluded_csv = exports.build_excluded_csv(run_id, run_directory, records, all_reviews).getvalue()

    bullseye_cards_bytes = _build_bullseye_cards(
        run_id, status, project, icp, approved, all_reviews, len(records), excluded_count,
    )
    handoff_bytes = _build_sales_handoff(run_id, run_directory, status)

    files = {
        "Bullseye_Target_Report.html": bullseye_cards_bytes,
        "Sales_Handoff.html": handoff_bytes,
        "bullseye_accounts.csv": bullseye_csv,
        "contender_accounts.csv": contender_csv,
        "excluded_targets.csv": excluded_csv,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    buf.seek(0)
    logger.info("Built client package for run %s (%d approved)", run_id, len(approved))
    return buf


def build_run_manifest(run_id: str, run_directory: Path, status) -> bytes:
    """Build the internal run manifest JSON and return UTF-8 bytes.

    Internal-only provenance summary (scope, ICP version, counts, methodology).
    Deliberately NOT included in the client package — it is for operator audit
    and reconciliation, exposed via the operator download route.
    """
    records = _load_records(run_directory)
    all_reviews = reviews.get_reviews(run_id, run_directory)
    project = projects.read_config_snapshot(run_directory) or {}
    icp = icp_profiles.read_snapshot(run_directory) or {}

    approved = _approved_records(records, all_reviews)
    excluded_count = sum(
        1 for r in records
        if record_adapter.effective_tier(r, all_reviews).lower() == "excluded"
    )
    metadata = _run_metadata(
        run_id, status, project, icp,
        len(records), len(approved), excluded_count,
    )
    return json.dumps(metadata, indent=2).encode("utf-8")


# ---------------------------------------------------------------------------
# Record helpers
# ---------------------------------------------------------------------------

def _load_records(run_directory: Path) -> list[dict]:
    """Read and normalise enriched_targets.json into a list of records."""
    path = run_directory / "enriched_targets.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return record_adapter.normalize_records_payload(json.load(f))


def _approved_records(records: list[dict], all_reviews: dict) -> list[dict]:
    """Return approved records (per exports.is_approved) sorted by score (desc)."""
    approved = [
        rec for rec in records
        if exports.is_approved(rec, all_reviews.get(record_adapter.get_record_id(rec), {}))
    ]
    approved.sort(key=lambda r: r.get("bullseye_score") or 0, reverse=True)
    return approved


# ---------------------------------------------------------------------------
# Report + metadata builders
# ---------------------------------------------------------------------------

def _error_html(title: str, exc: Exception) -> bytes:
    """Build a minimal HTML page that visibly states a generation error.

    A visible error page keeps the client ZIP non-corrupt (the documented
    guarantee) while making the failure obvious, instead of an empty file that
    looks like a successful export.
    """
    import html as _html
    return (
        f"<html><body>"
        f"<p><strong>{_html.escape(title)}:</strong> "
        f"{_html.escape(type(exc).__name__)}: {_html.escape(str(exc)[:180])}</p>"
        f"<p>Please contact the operations team.</p>"
        f"</body></html>"
    ).encode("utf-8")


def _build_bullseye_cards(
    run_id: str,
    status,
    project: dict,
    icp: dict,
    approved: list[dict],
    all_reviews: dict,
    screened: int,
    excluded_count: int,
) -> bytes:
    """Render the Bullseye Target Report (per-account cards) HTML; return UTF-8 bytes."""
    try:
        from reports import pdf_report
        return pdf_report.build_bullseye_cards_html(
            run_id=run_id,
            status=status,
            project=project,
            icp=icp,
            approved_records=approved,
            all_reviews=all_reviews,
            screened=screened,
            excluded_count=excluded_count,
        )
    except Exception as exc:
        logger.exception("Bullseye cards generation failed for run %s", run_id)
        return _error_html("Bullseye Target Report generation failed", exc)


def _build_sales_handoff(run_id: str, run_directory: Path, status) -> bytes:
    """Render the Sales Handoff HTML for the client ZIP; return UTF-8 bytes."""
    try:
        return sales_export._build_client_handoff_html(run_id, run_directory, status)
    except Exception as exc:
        logger.exception("Sales Handoff HTML generation failed for run %s", run_id)
        return _error_html("Sales Handoff generation failed", exc)


def _run_metadata(
    run_id: str,
    status,
    project: dict,
    icp: dict,
    screened: int,
    approved: int,
    excluded: int,
) -> dict:
    """Build the machine-readable run_metadata.json payload."""
    geography = status.target_geography or project.get("target_geography") or []
    return {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client_name": status.client_name or project.get("client_name") or None,
        "project_id": status.project_id or None,
        "product_name": status.product_name or project.get("product_name") or None,
        "target_specialty": status.target_specialty or project.get("target_specialty") or None,
        "target_geography": geography,
        "icp_profile_id": status.icp_profile_id or icp.get("icp_id") or None,
        "icp_profile_name": status.icp_profile_name or icp.get("name") or None,
        "icp_profile_version": status.icp_profile_version or icp.get("version") or None,
        "records_screened": screened,
        "records_approved": approved,
        "records_excluded": excluded,
        "methodology": METHODOLOGY,
    }
