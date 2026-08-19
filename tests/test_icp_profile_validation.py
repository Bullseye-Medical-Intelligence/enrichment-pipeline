"""
tests/test_icp_profile_validation.py
API-side ICP profile validation of the profile-level product_context field —
the client-approved product copy injected into sales-hook generation.
Deterministic, no network.
"""

import os
import sys
from pathlib import Path

import pytest

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import icp_profiles  # noqa: E402


def _profile(**over):
    base = {
        "icp_id": "test-profile",
        "name": "Test Profile",
        "version": "test-v1",
        "signals": [{"signal_id": "S-1", "signal_label": "A",
                     "prompt_instruction": "?", "positive_weight": 10}],
    }
    base.update(over)
    return base


def test_absent_product_context_is_valid():
    icp_profiles.validate_icp_profile(_profile())


def test_string_product_context_is_valid():
    icp_profiles.validate_icp_profile(_profile(product_context="A short, approved fact."))


def test_non_string_product_context_rejected():
    with pytest.raises(ValueError, match="product_context.*must be a string"):
        icp_profiles.validate_icp_profile(_profile(product_context=["not", "a", "string"]))


def test_overlong_product_context_rejected():
    """The cap forces tight factual copy — marketing prose invites embellishment."""
    with pytest.raises(ValueError, match="product_context"):
        icp_profiles.validate_icp_profile(_profile(product_context="x" * 701))
    icp_profiles.validate_icp_profile(_profile(product_context="x" * 700))  # at the cap: fine
