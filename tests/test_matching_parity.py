"""
tests/test_matching_parity.py
Guards that practice matching has a single source of truth: practice_matching.py.

registry_update.py imports its normalization + matching helpers from
practice_matching. This test asserts:
  1. registry_update's helpers ARE the shared practice_matching functions (identity),
     so they cannot drift.
  2. The shared matcher's behavior is correct across representative inputs and the
     fixed match priority (place_id → domain → phone → name+address; NPI never a key).
"""

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "pipeline-api"

os.environ.setdefault("PIPELINE_API_KEY", "test-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO_ROOT))

sys.path.insert(0, str(_API_DIR))

import practice_matching as pm  # noqa: E402  (the single source of truth)
import registry_update  # noqa: E402


# ---------------------------------------------------------------------------
# Single source of truth — registry_update delegates to practice_matching
# ---------------------------------------------------------------------------

def test_registry_update_uses_shared_helpers():
    assert registry_update._normalize_domain is pm.normalize_domain
    assert registry_update._normalize_phone is pm.normalize_phone
    assert registry_update._normalize_name is pm.normalize_name
    assert registry_update._normalize_address is pm.normalize_address
    assert registry_update._name_address_key is pm.name_address_key
    assert registry_update._build_indexes is pm.build_match_indexes
    assert registry_update.match_entry is pm.match_with_ambiguity


# ---------------------------------------------------------------------------
# Shared matcher behavior (edge cases preserved)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.Alpha-Clinic.com/services", "alpha-clinic.com"),
    ("http://beta.org:8080", "beta.org"),
    ("gamma.net", "gamma.net"),
    ("", ""),
])
def test_normalize_domain(url, expected):
    assert pm.normalize_domain(url) == expected


@pytest.mark.parametrize("phone,expected", [
    ("(404) 555-1000", "4045551000"),
    ("1-404-555-1000", "4045551000"),   # keeps last 10
    ("555-1000", "5551000"),            # under 10 digits, unchanged
    ("", ""),
])
def test_normalize_phone(phone, expected):
    assert pm.normalize_phone(phone) == expected


def test_normalize_name_and_address():
    assert pm.normalize_name("Alpha Women's Health, P.C.") == "alpha women s health p c"
    assert pm.normalize_address("123 Main St, Suite 4", "", "", "") == "123 main st  suite 4"
    assert pm.normalize_address("", "Decatur", "GA", "30030") == "decatur ga 30030"


def test_name_address_key():
    assert pm.name_address_key("alpha", "123 main") == "alpha|123 main"
    assert pm.name_address_key("", "") == ""


def _entry(eid, **f):
    base = {"google_place_id": "", "website_domain": "", "phone_digits": "",
            "name_normalized": "", "address_normalized": ""}
    base.update(f)
    return {eid: base}


def _fields(**f):
    base = {"google_place_id": "", "website_domain": "", "phone_digits": "",
            "name_normalized": "", "address_normalized": ""}
    base.update(f)
    return base


@pytest.mark.parametrize("entry_kwargs,field_kwargs,expected", [
    (dict(google_place_id="PID-1"), dict(google_place_id="PID-1"), "E"),
    (dict(website_domain="alpha.com"), dict(website_domain="alpha.com"), "E"),
    (dict(phone_digits="4045551000"), dict(phone_digits="4045551000"), "E"),
    (dict(name_normalized="alpha", address_normalized="123 main"),
     dict(name_normalized="alpha", address_normalized="123 main"), "E"),
    (dict(website_domain="alpha.com"), dict(website_domain="other.com"), None),
])
def test_find_match_priority(entry_kwargs, field_kwargs, expected):
    indexes = pm.build_match_indexes(_entry("E", **entry_kwargs))
    entry_id, _basis = pm.find_match(_fields(**field_kwargs), indexes)
    assert entry_id == expected


def test_phone_under_ten_digits_not_a_key():
    indexes = pm.build_match_indexes(_entry("E", phone_digits="5551000"))
    assert pm.find_match(_fields(phone_digits="5551000"), indexes) == (None, None)
    assert pm.match_with_ambiguity(_fields(phone_digits="5551000"), indexes) == (None, False)


def test_first_priority_wins_in_find_match():
    """place_id (entry A) outranks phone (entry B): find_match returns A."""
    entries = {"A": {"google_place_id": "PID"}, "B": {"phone_digits": "4045551000"}}
    indexes = pm.build_match_indexes(entries)
    fields = _fields(google_place_id="PID", phone_digits="4045551000")
    assert pm.find_match(fields, indexes) == ("A", "google_place_id")


def test_conflicting_identifiers_are_ambiguous():
    """Same conflict via match_with_ambiguity → no match, flagged ambiguous."""
    entries = {"A": {"google_place_id": "PID"}, "B": {"phone_digits": "4045551000"}}
    indexes = pm.build_match_indexes(entries)
    fields = _fields(google_place_id="PID", phone_digits="4045551000")
    assert pm.match_with_ambiguity(fields, indexes) == (None, True)


def test_npi_is_not_a_match_key():
    """An NPI-only field set matches nothing — NPI is supporting only."""
    indexes = pm.build_match_indexes(_entry("E", website_domain="alpha.com"))
    assert pm.find_match(_fields(), indexes) == (None, None)
