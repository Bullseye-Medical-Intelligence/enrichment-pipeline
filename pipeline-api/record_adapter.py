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

# Mirrors LOW_SCORE_MANUAL_REVIEW_THRESHOLD in enrichment/constants.py.
# Applied at display time so old frozen runs (enriched before the threshold
# existed) show Manual Review instead of Contender for thin-evidence records.
_LOW_SCORE_MANUAL_REVIEW_THRESHOLD = 50

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

    Analyst overrides bypass all normalization — what the analyst set is final.
    For pipeline tiers, applies two retroactive normalizations so old frozen runs
    render correctly without re-enrichment:
    - "Watchlist" → "Contender" (tier rename)
    - Contender + score < 50 + enriched → "Manual Review" (threshold raise)
    """
    override = (review or {}).get("override_tier")
    if override:
        return _LEGACY_TIER_ALIAS.get(override, override)

    tier = record.get("target_tier", "")
    tier = _LEGACY_TIER_ALIAS.get(tier, tier)

    if tier == "Contender" and record.get("enrichment_status") not in ("not_enriched", None):
        score = record.get("bullseye_score") or 0
        if score < _LOW_SCORE_MANUAL_REVIEW_THRESHOLD:
            return "Manual Review"

    return tier


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


# Ordered tier list used by report renderers and the internal sales handoff.
# Defines the canonical display order: call-first tiers first, suppressed last.
TIER_ORDER: list[str] = [
    "Bullseye", "Needs Verification", "Contender", "Manual Review", "Excluded"
]


def format_phone(raw: str) -> str:
    """Format a phone number string to (NXX) NXX-XXXX or +1 (NXX) NXX-XXXX."""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return raw or "—"
