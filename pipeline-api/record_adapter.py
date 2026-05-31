"""
record_adapter.py
Normalisation helpers for enriched_targets.json records.
Centralises field-name differences (record_id vs id) and payload shapes
(wrapper dict vs bare list) so no other module duplicates this logic.
"""

from urllib.parse import unquote, urlparse, urlunparse


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
    """Return the effective tier: analyst override if set, else the pipeline tier."""
    return (review or {}).get("override_tier") or record.get("target_tier", "")


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
