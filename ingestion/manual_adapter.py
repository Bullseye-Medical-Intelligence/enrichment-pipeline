"""
manual_adapter.py
Pass-through adapter for CSVs already in the Bullseye canonical schema format.
Used for analyst-prepared lists or records sourced outside Outscraper.
"""

import csv
import hashlib
import re
from typing import Optional


# Required fields in a canonical manual CSV
REQUIRED_FIELDS = ["practice_name"]

# All canonical schema fields — any present in the CSV will be used
CANONICAL_FIELDS = [
    "id",
    "practice_name",
    "provider_names",
    "specialty",
    "npi_optional",
    "website_url",
    "phone",
    "address_city",
    "address_state",
    "address_zip",
    "metro_region_tag",
    "state_mandate_status",
]


def _generate_record_id(npi: Optional[str], practice_name: str,
                         address_state: str, address_zip: str) -> str:
    """Generate a stable, deterministic record ID."""
    if npi and npi.strip():
        return f"T-{npi.strip()}"
    raw = f"{practice_name.lower().strip()}|{address_state.lower().strip()}|{address_zip.strip()}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"T-{h}"


def _normalize_url(url: str) -> str:
    """Ensure URL has a scheme; strip trailing slashes."""
    if not url:
        return ""
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def _parse_provider_names(raw: str) -> list:
    """Parse provider names from a pipe- or comma-separated string."""
    if not raw:
        return []
    # Try pipe-separated first
    if "|" in raw:
        return [n.strip() for n in raw.split("|") if n.strip()]
    # Fall back to comma-separated
    return [n.strip() for n in raw.split(",") if n.strip()]


def load_manual_csv(filepath: str) -> list[dict]:
    """
    Load a manually-prepared CSV already in Bullseye canonical format.
    Validates required fields and normalizes values.

    Args:
        filepath: Path to the canonical CSV file.

    Returns:
        List of canonical record dicts.
    """
    records = []
    skipped = []

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    for row_num, row in enumerate(rows, start=2):
        try:
            record = _map_row(row, row_num)
            records.append(record)
        except Exception as e:
            skipped.append({
                "row": row_num,
                "error": str(e),
                "raw": dict(row),
            })

    if skipped:
        print(f"[manual_adapter] Skipped {len(skipped)} rows due to errors:")
        for s in skipped:
            print(f"  Row {s['row']}: {s['error']}")

    print(f"[manual_adapter] Loaded {len(records)} records from {filepath}")
    return records


def _map_row(row: dict, row_num: int) -> dict:
    """Map a single canonical CSV row to the pipeline record format."""

    practice_name = (row.get("practice_name") or "").strip()
    if not practice_name:
        raise ValueError(f"Row {row_num}: missing required field 'practice_name'")

    npi = (row.get("npi_optional") or "").strip() or None
    address_state = (row.get("address_state") or "").strip()
    address_city = (row.get("address_city") or "").strip()
    address_zip = (row.get("address_zip") or "").strip()
    website_url = _normalize_url(row.get("website_url") or "")
    provider_names_raw = (row.get("provider_names") or "").strip()

    # Use existing ID if provided, otherwise generate one
    existing_id = (row.get("id") or "").strip()
    record_id = existing_id if existing_id else _generate_record_id(
        npi, practice_name, address_state, address_zip
    )

    return {
        "id": record_id,
        "practice_name": practice_name,
        "provider_names": _parse_provider_names(provider_names_raw),
        "specialty": (row.get("specialty") or "").strip() or "Unknown",
        "npi_optional": npi,
        "website_url": website_url,
        "phone": (row.get("phone") or "").strip(),
        "address_city": address_city,
        "address_state": address_state,
        "address_zip": address_zip,
        "metro_region_tag": (row.get("metro_region_tag") or address_city).strip(),
        "state_mandate_status": (row.get("state_mandate_status") or "").strip(),
        "raw_input_source": "",
        "_source_type": "manual",
        "_row_num": row_num,
    }
