"""
reports/pdf_report.py
Render the Executive Target Report as a self-contained HTML file.

The report template embeds its own CSS and inlines the logo as a data URI, so
the rendered HTML opens in any browser with no external assets and no native
dependencies. Clients who want a PDF can print from the browser; the template
carries @page / page-break rules for that path.

Entry point: build_executive_report_html() — returns UTF-8 HTML bytes.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

import record_adapter
from sales_export import _is_first_person_angle

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_MAIN_STATIC_DIR = Path(__file__).parent.parent / "static"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=True,
)


def build_executive_report_html(
    run_id: str,
    status,
    project: dict,
    icp: dict,
    approved_records: list[dict],
    all_reviews: dict,
    screened: int,
    excluded_count: int,
) -> bytes:
    """Render the Executive Target Report to self-contained HTML bytes.

    approved_records should already be filtered (qc_status=approved, non-excluded)
    and sorted by score desc. all_reviews is the full review map {record_id: review}.
    """
    bullseye = [
        r for r in approved_records
        if record_adapter.effective_tier(r, all_reviews).lower() == "bullseye"
    ]
    contender = [
        r for r in approved_records
        if record_adapter.effective_tier(r, all_reviews).lower() == "contender"
    ]

    geography = status.target_geography or project.get("target_geography") or []
    if isinstance(geography, list):
        geography = ", ".join(geography)

    ctx = {
        "run_id": run_id,
        "client_name": status.client_name or project.get("client_name") or "—",
        "product_name": status.product_name or project.get("product_name") or "—",
        "target_specialty": status.target_specialty or project.get("target_specialty") or "—",
        "geography": geography or "—",
        "icp_name": status.icp_profile_name or icp.get("name") or "—",
        "icp_version": status.icp_profile_version or icp.get("version") or "—",
        "generated_date": datetime.now(timezone.utc).strftime("%B %d, %Y"),
        "screened": screened,
        "bullseye_count": len(bullseye),
        "contender_count": len(contender),
        "excluded_count": excluded_count,
        "bullseye_records": [
            _prepare_record(r, all_reviews.get(record_adapter.get_record_id(r), {}))
            for r in bullseye
        ],
        "methodology": (
            "Bullseye Medical Intelligence reviewed publicly available physician and "
            "practice-level online signals to identify accounts that appear commercially "
            "relevant for outreach. The review focused on observable fit indicators, "
            "service-line alignment, exclusion risks, evidence strength, and rep-facing "
            "sales actionability. Bullseye does not use PHI, patient records, claims files, "
            "appointment data, EMR access, login-gated systems, or private patient-level "
            "datasets."
        ),
        "logo_light": _logo_data_uri("light"),
        "logo_dark": _logo_data_uri("dark"),
        "favicon_uri": _mark_data_uri("bullseye-favicon.svg"),
    }

    template = _jinja_env.get_template("executive_target_report.html")
    html_str = template.render(**ctx)
    logger.info(
        "Generated Executive Target Report HTML for run %s: %d bullseye, %d contender, %d bytes",
        run_id, len(bullseye), len(contender), len(html_str),
    )
    return html_str.encode("utf-8")


def build_bullseye_cards_html(
    run_id: str,
    status,
    project: dict,
    icp: dict,
    approved_records: list[dict],
    all_reviews: dict,
    screened: int,
    excluded_count: int,
) -> bytes:
    """Render the Bullseye Target Report as a self-contained HTML file.

    Returns UTF-8 encoded bytes suitable for direct inclusion in the client ZIP.
    The HTML file is standalone — it embeds all CSS and uses Google Fonts via CDN.
    approved_records should already be filtered and sorted by score desc.
    """
    bullseye = [
        r for r in approved_records
        if record_adapter.effective_tier(r, all_reviews).lower() == "bullseye"
    ]

    geography = status.target_geography or project.get("target_geography") or []
    if isinstance(geography, list):
        geography = ", ".join(geography)

    ctx = {
        "run_id": run_id,
        "client_name": status.client_name or project.get("client_name") or "—",
        "product_name": status.product_name or project.get("product_name") or "—",
        "target_specialty": status.target_specialty or project.get("target_specialty") or "—",
        "geography": geography or "—",
        "icp_name": status.icp_profile_name or icp.get("name") or "—",
        "icp_version": status.icp_profile_version or icp.get("version") or "—",
        "generated_date": datetime.now(timezone.utc).strftime("%B %d, %Y"),
        "screened": screened,
        "bullseye_records": [
            _prepare_record(r, all_reviews.get(record_adapter.get_record_id(r), {}))
            for r in bullseye
        ],
        "methodology_short": (
            "Based on publicly available signals only. "
            "No PHI or patient data used."
        ),
        "mark_uri": _mark_data_uri("bullseye-mark.svg"),
        "favicon_uri": _mark_data_uri("bullseye-favicon.svg"),
    }

    template = _jinja_env.get_template("bullseye_cards.html")
    html_str = template.render(**ctx)
    logger.info(
        "Generated Bullseye HTML report for run %s: %d accounts, %d bytes",
        run_id, len(bullseye), len(html_str),
    )
    return html_str.encode("utf-8")


def _format_location(rec: dict) -> str:
    """Return 'City, ST' from address fields, or '—' if both are absent."""
    city = rec.get("address_city") or ""
    state = rec.get("address_state") or ""
    return ", ".join(p for p in (city, state) if p) or "—"


def _extract_confirmed_signals(signals: list) -> list[dict]:
    """Return [{label, inferred}] for every confirmed or inferred signal."""
    result = []
    for sig in signals:
        label = sig.get("signal_label") or sig.get("label") or ""
        inferred = bool(sig.get("state_inferred", False))
        if (sig.get("signal_state") == "yes" or inferred) and label:
            result.append({"label": label, "inferred": inferred})
    return result


def _prepare_sales_record(rec: dict, review: dict) -> dict:
    """Map a raw enriched record + review to a sales-handoff template dict.

    Internal-only: includes analyst notes, full call brief, tier/exclusion context.
    No numeric scores, no raw evidence text.
    """
    signals = rec.get("signals") or []
    brief = rec.get("call_brief") or {}
    sales_angles = rec.get("sales_angle") or []
    if isinstance(sales_angles, str):
        sales_angles = [sales_angles]
    sales_angles = [a for a in sales_angles if not _is_first_person_angle(a)]

    return {
        "name": rec.get("practice_name") or rec.get("name") or "Unknown Practice",
        "specialty": rec.get("specialty") or rec.get("target_specialty") or "—",
        "location": _format_location(rec),
        "phone_formatted": record_adapter.format_phone(
            rec.get("phone") or rec.get("phone_number") or ""
        ),
        "website": rec.get("website_url") or rec.get("website") or "",
        # Use displayed_tier so low-score Contenders promoted to Manual Review
        # carry the correct label on their card, matching the section they're in.
        "tier": record_adapter.displayed_tier(rec, review),
        "confidence_band": rec.get("confidence_band") or "—",
        "tier_cap_reason": rec.get("tier_cap_reason") or "",
        "exclusion_reason": rec.get("exclusion_reason") or "",
        "confirmed_signals": _extract_confirmed_signals(signals),
        "why_contact": brief.get("why_contact") or "",
        "key_contact": brief.get("key_contact") or "",
        "provider_names": rec.get("provider_names") or [],
        "opening_line": brief.get("opening_line") or "",
        "likely_objection": brief.get("likely_objection") or "",
        "discovery_question": brief.get("discovery_question") or "",
        "hours_of_operation": brief.get("hours_of_operation") or "",
        "sales_angles": sales_angles,
        # Analyst-authored fields (analyst_note etc.) are deliberately NOT
        # copied into template context: these reports are client-facing
        # (client package ZIP + published briefs) and internal notes must
        # never leak into them.
        "override_reason": review.get("override_reason") or "",
        "override_tier": review.get("override_tier") or "",
        "qc_status": review.get("qc_status") or "",
        "reviewed_by": review.get("reviewed_by") or "",
    }


def build_sales_handoff_html(
    run_id: str,
    status,
    project: dict,
    icp: dict,
    grouped_records: dict,
    screened: int,
) -> bytes:
    """Render the internal Sales Handoff report to self-contained HTML bytes.

    grouped_records maps tier_name -> list of (rec, review) pairs. All five
    tiers, analyst notes, full call brief. Internal-only.
    """
    geography = status.target_geography or project.get("target_geography") or []
    if isinstance(geography, list):
        geography = ", ".join(geography)

    tier_sections = []
    for tier_name in record_adapter.TIER_ORDER:
        pairs = grouped_records.get(tier_name, [])
        if not pairs:
            continue
        tier_sections.append({
            "tier": tier_name,
            "count": len(pairs),
            "records": [_prepare_sales_record(rec, review) for rec, review in pairs],
        })

    ctx = {
        "run_id": run_id,
        "client_name": status.client_name or project.get("client_name") or "—",
        "product_name": status.product_name or project.get("product_name") or "—",
        "target_specialty": status.target_specialty or project.get("target_specialty") or "—",
        "geography": geography or "—",
        "icp_name": status.icp_profile_name or icp.get("name") or "—",
        "icp_version": status.icp_profile_version or icp.get("version") or "—",
        "generated_date": datetime.now(timezone.utc).strftime("%B %d, %Y"),
        "screened": screened,
        "tier_sections": tier_sections,
        "logo_light": _logo_data_uri("light"),
        "mark_uri": _mark_data_uri("bullseye-mark.svg"),
        "favicon_uri": _mark_data_uri("bullseye-favicon.svg"),
    }

    template = _jinja_env.get_template("sales_handoff.html")
    html_str = template.render(**ctx)
    logger.info(
        "Generated Sales Handoff HTML for run %s: %d tier sections, %d bytes",
        run_id, len(tier_sections), len(html_str),
    )
    return html_str.encode("utf-8")


def _prepare_record(rec: dict, review: dict) -> dict:
    """Map a raw enriched record + review to a template-ready dict."""
    location = _format_location(rec)

    phone_raw = rec.get("phone") or rec.get("phone_number") or ""
    phone_digits = "".join(c for c in phone_raw if c.isdigit() or c in "+()")
    phone_formatted = record_adapter.format_phone(phone_raw)

    signals = rec.get("signals") or []
    signals_with_evidence = []
    signal_labels = []

    for sig in signals:
        label = sig.get("signal_label") or sig.get("label") or ""
        state_val = sig.get("signal_state", "")

        if label:
            signal_labels.append(label)

        evidence = (sig.get("evidence_text") or "").strip()
        source = sig.get("source_url") or ""
        if evidence:
            state_display = "present" if state_val == "yes" else ("absent" if state_val == "no" else "")
            signals_with_evidence.append({
                "signal_label": label,
                "signal_state": state_display,
                "evidence_text": evidence,
                "source_url": source,
            })

    confirmed_signals = _extract_confirmed_signals(signals)

    # Signal coverage: % of confirmed/inferred out of total evaluated signals
    total_signals = len(signals)
    yes_count = sum(
        1 for s in signals
        if s.get("signal_state") == "yes" or s.get("state_inferred")
    )
    signal_coverage = int(yes_count / total_signals * 100) if total_signals > 0 else 0

    # Fit score (pipeline field, 0-100)
    fit_score_raw = rec.get("fit_signal_score")
    fit_score = min(int(fit_score_raw), 100) if fit_score_raw is not None else None

    # Exclusion risk: count friction/missing-required red flags
    risk_count = 0
    for sig in signals:
        state_val = sig.get("signal_state", "")
        weight = sig.get("positive_weight", 0)
        # positive_weight may be a bool on older records (pre-fix bug); treat as 0
        if isinstance(weight, bool):
            weight = 0
        required = sig.get("required_for_bullseye", False)
        if isinstance(weight, (int, float)) and weight < 0 and state_val == "yes":
            risk_count += 1  # confirmed friction signal
        elif required and state_val == "no":
            risk_count += 1  # confirmed-absent must-have

    if risk_count == 0:
        exclusion_risk, exclusion_risk_pct = "Low", 92
    elif risk_count == 1:
        exclusion_risk, exclusion_risk_pct = "Moderate", 55
    else:
        exclusion_risk, exclusion_risk_pct = "Elevated", 20

    sales_angles = rec.get("sales_angle") or []
    if isinstance(sales_angles, str):
        sales_angles = [sales_angles]
    sales_angles = [a for a in sales_angles if not _is_first_person_angle(a)]

    brief = rec.get("call_brief") or {}

    score = rec.get("bullseye_score")
    # Client-facing: show the qualitative confidence band, never the number.
    confidence_band = rec.get("confidence_band") or "—"
    displayed_tier = review.get("override_tier") or rec.get("target_tier", "—")

    return {
        "name": rec.get("practice_name") or rec.get("name") or "Unknown Practice",
        "specialty": rec.get("specialty") or rec.get("target_specialty") or "—",
        "location": location,
        "phone": phone_digits,
        "phone_formatted": phone_formatted,
        "website": rec.get("website_url") or rec.get("website") or "",
        "score": score,
        "score_pct": min(int(score), 100) if score is not None else 0,
        "confidence_band": confidence_band,
        "tier": displayed_tier,
        "why_contact": brief.get("why_contact") or "",
        "opening_line": brief.get("opening_line") or "",
        "signal_labels": signal_labels,
        "signals_with_evidence": signals_with_evidence,
        "confirmed_signals": confirmed_signals,
        "sales_angles": sales_angles,
        "fit_score": fit_score,
        "signal_coverage": signal_coverage,
        "exclusion_risk": exclusion_risk,
        "exclusion_risk_pct": exclusion_risk_pct,
        # Analyst-authored fields deliberately omitted — client-facing report.
        "override_reason": review.get("override_reason") or "",
        "reviewed_by": review.get("reviewed_by") or "",
    }



def _logo_data_uri(variant: str) -> str:
    """Return a base64 data URI for the SVG logo (variant: 'light' or 'dark')."""
    path = _STATIC_DIR / f"logo_{variant}.svg"
    try:
        svg_bytes = path.read_bytes()
        b64 = base64.b64encode(svg_bytes).decode("ascii")
        return f"data:image/svg+xml;base64,{b64}"
    except OSError:
        logger.warning("Logo not accessible: %s", path)
        return ""


def _mark_data_uri(filename: str) -> str:
    """Return a base64 data URI for a canonical SVG asset from the main static dir."""
    path = _MAIN_STATIC_DIR / filename
    try:
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/svg+xml;base64,{b64}"
    except OSError:
        logger.warning("SVG asset not accessible: %s", path)
        return ""
