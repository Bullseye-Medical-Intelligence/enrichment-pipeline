"""
handoff_renderer — Bullseye Sales Handoff renderer.

Public API:
    render_handoff(run, client_facing=True) -> str
    HandoffRun, Account, Tier, Confidence
"""

from .models import Account, Confidence, HandoffRun, Tier
from .renderer import render_handoff

__all__ = ["render_handoff", "HandoffRun", "Account", "Tier", "Confidence"]
