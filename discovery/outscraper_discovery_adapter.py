"""
outscraper_discovery_adapter.py
Parse Outscraper CSV exports and normalize rows into the field dict expected
by the matcher and classifier.

Column names are intentionally permissive — Outscraper exports vary by version
and the user can also supply manual CSVs with slightly different headers.
"""

import csv
import io
from discovery.matcher import (
    normalize_domain,
    normalize_phone,
    normalize_name,
    normalize_address,
)

# ---------------------------------------------------------------------------
# Column name variants (all lowercased)
# ---------------------------------------------------------------------------

_URL_COLS = ("site", "website", "website_url", "url", "web", "web_url", "website_address")
_PLACE_ID_COLS = ("place_id", "google_place_id")
_PHONE_COLS = ("phone", "phone_number", "phone1")
_NAME_COLS = ("name", "practice_name", "business_name")
_ADDRESS_COLS = ("full_address", "address", "street_address")
_CITY_COLS = ("city", "locality", "address_city", "city_name")
_STATE_COLS = ("state", "region", "address_state", "state_code", "state_name")
_ZIP_COLS = ("postal_code", "zip", "zip_code", "postal")
_CATEGORY_COLS = ("type", "category", "business_type", "categories")
_NPI_COLS = ("npi", "npi_number")


def _first(row: dict, cols: tuple[str, ...]) -> str:
    """Return the first non-empty value found across *cols* in a lowercased row."""
    for col in cols:
        val = (row.get(col) or "").strip()
        if val:
            return val
    return ""


def parse_csv(csv_bytes: bytes) -> list[dict]:
    """
    Parse raw CSV bytes into a list of row dicts with all keys lowercased.

    Handles UTF-8-with-BOM (common in Windows Outscraper exports).
    """
    text = csv_bytes.decode("utf-8-sig")
    return [
        {k.lower(): v for k, v in row.items()}
        for row in csv.DictReader(io.StringIO(text))
    ]


def extract_fields(row: dict) -> dict:
    """
    Extract and normalize every match-relevant field from a lowercased CSV row.

    Returns a flat dict with both raw values (for display) and normalized values
    (for matching and change detection).
    """
    url_raw = _first(row, _URL_COLS)
    phone_raw = _first(row, _PHONE_COLS)
    name_raw = _first(row, _NAME_COLS)
    full_address = _first(row, _ADDRESS_COLS)
    city = _first(row, _CITY_COLS)
    state = _first(row, _STATE_COLS)
    zip_ = _first(row, _ZIP_COLS)

    return {
        # Match keys (normalized)
        "google_place_id": _first(row, _PLACE_ID_COLS),
        "website_domain": normalize_domain(url_raw),
        "phone_digits": normalize_phone(phone_raw),
        "name_normalized": normalize_name(name_raw),
        "address_normalized": normalize_address(full_address, city, state, zip_),
        # Raw / display values
        "practice_name": name_raw,
        "website_url": url_raw,
        "phone": phone_raw,
        "google_category": _first(row, _CATEGORY_COLS),
        "npi": _first(row, _NPI_COLS),
        "address_city": city,
        "address_state": state,
        "address_zip": zip_,
    }
