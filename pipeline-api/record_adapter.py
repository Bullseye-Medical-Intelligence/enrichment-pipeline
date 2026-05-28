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
