"""
client_exports.py
Client deliverable package generation for a completed run.

Builds an in-memory ZIP containing:
  Executive_Target_Report.pdf   — branded PDF (WeasyPrint)
  bullseye_accounts.csv         — Bullseye-tier approved records
  contender_accounts.csv        — Contender-tier approved records
  excluded_targets.csv          — all excluded records
  run_metadata.json             — machine-readable run summary

No internal/debug artifacts (run_log.json, reviews.json, raw enriched JSON) are
included in the package.
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

    pdf_bytes = _build_pdf(
        run_id, status, project, icp,
        approved, all_reviews,
        len(records), excluded_count,
    )
    html_bytes = _build_bullseye_html(
        run_id, status, project, icp,
        approved, all_reviews,
        len(records), excluded_count,
    )

    metadata = _run_metadata(
        run_id, status, project, icp,
        len(records), len(approved), excluded_count,
    )

    files = {
        "Executive_Target_Report.pdf": pdf_bytes,
        "Bullseye_Target_Report.html": html_bytes,
        "bullseye_accounts.csv": bullseye_csv,
        "contender_accounts.csv": contender_csv,
        "excluded_targets.csv": excluded_csv,
        "run_metadata.json": json.dumps(metadata, indent=2).encode("utf-8"),
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    buf.seek(0)
    logger.info("Built client package for run %s (%d approved)", run_id, len(approved))
    return buf


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
# PDF + metadata builders
# ---------------------------------------------------------------------------

def _error_pdf(message: str) -> bytes:
    """Build a minimal but valid one-page PDF that visibly states an error.

    Used when the executive report fails to render. A visible error page keeps
    the client ZIP non-corrupt (the documented guarantee) while making the
    failure obvious, instead of a blank document that looks like a successful
    export. Uses raw PDF bytes so it works even when WeasyPrint is the cause of
    the failure.
    """
    safe = message.replace("\\", "").replace("(", "[").replace(")", "]")
    content = (
        f"BT /F1 16 Tf 72 720 Td (Executive Target Report) Tj ET\n"
        f"BT /F1 11 Tf 72 690 Td ({safe}) Tj ET"
    ).encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += str(i).encode() + b" 0 obj\n" + obj + b"\nendobj\n"

    xref_pos = len(pdf)
    pdf += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += ("%010d 00000 n \n" % off).encode()
    pdf += (b"trailer\n<< /Size " + str(len(objects) + 1).encode() +
            b" /Root 1 0 R >>\nstartxref\n" + str(xref_pos).encode() + b"\n%%EOF\n")
    return bytes(pdf)


def _build_pdf(
    run_id: str,
    status,
    project: dict,
    icp: dict,
    approved: list[dict],
    all_reviews: dict,
    screened: int,
    excluded_count: int,
) -> bytes:
    """Render the Executive Target Report PDF; return raw bytes."""
    try:
        from reports import pdf_report
        return pdf_report.build_executive_report(
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
        logger.exception("PDF generation failed for run %s; returning error-page PDF", run_id)
        # Visible error page, not a blank stub — keeps the ZIP non-corrupt while
        # making the failure obvious to whoever opens the deliverable.
        return _error_pdf(
            f"Report generation failed for run {run_id}. "
            f"{type(exc).__name__}: {str(exc)[:180]}. "
            f"Please contact the operations team."
        )


def _build_bullseye_html(
    run_id: str,
    status,
    project: dict,
    icp: dict,
    approved: list[dict],
    all_reviews: dict,
    screened: int,
    excluded_count: int,
) -> bytes:
    """Render the Bullseye cards HTML report; return UTF-8 bytes."""
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
        logger.exception("HTML report generation failed for run %s", run_id)
        import html as _html
        return (
            f"<html><body>"
            f"<p><strong>Report generation failed:</strong> "
            f"{_html.escape(type(exc).__name__)}: {_html.escape(str(exc))}</p>"
            f"<p>Please contact the operations team.</p>"
            f"</body></html>"
        ).encode("utf-8")


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
