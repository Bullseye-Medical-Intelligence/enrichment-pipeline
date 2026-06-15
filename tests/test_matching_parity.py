"""
tests/test_matching_parity.py
Regression guard for the DUPLICATED practice-matching logic.

Normalization + match priority is currently copied in two API-level modules
(pipeline-api/discovery.py and pipeline-api/registry_update.py). This test pins
the copies together: if someone changes one without the other, it fails. See
pipeline-api/MATCHING_NOTES.md for why the duplication exists and the long-term
fix (a shared API-safe matching utility).

The local pipeline-api/discovery.py is loaded by explicit file path under a
unique module name, because the bare name `discovery` also resolves to the
repo-root discovery package (a different module).
"""

import importlib.util
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

import registry_update  # noqa: E402  (the local API module; unambiguous name)


def _load_local_discovery():
    """Load pipeline-api/discovery.py by path under a unique, non-colliding name."""
    spec = importlib.util.spec_from_file_location(
        "pa_discovery_local", str(_API_DIR / "discovery.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_DISC = _load_local_discovery()


# ---------------------------------------------------------------------------
# Normalization parity
# ---------------------------------------------------------------------------

_DOMAINS = [
    "https://www.Alpha-Clinic.com/services",
    "http://beta.org:8080",
    "gamma.net",
    "",
    "not a url",
]
_PHONES = ["(404) 555-1000", "404.555.1000", "1-404-555-1000", "555-1000", ""]
_NAMES = ["Alpha Women's Health, P.C.", "  Beta   OB/GYN  ", "Gamma-Care", ""]
_ADDRS = [
    ("123 Main St, Suite 4", "Atlanta", "GA", "30301"),
    ("", "Decatur", "GA", "30030"),
    ("", "", "", ""),
]


@pytest.mark.parametrize("url", _DOMAINS)
def test_normalize_domain_parity(url):
    assert _DISC._normalize_domain(url) == registry_update._normalize_domain(url)


@pytest.mark.parametrize("phone", _PHONES)
def test_normalize_phone_parity(phone):
    assert _DISC._normalize_phone(phone) == registry_update._normalize_phone(phone)


@pytest.mark.parametrize("name", _NAMES)
def test_normalize_name_parity(name):
    assert _DISC._normalize_name(name) == registry_update._normalize_name(name)


@pytest.mark.parametrize("addr", _ADDRS)
def test_normalize_address_parity(addr):
    full, city, state, zip_ = addr
    assert (_DISC._normalize_address(full, city, state, zip_)
            == registry_update._normalize_address(full, city, state, zip_))


def test_name_address_key_parity():
    for name, addr in [("alpha", "123 main"), ("", "x"), ("", "")]:
        assert (_DISC._name_address_key(name, addr)
                == registry_update._name_address_key(name, addr))


# ---------------------------------------------------------------------------
# Match-priority parity
# ---------------------------------------------------------------------------

def _entry(eid, **f):
    base = {"entry_id": eid, "google_place_id": "", "website_domain": "",
            "phone_digits": "", "name_normalized": "", "address_normalized": ""}
    base.update(f)
    return base


def _fields(**f):
    base = {"google_place_id": "", "website_domain": "", "phone_digits": "",
            "name_normalized": "", "address_normalized": ""}
    base.update(f)
    return base


@pytest.mark.parametrize("entry_kwargs,field_kwargs,expected", [
    # place_id wins
    (dict(google_place_id="PID-1"), dict(google_place_id="PID-1"), "E"),
    # domain match
    (dict(website_domain="alpha.com"), dict(website_domain="alpha.com"), "E"),
    # phone match (>= 10 digits)
    (dict(phone_digits="4045551000"), dict(phone_digits="4045551000"), "E"),
    # name+address match
    (dict(name_normalized="alpha", address_normalized="123 main"),
     dict(name_normalized="alpha", address_normalized="123 main"), "E"),
    # no match
    (dict(website_domain="alpha.com"), dict(website_domain="other.com"), None),
])
def test_match_priority_parity(entry_kwargs, field_kwargs, expected):
    entries = {"E": _entry("E", **entry_kwargs)}
    fields = _fields(**field_kwargs)

    disc_indexes = _DISC._build_indexes(entries)
    ru_indexes = registry_update._build_indexes(entries)
    assert disc_indexes == ru_indexes  # index builders agree

    disc_id, _basis = _DISC.find_match(fields, disc_indexes, entries)
    ru_id, _ambiguous = registry_update.match_entry(fields, ru_indexes)
    assert disc_id == ru_id == expected


def test_phone_under_ten_digits_not_matched_in_both():
    """A short phone is not a match key in either implementation."""
    entries = {"E": _entry("E", phone_digits="5551000")}
    fields = _fields(phone_digits="5551000")
    disc_id, _ = _DISC.find_match(fields, _DISC._build_indexes(entries), entries)
    ru_id, _ = registry_update.match_entry(fields, registry_update._build_indexes(entries))
    assert disc_id is None and ru_id is None
