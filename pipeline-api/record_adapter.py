"""
record_adapter.py
Normalisation helpers for enriched_targets.json records.
Centralises field-name differences (record_id vs id) and payload shapes
(wrapper dict vs bare list) so no other module duplicates this logic.
"""

import csv
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

# ---------------------------------------------------------------------------
# ZIP → city/state lookup (lazy-loaded, display layer only)
# ---------------------------------------------------------------------------

_ZIP_CSV = Path(__file__).parent.parent / "ingestion" / "us_zip_city_state.csv"
_ZIP_INDEX: dict | None = None


def _ensure_zip_index() -> None:
    """Lazily build the ZIP → (city, state) lookup from the bundled CSV."""
    global _ZIP_INDEX
    if _ZIP_INDEX is not None:
        return
    _ZIP_INDEX = {}
    if not _ZIP_CSV.exists():
        return
    with open(_ZIP_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            z = row.get("zip", "").strip().zfill(5)
            if z:
                _ZIP_INDEX[z] = (row.get("city", ""), row.get("state", ""))


def zip_to_city_state(zip_code: str) -> tuple[str, str]:
    """Resolve a ZIP code string to (city, state), returning ("", "") on miss."""
    _ensure_zip_index()
    if not zip_code:
        return "", ""
    z = zip_code.strip()[:5].zfill(5)
    return (_ZIP_INDEX or {}).get(z, ("", ""))


# ---------------------------------------------------------------------------
# Legacy tier aliases (renamed tiers in old frozen run data)
# ---------------------------------------------------------------------------

_LEGACY_TIER_ALIAS = {"Watchlist": "Contender"}

# ---------------------------------------------------------------------------
# Confidence band derivation for old records that pre-date the field
# ---------------------------------------------------------------------------

def effective_confidence_band(record: dict) -> str:
    """Return the record's confidence band, computing from confidence_score if absent."""
    band = record.get("confidence_band", "")
    if band:
        return band
    score = record.get("confidence_score") or 0
    if score >= 65:
        return "High"
    if score >= 45:
        return "Moderate"
    return "Low"


def normalize_homepage_url(url: str) -> str:
    """Decode percent-encoding and strip tracking query params / fragments.

    Keeps scheme + netloc + path so a practice's specific location page
    (franchise sub-URL, etc.) is preserved for display, export, and re-crawl.
    Stripping the path sent a bare domain that often failed validation, which
    is why re-crawls of sub-page URLs came back empty. Drops only the query
    string and fragment (UTM/tracking params). Returns "" for blank or
    malformed input with no netloc.
    """
    if not url:
        return ""
    url = unquote(url.strip())
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/") or ""
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def get_record_id(record: dict) -> str:
    """Return the record's stable ID, checking record_id then id."""
    return record.get("record_id") or record.get("id") or ""


def normalize_records_payload(data) -> list[dict]:
    """Extract the records list from an enriched_targets.json payload.

    Pipeline writes: {run_id, generated_at, record_count, records: [...]}.
    Also handles a bare list for forward/backward compatibility.
    """
    if isinstance(data, dict):
        return data.get("records") or []
    if isinstance(data, list):
        return data
    return []


def displayed_tier(record: dict, review: dict) -> str:
    """Return the effective tier: analyst override if set, else the pipeline tier.

    Applies legacy tier aliases so old frozen runs show current tier labels
    (e.g. "Watchlist" → "Contender") without requiring re-enrichment.
    """
    tier = (review or {}).get("override_tier") or record.get("target_tier", "")
    return _LEGACY_TIER_ALIAS.get(tier, tier)


def effective_tier(record: dict, all_reviews: dict) -> str:
    """displayed_tier resolved against a {record_id: review} map."""
    return displayed_tier(record, all_reviews.get(get_record_id(record), {}))


# Contact Priority in the queue IS the displayed tier — the four-tier ladder
# already names the action, so there is no separate relabel. This is pure
# presentation of displayed_tier; there is one source of truth for account
# quality. Rank: higher sorts first (call sooner).
_TIER_QUEUE_RANK = {
    "bullseye": 3,
    "needs verification": 2,
    "contender": 1,
    "manual review": 0,
    "excluded": 0,
}
_TIER_QUEUE_RANK_DEFAULT = 1


def contact_priority(record: dict, review: dict) -> str:
    """Return the Contact Priority label for a record — its displayed tier."""
    return displayed_tier(record, review).strip() or "Contender"


def contact_priority_rank(record: dict, review: dict) -> int:
    """Return the queue sort rank for a record's tier (higher = call sooner)."""
    tier = displayed_tier(record, review).strip().lower()
    return _TIER_QUEUE_RANK.get(tier, _TIER_QUEUE_RANK_DEFAULT)
