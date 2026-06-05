"""
customer_suppression.py
Per-project customer suppression list: identify and exclude existing customers
before any crawl or LLM spend (Step 1c).

When a project has an existing_customers.csv, records that match a known customer
are marked _customer_suppressed and excluded before the structural pre-filter and
before enrichment. This prevents wasting crawl/LLM budget on practices already
in the client's book of business.

Match priority (most to least specific):
  1. NPI exact match (npi_number column)
  2. Name token overlap (>= 2 shared significant tokens) + same ZIP5
  3. Name token overlap (>= 3 shared significant tokens) + same state

Liberal matching policy: a false suppress (existing customer missed) is cheaper
than wasting enrichment budget; a false positive (valid prospect wrongly suppressed)
is recoverable — the operator can re-enrich the record after removing the match.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Noise tokens stripped from practice names before comparison.
# Mirrors the set in npi_lookup.py so name normalization is consistent.
_NAME_NOISE = frozenset({
    "of", "the", "and", "for", "at", "in",
    "pc", "llc", "pa", "inc", "ltd", "pllc", "lp",
    "assoc", "associates", "group", "center", "centre",
    "practice", "clinic", "health", "care", "medical",
    "medicine", "services", "solutions",
})

# Recognized column names for each role (case-insensitive, checked in order).
_NPI_COLUMNS: frozenset[str] = frozenset({"npi_number", "npi", "provider_npi"})
_NAME_COLUMNS: frozenset[str] = frozenset({
    "practice_name", "name", "account_name", "organization_name", "practice",
})
_ZIP_COLUMNS: frozenset[str] = frozenset({
    "address_zip", "zip", "zip_code", "postal_code", "zip5",
})
_STATE_COLUMNS: frozenset[str] = frozenset({
    "address_state", "state", "state_code",
})

# Minimum shared significant tokens required for a name match.
_MIN_TOKENS_ZIP = 2    # name+ZIP: 2 significant tokens (more specific geography)
_MIN_TOKENS_STATE = 3  # name+state: 3 significant tokens (less specific geography)


def _name_tokens(name: str) -> frozenset[str]:
    """Return lowercase non-noise word tokens from a name string."""
    if not name:
        return frozenset()
    return frozenset(re.findall(r"[a-z0-9]+", name.lower())) - _NAME_NOISE


def _zip5(z: str) -> str:
    """Return first 5 digits of a ZIP code, or empty string."""
    if not z:
        return ""
    digits = re.sub(r"\D", "", str(z))
    return digits[:5] if len(digits) >= 5 else ""


def _state2(s: str) -> str:
    """Return 2-letter uppercase state code, or empty string."""
    if not s:
        return ""
    s = s.strip().upper()
    return s if len(s) == 2 else ""


def _find_col(fieldnames: list[str], candidates: frozenset[str]) -> Optional[str]:
    """Return the first fieldname (case-insensitive) matching any candidate, or None."""
    for f in fieldnames:
        if f.lower().strip() in candidates:
            return f
    return None


@dataclass
class SuppressionList:
    """Indexed suppression data for fast per-record lookups.

    npi_set: set of NPI strings for O(1) exact match.
    by_zip:  zip5 → list of (token_set, display_name) from suppression rows.
    by_state: state → list of (token_set, display_name) from suppression rows.
    """

    npi_set: frozenset[str] = field(default_factory=frozenset)
    by_zip: dict[str, list[tuple[frozenset[str], str]]] = field(default_factory=dict)
    by_state: dict[str, list[tuple[frozenset[str], str]]] = field(default_factory=dict)
    row_count: int = 0

    @property
    def is_empty(self) -> bool:
        """True when the list has no entries (file absent, empty, or unreadable)."""
        return self.row_count == 0


def load_suppression_list(path: "str | Path") -> SuppressionList:
    """Load and index a customer suppression CSV file.

    Flexible headers accepted — see _NPI_COLUMNS, _NAME_COLUMNS, _ZIP_COLUMNS,
    _STATE_COLUMNS for recognized column names. Columns that are absent are
    skipped gracefully; a CSV with only a name column and no geography will only
    produce state/zip entries when those columns exist.

    Returns an empty SuppressionList if the file does not exist or cannot be read.
    Caller should check suppression_list.is_empty before running the match loop.
    """
    path = Path(path)
    if not path.exists():
        logger.warning("Suppression list not found: %s", path)
        return SuppressionList()

    npi_set: set[str] = set()
    by_zip: dict[str, list[tuple[frozenset[str], str]]] = {}
    by_state: dict[str, list[tuple[frozenset[str], str]]] = {}
    row_count = 0

    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                logger.warning("Suppression list has no headers: %s", path)
                return SuppressionList()

            npi_col = _find_col(reader.fieldnames, _NPI_COLUMNS)
            name_col = _find_col(reader.fieldnames, _NAME_COLUMNS)
            zip_col = _find_col(reader.fieldnames, _ZIP_COLUMNS)
            state_col = _find_col(reader.fieldnames, _STATE_COLUMNS)

            for row in reader:
                row_count += 1

                if npi_col:
                    npi_val = (row.get(npi_col) or "").strip()
                    if npi_val:
                        npi_set.add(npi_val)

                if name_col:
                    name_val = (row.get(name_col) or "").strip()
                    tokens = _name_tokens(name_val)
                    if tokens:
                        if zip_col:
                            z = _zip5(row.get(zip_col) or "")
                            if z:
                                by_zip.setdefault(z, []).append((tokens, name_val))
                        if state_col:
                            st = _state2(row.get(state_col) or "")
                            if st:
                                by_state.setdefault(st, []).append((tokens, name_val))

    except (OSError, csv.Error) as e:
        logger.error("Failed to load suppression list %s: %s", path, e)
        return SuppressionList()

    logger.info(
        "Suppression list loaded: %d rows, %d NPIs, %d ZIP buckets, %d state buckets",
        row_count, len(npi_set), len(by_zip), len(by_state),
    )
    return SuppressionList(
        npi_set=frozenset(npi_set),
        by_zip=by_zip,
        by_state=by_state,
        row_count=row_count,
    )


def check_suppression(record: dict, suppression: SuppressionList) -> tuple[bool, str]:
    """Check whether a pipeline record matches a known customer in the suppression list.

    Returns (is_suppressed, reason_string).
    reason_string is empty when not suppressed.

    Match priority (most to least specific):
    1. NPI exact match
    2. >= _MIN_TOKENS_ZIP shared significant name tokens AND same ZIP5
    3. >= _MIN_TOKENS_STATE shared significant name tokens AND same state
    """
    if suppression.is_empty:
        return False, ""

    # Priority 1 — NPI exact match
    npi = (record.get("npi_number") or "").strip()
    if npi and npi in suppression.npi_set:
        return True, f"Existing customer — NPI match: {npi}"

    name = (record.get("practice_name") or "").strip()
    rec_tokens = _name_tokens(name)
    if not rec_tokens:
        return False, ""

    # Priority 2 — name tokens + ZIP
    z = _zip5(record.get("address_zip") or "")
    if z and z in suppression.by_zip:
        for supp_tokens, supp_name in suppression.by_zip[z]:
            if len(rec_tokens & supp_tokens) >= _MIN_TOKENS_ZIP:
                return True, f"Existing customer — name+ZIP match: {supp_name}"

    # Priority 3 — name tokens + state
    st = _state2(record.get("address_state") or "")
    if st and st in suppression.by_state:
        for supp_tokens, supp_name in suppression.by_state[st]:
            if len(rec_tokens & supp_tokens) >= _MIN_TOKENS_STATE:
                return True, f"Existing customer — name+state match: {supp_name}"

    return False, ""
