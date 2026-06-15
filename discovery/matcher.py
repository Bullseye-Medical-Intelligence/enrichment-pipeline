"""
matcher.py
Normalization helpers and multi-key registry matching.

Matching priority (highest to lowest):
  1. google_place_id  — exact; Google's stable listing anchor
  2. website_domain   — normalized (no www, no scheme, no port)
  3. phone_digits     — last 10 US digits
  4. name_normalized + address_normalized  — composite deterministic key
"""

import re
from typing import Optional
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_domain(url: str) -> str:
    """Extract bare hostname; strip www, scheme, port, and trailing slash."""
    if not url:
        return ""
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        host = urlparse(url).netloc.lower().split(":")[0]
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def normalize_phone(phone: str) -> str:
    """Strip non-digits; keep last 10 (US standard)."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = re.sub(r"[^\w\s]", " ", (name or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def normalize_address(full_address: str, city: str, state: str, zip_: str) -> str:
    """
    Concatenate available address parts into one normalized string.

    Prefers full_address when present; falls back to city+state+zip.
    """
    if full_address:
        combined = full_address.lower()
    else:
        combined = " ".join(p.lower() for p in (city, state, zip_) if p)
    return re.sub(r"[^\w\s]", " ", combined).strip()


def name_address_key(name_norm: str, addr_norm: str) -> str:
    """Composite lookup key; empty string when both sides are empty."""
    if not name_norm and not addr_norm:
        return ""
    return f"{name_norm}|{addr_norm}"


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

def build_indexes(entries: dict) -> dict:
    """
    Build four O(1) lookup dicts from a registry entries dict.

    Returns {"place_id": {}, "domain": {}, "phone": {}, "name_address": {}}
    where each value maps a normalized key → entry_id.
    """
    by_place_id: dict[str, str] = {}
    by_domain: dict[str, str] = {}
    by_phone: dict[str, str] = {}
    by_name_address: dict[str, str] = {}

    for entry_id, entry in entries.items():
        if pid := (entry.get("google_place_id") or ""):
            by_place_id[pid] = entry_id
        if dom := (entry.get("website_domain") or ""):
            by_domain[dom] = entry_id
        if ph := (entry.get("phone_digits") or ""):
            by_phone[ph] = entry_id
        na = name_address_key(
            entry.get("name_normalized") or "",
            entry.get("address_normalized") or "",
        )
        if na:
            by_name_address[na] = entry_id

    return {
        "place_id": by_place_id,
        "domain": by_domain,
        "phone": by_phone,
        "name_address": by_name_address,
    }


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def find_match(
    row_fields: dict,
    indexes: dict,
) -> tuple[Optional[str], Optional[str]]:
    """
    Return (entry_id, match_basis) for the highest-priority registry match.

    Returns (None, None) when no match is found.
    """
    if pid := row_fields.get("google_place_id"):
        if pid in indexes["place_id"]:
            return indexes["place_id"][pid], "google_place_id"

    if dom := row_fields.get("website_domain"):
        if dom in indexes["domain"]:
            return indexes["domain"][dom], "website_domain"

    ph = row_fields.get("phone_digits") or ""
    if len(ph) >= 10 and ph in indexes["phone"]:
        return indexes["phone"][ph], "phone"

    na = name_address_key(
        row_fields.get("name_normalized") or "",
        row_fields.get("address_normalized") or "",
    )
    if na and na in indexes["name_address"]:
        return indexes["name_address"][na], "name_address"

    return None, None
