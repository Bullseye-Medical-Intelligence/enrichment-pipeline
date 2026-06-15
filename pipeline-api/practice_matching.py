"""
practice_matching.py
Single source of truth for API-safe practice identity matching.

Normalization + the match-priority decision used by both the (legacy)
discovery.py and registry_update.py live here so the two never drift. This module
is deliberately self-contained:

  - It MUST NOT import enrichment-pipeline internals.
  - It MUST NOT import the repo-root `discovery` package (subprocess-only boundary).
  - It has no side effects, no I/O, no config dependency.

Match priority (highest to lowest) — this order is a contract:
  1. google_place_id   (a.k.a. place_id)
  2. normalized website domain
  3. normalized phone   (last 10 digits, only when >= 10)
  4. normalized practice name + normalized address
NPI is a supporting identifier only — it is never a match key.

See MATCHING_NOTES.md for the broader context.
"""

import re
from typing import Optional
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_domain(url: str) -> str:
    """Extract bare hostname from a URL; strip scheme, www., and port."""
    if not url:
        return ""
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        host = urlparse(url).netloc.lower()
        host = host.split(":")[0]  # strip port
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def normalize_phone(phone: str) -> str:
    """Strip everything except digits; keep the last 10 (US standard)."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = re.sub(r"[^\w\s]", " ", (name or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def normalize_address(full_address: str, city: str, state: str, zip_: str) -> str:
    """Concatenate available address parts into one normalized string."""
    if full_address:
        combined = full_address.lower()
    else:
        combined = " ".join(p.lower() for p in (city, state, zip_) if p)
    return re.sub(r"[^\w\s]", " ", combined).strip()


def name_address_key(name_norm: str, addr_norm: str) -> str:
    """Composite lookup key; empty when both sides are empty."""
    if not name_norm and not addr_norm:
        return ""
    return f"{name_norm}|{addr_norm}"


# ---------------------------------------------------------------------------
# Indexing & matching
# ---------------------------------------------------------------------------

def build_match_indexes(entries: dict) -> dict:
    """Build the four lookup dicts from registry entries (entry_id → entry dict)."""
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
    return {"place_id": by_place_id, "domain": by_domain,
            "phone": by_phone, "name_address": by_name_address}


def match_candidates(fields: dict, indexes: dict) -> list[tuple[str, str]]:
    """Return (basis, entry_id) pairs in strict priority order.

    The single place the match priority is encoded. Both find_match() and
    match_with_ambiguity() build on this so they can never disagree about order.
    """
    out: list[tuple[str, str]] = []
    pid = fields.get("google_place_id") or ""
    if pid and pid in indexes["place_id"]:
        out.append(("google_place_id", indexes["place_id"][pid]))
    dom = fields.get("website_domain") or ""
    if dom and dom in indexes["domain"]:
        out.append(("website_domain", indexes["domain"][dom]))
    ph = fields.get("phone_digits") or ""
    if len(ph) >= 10 and ph in indexes["phone"]:
        out.append(("phone", indexes["phone"][ph]))
    na = name_address_key(fields.get("name_normalized") or "",
                          fields.get("address_normalized") or "")
    if na and na in indexes["name_address"]:
        out.append(("name_address", indexes["name_address"][na]))
    return out


def find_match(fields: dict, indexes: dict) -> tuple[Optional[str], Optional[str]]:
    """Return (entry_id, match_basis) for the best (highest-priority) match.

    First-priority-wins: if several identifiers match different entries, the
    highest-priority one is returned. Used by discovery's delta detection.
    """
    candidates = match_candidates(fields, indexes)
    if candidates:
        basis, entry_id = candidates[0]
        return entry_id, basis
    return None, None


def match_with_ambiguity(fields: dict, indexes: dict) -> tuple[Optional[str], bool]:
    """Return (entry_id, ambiguous).

    entry_id is the single matched entry, or None. ambiguous is True when
    different identifiers point to *different* existing entries — the caller
    rejects those (needs_manual_merge) rather than guessing. Used by registry update.
    """
    distinct = list(dict.fromkeys(entry_id for _basis, entry_id in match_candidates(fields, indexes)))
    if not distinct:
        return None, False
    if len(distinct) > 1:
        return None, True
    return distinct[0], False
