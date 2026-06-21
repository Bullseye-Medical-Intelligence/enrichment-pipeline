"""
handoff_renderer/models.py
Data contract for the Bullseye Sales Handoff renderer.

These dataclasses are normative: downstream callers must populate them
from enrichment output before passing to render_handoff(). The renderer
never reads files, calls APIs, or scores records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Optional


class Tier(Enum):
    BULLSEYE = "BULLSEYE"
    CONTENDER = "CONTENDER"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    EXCLUDED = "EXCLUDED"


class Confidence(Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass
class Account:
    """One screened practice account."""
    name: str
    city: str              # display: "Allen (75013), TX"
    phone: str
    website: str           # display domain, e.g. "nortexpsychiatry.com"
    evidence_domain: str   # shown in QC line
    tier: Tier
    confidence: Confidence
    internal_score: int    # 0-100; NEVER rendered when client_facing=True
    flags: list[str] = field(default_factory=list)  # e.g. ["Multi-site — buying may be central"]

    # Populated for BULLSEYE and CONTENDER
    who_to_ask: Optional[str] = None   # e.g. "Ask for Dr. Jane Smith or Dr. Patel"
    why_it_matters: Optional[list[str]] = None
    wedge: Optional[str] = None
    confirmed_signals: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)
    landmine: Optional[str] = None  # supports **bold** markdown span
    cap_reason: Optional[str] = None       # why not Bullseye; Contender accounts only
    hours_of_operation: Optional[str] = None  # office hours from call_brief
    website_url: Optional[str] = None       # full URL for href; website holds display domain

    # Populated for EXCLUDED only
    gate_fired: Optional[str] = None
    evidence: Optional[str] = None
    suppress_reason: Optional[str] = None
    revisit_if: Optional[str] = None

    # Derived presentation fields (populated by sales_export._record_to_account)
    hook: Optional[str] = None                         # 1-sentence collapsed-card hook
    motion: Optional[str] = None                       # "EXPAND" or "NEW LOGO"
    validate_sublabel: Optional[str] = None            # "Ready" or "Discovery" (Contender only)
    verification_step: Optional[str] = None            # concrete action text (NV only)
    not_found_signals: list[str] = field(default_factory=list)  # positive-weight not_found labels



@dataclass
class HandoffRun:
    """Top-level run context for one client deliverable."""
    product_name: str       # "Neurolief"
    client_name: str        # "Proliv Rx"
    run_date: date          # 2026-06-01
    specialty_label: str    # "Cash-Pay Interventional Psychiatry"
    metro: str              # "Dallas"
    icp_version: str        # "neurolief-prolivrx-cashpay-v1"
    qc_reviewer: str        # "rajiv"
    accounts: list[Account]
    pattern_insight: Optional[str] = None  # None → omit pattern block entirely
    expiry_date: Optional[date] = None     # defaults to run_date + 30 days

    def __post_init__(self) -> None:
        if self.expiry_date is None:
            self.expiry_date = self.run_date + timedelta(days=30)
