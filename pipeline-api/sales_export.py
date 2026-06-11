"""
sales_export.py
Sales Handoff HTML generation for a completed, fully-reviewed run.

Two builds serve different audiences:

  build_sales_handoff()          Internal-only. All 5 tiers, analyst notes,
                                 full call brief with coaching framing.
                                 Served by the operator "Download Sales HTML"
                                 button. NOT included in the client package ZIP.

  _build_client_handoff_html()   Client-facing via handoff_renderer. Three
                                 actionable tiers (Bullseye, Contender,
                                 Excluded), no analyst notes, no internal
                                 scores. Called by client_exports when
                                 assembling the client ZIP.
"""

import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

import exports
import icp_profiles
import projects
import record_adapter
import reviews

logger = logging.getLogger(__name__)

# Jinja2 env for standalone templates (sales_brief.html) that don't extend base.html.
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_brief_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)
import re as _re

_brief_env.filters["humanize"] = lambda s: s.replace("_", " ").title() if "_" in s else s


def _format_exclusion_parts(reason: str) -> list:
    """Split a pipe-delimited exclusion_reason into bullet-ready sentences.

    Converts any embedded snake_case identifiers (signal names that fell back
    to signal_id) to Title Case readable words, leaving normal prose unchanged.
    """
    parts = [p.strip() for p in reason.split(" | ") if p.strip()]
    cleaned = []
    for part in parts:
        part = _re.sub(
            r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b',
            lambda m: m.group(0).replace("_", " ").title(),
            part,
        )
        cleaned.append(part)
    return cleaned


_brief_env.filters["format_exclusion"] = _format_exclusion_parts

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
    """Build the internal Sales Handoff HTML and return UTF-8 bytes.

    Internal-only: all 5 tiers, analyst notes, full call brief with coaching
    framing. NOT included in the client package ZIP.
    """
    from reports import pdf_report  # lazy import — avoids circular dependency

    records = _load_records(run_directory)
    all_reviews = reviews.get_reviews(run_id, run_directory)
    project = projects.read_config_snapshot(run_directory) or {}
    icp = icp_profiles.read_snapshot(run_directory) or {}

    grouped = _group_by_tier(records, all_reviews)
    return pdf_report.build_sales_handoff_html(
        run_id=run_id,
        status=status,
        project=project,
        icp=icp,
        grouped_records=grouped,
        screened=len(records),
    )


def build_sales_brief(
    run_id: str,
    run_directory: Path,
    status,
    bullseye_id: str,
    contender_id: str,
    excluded_id: str,
) -> bytes:
    """Build a prospect-facing methodology brief for 3 operator-selected accounts.

    Renders a self-contained HTML document showing BEMI's ranking methodology
    illustrated with one Bullseye, one Contender, and one Excluded account.
    No internal numeric scores or analyst notes are included.
    """
    records = _load_records(run_directory)
    project = projects.read_config_snapshot(run_directory) or {}
    record_index = {record_adapter.get_record_id(r): r for r in records}

    bullseye_rec = record_index.get(bullseye_id)
    contender_rec = record_index.get(contender_id)
    excluded_rec = record_index.get(excluded_id)

    if not all([bullseye_rec, contender_rec, excluded_rec]):
        missing = [
            name for name, rec in [
                ("bullseye_id", bullseye_rec),
                ("contender_id", contender_rec),
                ("excluded_id", excluded_rec),
            ]
            if not rec
        ]
        raise ValueError(f"Records not found for: {', '.join(missing)}")

    run_date = _parse_run_date(status)
    specialty = (
        getattr(status, "target_specialty", None)
        or project.get("target_specialty")
        or ""
    )
    metro = _metro_label(status, project)

    html_str = _brief_env.get_template("sales_brief.html").render(
        run_id=run_id,
        run_date=str(run_date),
        specialty=specialty,
        metro=metro,
        bullseye=_sanitize_sales_angles(bullseye_rec),
        contender=_sanitize_sales_angles(contender_rec),
        excluded=_sanitize_sales_angles(excluded_rec),
    )
    logger.info(
        "Built Sales Brief for run %s: bullseye=%s contender=%s excluded=%s (%d bytes)",
        run_id, bullseye_id, contender_id, excluded_id, len(html_str),
    )
    return html_str.encode("utf-8")


def _build_client_handoff_html(run_id: str, run_directory: Path, status) -> bytes:
    """Build the client-facing Sales Handoff HTML for the client package ZIP.

    Three actionable tiers (Bullseye, Contender, Excluded), no analyst notes,
    no internal scores. Uses handoff_renderer with client_facing=True.
    """
    records = _load_records(run_directory)
    all_reviews = reviews.get_reviews(run_id, run_directory)
    project = projects.read_config_snapshot(run_directory) or {}
    icp = icp_profiles.read_snapshot(run_directory) or {}

    handoff_run = _build_handoff_run(run_id, status, project, icp, records, all_reviews)
    html_str = render_handoff(handoff_run, client_facing=True)
    logger.info(
        "Built client Sales Handoff HTML for run %s: %d accounts, %d bytes",
        run_id, len(handoff_run.accounts), len(html_str),
    )
    return html_str.encode("utf-8")


# ---------------------------------------------------------------------------
# Tier grouping helper (for internal handoff)
# ---------------------------------------------------------------------------

def _group_by_tier(
    records: list[dict],
    all_reviews: dict,
) -> dict[str, list[tuple[dict, dict]]]:
    """Group records by effective tier, sorted by bullseye_score desc within each tier."""
    groups: dict[str, list] = {t: [] for t in record_adapter.TIER_ORDER}

    for rec in records:
        rec_id = record_adapter.get_record_id(rec)
        review = all_reviews.get(rec_id, {})
        tier = record_adapter.effective_tier(rec, review)
        groups[tier if tier in groups else "Manual Review"].append((rec, review))

    for tier_name in record_adapter.TIER_ORDER:
        groups[tier_name].sort(
            key=lambda pair: pair[0].get("bullseye_score") or 0,
            reverse=True,
        )
    return groups


# ---------------------------------------------------------------------------
# Internal builders (client-facing handoff helpers)
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
        _humanize_label(sig.get("signal_label") or sig.get("label") or "")
        for sig in signals
        if (sig.get("signal_state") == "yes" or sig.get("state_inferred"))
        and (sig.get("signal_label") or sig.get("label"))
    ]

    brief = rec.get("call_brief") or {}

    # Why It Matters: the rep-facing value proposition (the sales angle / wedge).
    # The pipeline's why_contact string embeds the internal fit score, so it is
    # deliberately NOT used here — internal ranking data never reaches the client.
    sales_angles = [a for a in _coerce_list(rec.get("sales_angle")) if not _is_first_person_angle(a)]
    why_it_matters = sales_angles or None

    # Example opener: the LLM opener (or discovery question) a rep can use to
    # approach the account. Kept out of Verify, which lists what to uncover.
    opener = (brief.get("opening_line") or "").strip() or (brief.get("discovery_question") or "").strip()

    # Verify: grounded bullets on what to uncover — unconfirmed required signals
    # plus any desirable signal we looked for but could not confirm (not_found).
    verify = _coerce_list(brief.get("missing_to_verify"))
    for sig in signals:
        if sig.get("signal_state") != "not_found":
            continue
        weight = sig.get("positive_weight", 0)
        if isinstance(weight, bool):
            weight = 0
        label = sig.get("signal_label") or sig.get("label")
        if not label or not isinstance(weight, (int, float)) or weight <= 0:
            continue
        label_lower = label.lower()
        if "concierge" in label_lower or "membership" in label_lower:
            continue
        label = _humanize_label(label)
        if label not in verify:
            verify.append(label)

    return Account(
        name=rec.get("practice_name") or rec.get("name") or "Unknown Practice",
        city=_format_city(rec),
        phone=record_adapter.format_phone(rec.get("phone") or rec.get("phone_number") or ""),
        website=_extract_domain(rec.get("website_url") or rec.get("website") or ""),
        evidence_domain=_extract_domain(rec.get("website_url") or rec.get("website") or ""),
        tier=tier,
        confidence=confidence,
        internal_score=int(rec.get("bullseye_score") or 0),
        flags=[],  # practice-level flags not yet in pipeline schema
        # Non-excluded content
        who_to_ask=(brief.get("key_contact") or "") or None,
        why_it_matters=why_it_matters,
        wedge=opener or None,
        confirmed_signals=confirmed_signals,
        verify=verify,
        landmine=_build_landmine(brief),
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


def _build_landmine(brief: dict) -> str | None:
    """Combine confirmed friction risks and the likely objection into one line.

    disqualifier_risk is a list of grounded friction descriptions; likely_objection
    is the LLM-generated push-back a rep should be ready for. Returns None when
    neither is present so the section is omitted.
    """
    parts = _coerce_list(brief.get("disqualifier_risk"))
    objection = (brief.get("likely_objection") or "").strip()
    if objection:
        parts.append(f"Likely objection: {objection}")
    return "  ·  ".join(parts) if parts else None


def _coerce_list(value) -> list[str]:
    """Normalise a field that might be a list, a string, or None to list[str]."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)]


def _humanize_label(label: str) -> str:
    """Convert snake_case signal IDs to readable labels if they contain underscores."""
    if "_" in label:
        return label.replace("_", " ").title()
    return label


# Word-boundary anchored regex for first- and second-person framing detection.
# \b guards prevent false matches inside longer words: "Miami sees" won't match
# "i see", and "outlet's" won't match "let's".
_FIRST_PERSON_RE = _re.compile(
    r"\b(?:"
    r"i\s+noticed|i\s+wanted|i\s+see|i\s+found|i\s+recommend"
    r"|i\s+believe|i\s+think|i\s+suggest|i'd|i've|i\s+would"
    r"|we\s+could|we\s+offer|we\s+can|we\s+should"
    r"|we'd|we'll|we\s+would|we\s+suggest|we\s+recommend"
    r"|you\s+should|you\s+could|you\s+can|you\s+might|you'll"
    r"|our\s+product|our\s+solution|our\s+team"
    r"|let\s+me|let's"
    r")\b",
    _re.IGNORECASE,
)


def _is_first_person_angle(text: str) -> bool:
    """Return True if the angle text contains first- or second-person framing.

    Returns False for non-string input so callers need no type guard.
    """
    if not isinstance(text, str):
        return False
    return bool(_FIRST_PERSON_RE.search(text))


def _sanitize_sales_angles(record: dict) -> dict:
    """Return a shallow copy of the record with first-person sales angles stripped.

    Angles that slip through old prompt versions are removed rather than displayed.
    Third-person angles are kept unchanged.
    """
    angles = _coerce_list(record.get("sales_angle"))
    clean = [a for a in angles if not _is_first_person_angle(a)]
    if len(clean) == len(angles):
        return record  # nothing to strip, return original
    import copy
    out = copy.copy(record)
    out["sales_angle"] = clean
    return out
