"""
client_exports.py
Client deliverable package generation for a completed run.

Builds an in-memory ZIP from the immutable enriched_targets.json plus the
additive reviews.json overlay. Reuses exports.py for the approved/excluded CSVs.
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

TOP_BRIEF_LIMIT = 10

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
    excluded_count = sum(1 for r in records if _displayed_tier(r, all_reviews).lower() == "excluded")

    approved_csv = exports.build_approved_csv(run_id, run_directory).getvalue()
    excluded_csv = exports.build_excluded_csv(run_id, run_directory).getvalue()

    files = {
        "executive_summary.md": _executive_summary(
            status, project, icp, len(records), len(approved), excluded_count
        ).encode("utf-8"),
        "approved_targets.csv": approved_csv,
        "excluded_targets.csv": excluded_csv,
        "top_target_briefs.md": _top_target_briefs(approved, all_reviews).encode("utf-8"),
        "methodology.md": _methodology_md().encode("utf-8"),
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


def _displayed_tier(record: dict, all_reviews: dict) -> str:
    """Return the effective tier: analyst override if set, else pipeline tier."""
    review = all_reviews.get(record_adapter.get_record_id(record), {})
    return review.get("override_tier") or record.get("target_tier", "")


def _approved_records(records: list[dict], all_reviews: dict) -> list[dict]:
    """Return approved, non-hard-excluded records sorted by score (desc).

    Mirrors exports.build_approved_csv: a hard pipeline exclusion cannot be
    bypassed by an analyst override.
    """
    approved = []
    for rec in records:
        review = all_reviews.get(record_adapter.get_record_id(rec), {})
        if review.get("qc_status") != "approved":
            continue
        if rec.get("exclusion_status") == "EXCLUDED":
            continue
        if _displayed_tier(rec, all_reviews).lower() == "excluded":
            continue
        approved.append(rec)
    approved.sort(key=lambda r: r.get("bullseye_score") or 0, reverse=True)
    return approved


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------

def _executive_summary(status, project: dict, icp: dict, screened: int,
                       approved: int, excluded: int) -> str:
    """Build executive_summary.md from run + project + ICP context."""
    geography = status.target_geography or project.get("target_geography") or []
    if isinstance(geography, list):
        geography = ", ".join(geography)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output = status.records_output or screened

    lines = [
        "# Executive Summary",
        "",
        f"- **Client:** {status.client_name or project.get('client_name') or '—'}",
        f"- **Project:** {status.project_id or '—'}",
        f"- **Product:** {status.product_name or project.get('product_name') or '—'}",
        f"- **Target Specialty:** {status.target_specialty or project.get('target_specialty') or '—'}",
        f"- **Geography:** {geography or '—'}",
        f"- **ICP Profile:** {status.icp_profile_name or icp.get('name') or '—'}"
        f" ({status.icp_profile_version or icp.get('version') or '—'})",
        "",
        "## Volumes",
        "",
        f"- **Records screened:** {screened}",
        f"- **Records enriched / output:** {output}",
        f"- **Approved targets:** {approved}",
        f"- **Excluded targets:** {excluded}",
        f"- **Generated:** {generated}",
        "",
        "## Methodology",
        "",
        METHODOLOGY,
    ]
    return "\n".join(lines) + "\n"


def _top_target_briefs(approved: list[dict], all_reviews: dict) -> str:
    """Build top_target_briefs.md for the highest-scoring approved targets."""
    lines = ["# Top Target Briefs", ""]
    top = approved[:TOP_BRIEF_LIMIT]
    if not top:
        lines.append("No approved targets in this run.")
        return "\n".join(lines) + "\n"

    for i, rec in enumerate(top, start=1):
        review = all_reviews.get(record_adapter.get_record_id(rec), {})
        displayed = review.get("override_tier") or rec.get("target_tier", "—")
        city = rec.get("address_city", "")
        state = rec.get("address_state", "")
        location = ", ".join(p for p in (city, state) if p) or "—"

        lines.append(f"## {i}. {rec.get('practice_name', 'Unknown practice')}")
        if rec.get("website_url"):
            lines.append(f"- **Website:** {rec['website_url']}")
        lines.append(f"- **Location:** {location}")
        lines.append(f"- **Tier:** {displayed} (pipeline: {rec.get('target_tier', '—')})")
        if rec.get("bullseye_score") is not None:
            lines.append(f"- **Score:** {rec.get('bullseye_score')}"
                         f" | Confidence: {rec.get('confidence_score', '—')}")

        sales_angles = rec.get("sales_angle") or []
        if sales_angles:
            lines.append("- **Sales angle:**")
            lines.extend(f"  - {angle}" for angle in sales_angles)

        evidence = _top_evidence(rec)
        if evidence:
            lines.append("- **Evidence:**")
            lines.extend(f"  - {e}" for e in evidence)

        if review.get("analyst_note"):
            lines.append(f"- **Analyst note:** {review['analyst_note']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _top_evidence(record: dict, limit: int = 3) -> list[str]:
    """Return up to `limit` client-safe evidence snippets with source URLs."""
    snippets = []
    for signal in record.get("signals") or []:
        text = (signal.get("evidence_text") or "").strip()
        if not text:
            continue
        source = signal.get("source_url")
        snippets.append(f"{text} ({source})" if source else text)
        if len(snippets) >= limit:
            break
    return snippets


def _methodology_md() -> str:
    """Build the standalone methodology.md document."""
    return f"# Methodology\n\n{METHODOLOGY}\n"
