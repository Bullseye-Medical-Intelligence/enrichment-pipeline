"""
reports/pdf_report.py
Render the Executive Target Report PDF via WeasyPrint.

Entry point: build_executive_report() — returns raw PDF bytes.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

import record_adapter

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=True,
)


def build_executive_report(
    run_id: str,
    status,
    project: dict,
    icp: dict,
    approved_records: list[dict],
    all_reviews: dict,
    screened: int,
    excluded_count: int,
) -> bytes:
    """Render the Executive Target Report to PDF bytes.

    approved_records should already be filtered (qc_status=approved, non-excluded)
    and sorted by score desc. all_reviews is the full review map {record_id: review}.
    """
    bullseye = [
        r for r in approved_records
        if record_adapter.effective_tier(r, all_reviews).lower() == "bullseye"
    ]
    warm = [
        r for r in approved_records
        if record_adapter.effective_tier(r, all_reviews).lower() in ("strong", "warm")
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
        "warm_count": len(warm),
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
    }

    template = _jinja_env.get_template("executive_target_report.html")
    html_str = template.render(**ctx)

    pdf_bytes = HTML(string=html_str, base_url=str(_STATIC_DIR)).write_pdf()
    logger.info(
        "Generated PDF for run %s: %d bullseye, %d warm, %d bytes",
        run_id, len(bullseye), len(warm), len(pdf_bytes),
    )
    return pdf_bytes


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _prepare_record(rec: dict, review: dict) -> dict:
    """Map a raw enriched record + review to a template-ready dict."""
    city = rec.get("address_city") or ""
    state = rec.get("address_state") or ""
    location = ", ".join(p for p in (city, state) if p) or "—"

    phone_raw = rec.get("phone") or rec.get("phone_number") or ""
    phone_digits = "".join(c for c in phone_raw if c.isdigit() or c in "+()")
    phone_formatted = _format_phone(phone_raw)

    signals_with_evidence = []
    signal_labels = []
    for sig in (rec.get("signals") or []):
        label = sig.get("signal_label") or sig.get("label") or ""
        if label:
            signal_labels.append(label)
        evidence = (sig.get("evidence_text") or "").strip()
        source = sig.get("source_url") or ""
        if evidence:
            signals_with_evidence.append({
                "label": label,
                "evidence": evidence,
                "source_url": source,
            })

    sales_angles = rec.get("sales_angle") or []
    if isinstance(sales_angles, str):
        sales_angles = [sales_angles]

    brief = rec.get("call_brief") or {}

    score = rec.get("bullseye_score")
    confidence = rec.get("confidence_score")
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
        "confidence": confidence,
        "tier": displayed_tier,
        "why_contact": brief.get("why_contact") or "",
        "opening_line": brief.get("opening_line") or "",
        "signal_labels": signal_labels,
        "signals_with_evidence": signals_with_evidence,
        "sales_angles": sales_angles,
        "analyst_note": review.get("analyst_note") or "",
        "override_reason": review.get("override_reason") or "",
        "reviewed_by": review.get("reviewed_by") or "",
    }


def _format_phone(raw: str) -> str:
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return raw or "—"


def _logo_data_uri(variant: str) -> str:
    """Return a base64 data URI for the SVG logo (variant: 'light' or 'dark')."""
    path = _STATIC_DIR / f"logo_{variant}.svg"
    try:
        svg_bytes = path.read_bytes()
        b64 = base64.b64encode(svg_bytes).decode("ascii")
        return f"data:image/svg+xml;base64,{b64}"
    except FileNotFoundError:
        logger.warning("Logo not found: %s", path)
        return ""
