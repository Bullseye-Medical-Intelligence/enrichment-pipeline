"""
handoff_renderer/renderer.py
Pure-function HTML renderer for the Bullseye Sales Handoff.

Exposes one public function:
    render_handoff(run: HandoffRun, client_facing: bool = True) -> str
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .models import Account, Confidence, HandoffRun, Tier

_TEMPLATE_DIR = Path(__file__).parent / "templates"

_TIER_DISPLAY = {
    Tier.BULLSEYE: {
        "section": "Call First",
        "subcount": "work these now",
        "pill": "Bullseye",
        "dot": "b",
    },
    Tier.CONTENDER: {
        "section": "Validate",
        "subcount": "qualify the buyer first",
        "pill": "Validate",
        "dot": "c",
    },
    Tier.EXCLUDED: {
        "section": "Suppress",
        "subcount": "protect field time",
        "pill": "Suppress",
        "dot": "e",
    },
}

_CONFIDENCE_SEGMENTS = {
    Confidence.HIGH: 3,
    Confidence.MEDIUM: 2,
    Confidence.LOW: 1,
}

_CONFIDENCE_LABEL = {
    Confidence.HIGH: "High",
    Confidence.MEDIUM: "Medium",
    Confidence.LOW: "Low",
}

_CONFIDENCE_ORDER = {
    Confidence.HIGH: 0,
    Confidence.MEDIUM: 1,
    Confidence.LOW: 2,
}

_TIER_ORDER = [Tier.BULLSEYE, Tier.CONTENDER, Tier.EXCLUDED]


def _bold_md(text: Optional[str]) -> Markup:
    """HTML-escape text, then convert **x** spans to <b>x</b>."""
    if not text:
        return Markup("")
    escaped = html.escape(text)
    converted = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    return Markup(converted)


def _make_jinja_env() -> Environment:
    """Build the Jinja2 environment with autoescape and the bold_md filter."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["bold_md"] = _bold_md
    return env


_jinja_env = _make_jinja_env()


def _sorted_accounts(accounts: list[Account]) -> list[Account]:
    """Sort by confidence HIGH > MEDIUM > LOW, then by internal_score descending."""
    return sorted(accounts, key=lambda a: (
        _CONFIDENCE_ORDER[a.confidence],
        -(a.internal_score or 0),
    ))


def _format_date(d) -> str:
    """Format a date as 'Month DD, YYYY'."""
    return d.strftime("%B %d, %Y")


def _prepare_account(acct: Account, qc_reviewer: str, client_facing: bool) -> dict:
    """Build the template context dict for one account.

    internal_score is excluded when client_facing=True.
    """
    tier_info = _TIER_DISPLAY[acct.tier]
    result = {
        "name": acct.name,
        "city": acct.city,
        "phone": acct.phone,
        "website": acct.website,
        "evidence_domain": acct.evidence_domain,
        "tier": acct.tier,
        "tier_info": tier_info,
        "confidence": acct.confidence,
        "conf_label": _CONFIDENCE_LABEL[acct.confidence],
        "conf_segments": _CONFIDENCE_SEGMENTS[acct.confidence],
        "flags": acct.flags,
        "has_flags": bool(acct.flags),
        "why_it_matters": acct.why_it_matters,
        "wedge": acct.wedge,
        "confirmed_signals": acct.confirmed_signals,
        "verify": acct.verify,
        "landmine": acct.landmine,
        "gate_fired": acct.gate_fired,
        "evidence": acct.evidence,
        "suppress_reason": acct.suppress_reason,
        "revisit_if": acct.revisit_if,
        "qc_reviewer": qc_reviewer,
    }
    if not client_facing:
        result["internal_score"] = acct.internal_score
    return result


def render_handoff(run: HandoffRun, client_facing: bool = True) -> str:
    """Render a HandoffRun to a self-contained HTML string.

    When client_facing=True (default), internal_score is never emitted anywhere
    in the output — not in text, not in comments, not in data attributes.
    """
    grouped: dict[Tier, list[Account]] = {t: [] for t in _TIER_ORDER}
    for acct in run.accounts:
        grouped[acct.tier].append(acct)

    sections = []
    for tier in _TIER_ORDER:
        accts = _sorted_accounts(grouped[tier])
        if not accts:
            continue
        sections.append({
            "tier": tier,
            "tier_info": _TIER_DISPLAY[tier],
            "accounts": [_prepare_account(a, run.qc_reviewer, client_facing) for a in accts],
        })

    counts = {t: len(grouped[t]) for t in _TIER_ORDER}

    template = _jinja_env.get_template("sales_handoff.html")
    return template.render(
        run=run,
        sections=sections,
        counts=counts,
        total=len(run.accounts),
        run_date=_format_date(run.run_date),
        expiry_date=_format_date(run.expiry_date),
        client_facing=client_facing,
        Tier=Tier,
    )
