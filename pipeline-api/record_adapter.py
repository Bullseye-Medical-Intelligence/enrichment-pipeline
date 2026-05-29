"""
record_adapter.py
Normalisation helpers for enriched_targets.json records.
Centralises field-name differences (record_id vs id) and payload shapes
(wrapper dict vs bare list) so no other module duplicates this logic.
"""


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


# Rep-facing relabel of the tier ladder for the Contact Queue. This is pure
# presentation of the existing displayed_tier — it is NOT a second stored
# classification, so there is one source of truth for account quality.
# (rank, label): higher rank sorts first in the queue.
_CONTACT_PRIORITY = {
    "bullseye": (5, "Priority Outreach"),
    "strong": (5, "Priority Outreach"),
    "needs verification": (4, "Verify & Engage"),
    "warm": (3, "Develop"),
    "watchlist": (2, "Develop"),
    "cold": (1, "Monitor"),
    "excluded": (0, "Do Not Pursue"),
}
_CONTACT_PRIORITY_DEFAULT = (2, "Develop")


def contact_priority(record: dict, review: dict) -> str:
    """Return the rep-facing Contact Priority label for a record's displayed tier."""
    tier = displayed_tier(record, review).strip().lower()
    return _CONTACT_PRIORITY.get(tier, _CONTACT_PRIORITY_DEFAULT)[1]


def contact_priority_rank(record: dict, review: dict) -> int:
    """Return the sort rank for a record's Contact Priority (higher = call sooner)."""
    tier = displayed_tier(record, review).strip().lower()
    return _CONTACT_PRIORITY.get(tier, _CONTACT_PRIORITY_DEFAULT)[0]
