"""
sales_export.py
Sales Handoff HTML generation for a completed, fully-reviewed run.

Produces the Bullseye Sales Handoff — the primary client deliverable:
a single self-contained HTML file using the reference design (Call First /
Validate / Suppress, three-tier format, no internal scores).

Used by:
  - /runs/{run_id}/download/sales-html  (operator download button)
  - client_exports.build_client_package (included in client ZIP)
"""

import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import exports
import icp_profiles
import projects
import record_adapter
import reviews

logger = logging.getLogger(__name__)

# Add the repo root to sys.path so we can import the handoff_renderer module.
# pipeline-api/ is one level below the repo root; __file__ is inside pipeline-api/.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from handoff_renderer import Account, Confidence, HandoffRun, Tier, render_handoff  # noqa: E402

# Tiers included in the client handoff (Needs Verification and Manual Review
# are not shipped until an analyst confirms them with an override).
_CLIENT_TIERS = {"Bullseye", "Contender", "Excluded"}

_TIER_MAP = {
    "Bullseye": Tier.BULLSEYE,
    "Contender": Tier.CONTENDER,
    "Excluded": Tier.EXCLUDED,
}

_CONFIDENCE_MAP = {
    "High": Confidence.HIGH,
    "Moderate": Confidence.MEDIUM,
    "Low": Confidence.LOW,
}


def build_sales_handoff(run_id: str, run_directory: Path, status) -> bytes:
    """Build the Sales Handoff HTML and return UTF-8 bytes.

    Produces the reference-format client deliverable (Call First / Validate /
    Suppress). No internal scores, no analyst notes. Safe to include in the
    client ZIP and to share with the client's sales team.
    """
    records = _load_records(run_directory)
    all_reviews = reviews.get_reviews(run_id, run_directory)
    project = projects.read_config_snapshot(run_directory) or {}
    icp = icp_profiles.read_snapshot(run_directory) or {}

    handoff_run = _build_handoff_run(run_id, status, project, icp, records, all_reviews)
    html_str = render_handoff(handoff_run, client_facing=True)
    logger.info(
        "Generated Sales Handoff HTML for run %s: %d accounts, %d bytes",
        run_id, len(handoff_run.accounts), len(html_str),
    )
    return html_str.encode("utf-8")


# ---------------------------------------------------------------------------
# Internal builders
# ---------------------------------------------------------------------------

def _load_records(run_directory: Path) -> list[dict]:
    """Read and normalise enriched_targets.json into a list of records."""
    path = run_directory / "enriched_targets.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return record_adapter.normalize_records_payload(json.load(f))


def _build_handoff_run(
    run_id: str,
    status,
    project: dict,
    icp: dict,
    records: list[dict],
    all_reviews: dict,
) -> HandoffRun:
    """Convert pipeline run data into a HandoffRun for the renderer."""
    run_date = _parse_run_date(status)
    metro = _metro_label(status, project)
    qc_reviewer = (
        (status.operator or "").strip() or project.get("qc_reviewer") or "—"
    )
    icp_version = (
        status.icp_profile_id
        or status.icp_profile_name
        or icp.get("icp_id")
        or icp.get("name")
        or "—"
    )

    accounts = []
    for rec in records:
        rec_id = record_adapter.get_record_id(rec)
        review = all_reviews.get(rec_id, {})
        tier_str = record_adapter.effective_tier(rec, review)
        if tier_str not in _CLIENT_TIERS:
            continue
        # Bullseye and Contender require analyst approval — rejected records must
        # not appear in the handoff even though the run gate only checks for pending.
        if tier_str in ("Bullseye", "Contender") and not exports.is_approved(rec, review):
            continue
        accounts.append(_record_to_account(rec, tier_str))

    return HandoffRun(
        product_name=status.product_name or project.get("product_name") or "—",
        client_name=status.client_name or project.get("client_name") or "—",
        run_date=run_date,
        specialty_label=status.target_specialty or project.get("target_specialty") or "—",
        metro=metro,
        icp_version=icp_version,
        qc_reviewer=qc_reviewer,
        accounts=accounts,
        pattern_insight=None,  # run-level insight is not in the pipeline schema
    )


def _record_to_account(rec: dict, tier_str: str) -> Account:
    """Map a single pipeline record dict to a handoff_renderer Account."""
    tier = _TIER_MAP.get(tier_str, Tier.EXCLUDED)
    confidence = _CONFIDENCE_MAP.get(rec.get("confidence_band") or "Low", Confidence.LOW)

    signals = rec.get("signals") or []
    confirmed_signals = [
        sig.get("signal_label") or sig.get("label") or ""
        for sig in signals
        if (sig.get("signal_state") == "yes" or sig.get("state_inferred"))
        and (sig.get("signal_label") or sig.get("label"))
    ]

    brief = rec.get("call_brief") or {}
    verify = _coerce_list(brief.get("missing_to_verify"))
    # missing_to_verify is only populated for verification_required signals that are
    # not_found. For fully-confirmed records it's empty, so fall back to the
    # discovery question so reps always get at least one actionable prompt.
    if not verify:
        disc = (brief.get("discovery_question") or "").strip()
        if disc:
            verify = [disc]
    sales_angles = _coerce_list(rec.get("sales_angle"))
    wedge = sales_angles[0] if sales_angles else (brief.get("opening_line") or None)

    return Account(
        name=rec.get("practice_name") or rec.get("name") or "Unknown Practice",
        city=_format_city(rec),
        phone=_format_phone(rec.get("phone") or rec.get("phone_number") or ""),
        website=_extract_domain(rec.get("website_url") or rec.get("website") or ""),
        evidence_domain=_extract_domain(rec.get("website_url") or rec.get("website") or ""),
        tier=tier,
        confidence=confidence,
        internal_score=int(rec.get("bullseye_score") or 0),
        flags=[],  # practice-level flags not yet in pipeline schema
        # Non-excluded content
        why_it_matters=brief.get("why_contact") or None,
        wedge=wedge or None,
        confirmed_signals=confirmed_signals,
        verify=verify,
        landmine=brief.get("disqualifier_risk") or None,
        # Excluded content
        gate_fired=rec.get("exclusion_reason") or None,
        evidence=_extract_domain(rec.get("website_url") or rec.get("website") or "") or None,
        suppress_reason=rec.get("exclusion_reason") or None,
        revisit_if=None,  # not in pipeline schema
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _parse_run_date(status) -> date:
    """Parse the run's start date from status; fall back to today."""
    for attr in ("created_at", "completed_at"):
        val = getattr(status, attr, None)
        if val:
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                pass
    return date.today()


def _metro_label(status, project: dict) -> str:
    """Extract a readable metro/geography label from the run."""
    geo = getattr(status, "target_geography", None) or project.get("target_geography") or []
    if isinstance(geo, list):
        return ", ".join(geo) if geo else "—"
    return str(geo) or "—"


def _format_city(rec: dict) -> str:
    """Format city + zip + state as 'City (ZIP), ST' or 'City, ST'."""
    city = (rec.get("address_city") or "").strip()
    state = (rec.get("address_state") or "").strip()
    zip_code = (rec.get("address_zip") or "").strip()
    if not city and not state:
        return "—"
    if zip_code:
        return f"{city} ({zip_code}), {state}" if city else f"({zip_code}), {state}"
    return f"{city}, {state}" if city else state


def _format_phone(raw: str) -> str:
    """Format a phone number string to (NXX) NXX-XXXX."""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return raw or "—"


def _extract_domain(url: str) -> str:
    """Return the bare domain (no www.) from a URL string."""
    if not url:
        return ""
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        host = parsed.netloc or parsed.path.split("/")[0]
        return host.removeprefix("www.")
    except Exception:
        return url


def _coerce_list(value) -> list[str]:
    """Normalise a field that might be a list, a string, or None to list[str]."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)]
