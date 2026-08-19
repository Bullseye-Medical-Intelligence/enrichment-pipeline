"""
google_places_adapter.py
Google Places / Google Maps place-listing CSV → Bullseye canonical schema.

Handles exports from Google Places scrapers (Apify's `crawler-google-places`
actor and compatible tools), whose column names differ from Outscraper's:
`title` not `name`, `categoryName` not `type`, `website` not `site`,
`postalCode` not `postal_code`, `placeId` not `place_id`. Such exports also
carry hundreds of flattened detail columns (opening hours, review text,
amenities); everything outside the mapping below is ignored, exactly as
PIPELINE.md requires — source field names are mapped once here and discarded.

Normalization helpers (state, address, specialty, URL, phone, record id) are
shared with outscraper_adapter so every source produces byte-identical
canonical records for the same practice.

Permanently-closed listings are dropped at ingest with a printed count: they
are not prospects, and paying to crawl them is waste. Temporarily-closed
listings are kept — they still trade.
"""

import csv
import urllib.parse

from ingestion.outscraper_adapter import (
    _clean_phone,
    _generate_record_id,
    _normalize_state,
    _normalize_url,
    _parse_full_address,
    infer_specialty,
)

# Canonical (lowercased) source column → what it feeds. Apify emits camelCase
# headers; readers lowercase them first so both casings resolve.
TITLE_COLUMNS = ("title", "name", "placename")
CATEGORY_COLUMNS = ("categoryname", "category", "categories/0", "type")
# Deliberately excludes a bare "url": Google exports put the Google MAPS link
# there, and every listing has one. Treating it as the practice website made
# 100% of rows look like they had a site, then sent the crawler to google.com.
WEBSITE_COLUMNS = ("website", "site", "website_url", "domain")
PHONE_COLUMNS = ("phone", "phoneunformatted", "phone_number")
PLACE_ID_COLUMNS = ("placeid", "place_id", "google_place_id")
STREET_COLUMNS = ("street", "address_line_1")
CITY_COLUMNS = ("city", "locality")
STATE_COLUMNS = ("state", "region", "administrativearea")
ZIP_COLUMNS = ("postalcode", "postal_code", "zip", "zipcode")
ADDRESS_COLUMNS = ("address", "full_address", "formattedaddress")

# Extra category slots an export may carry (categories/0 … categories/9). The
# primary category is often generic ("Doctor"); a secondary one is frequently
# the specialty that matters, so all of them feed specialty inference.
_MAX_CATEGORY_SLOTS = 10


def _first(row: dict, columns: tuple) -> str:
    """Return the first non-empty value among the given columns."""
    for col in columns:
        value = (row.get(col) or "").strip()
        if value:
            return value
    return ""


# Hosts that are never a practice's own website. A listing whose only link is
# one of these is treated as having no site, so it is honestly reported as
# no_web_presence instead of being crawled for boilerplate.
_NON_PRACTICE_HOSTS = (
    "google.com", "goo.gl", "maps.app.goo.gl", "business.site",
)


def _is_directory_url(url: str) -> bool:
    """True when a URL points at Google Maps or a similar non-practice host."""
    host = urllib.parse.urlparse(url or "").netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return any(host == h or host.endswith("." + h) for h in _NON_PRACTICE_HOSTS)


def _is_true(value: str) -> bool:
    """Interpret a CSV boolean cell ('true'/'TRUE'/'1') as a bool."""
    return (value or "").strip().lower() in ("true", "1", "yes")


def _collect_categories(row: dict) -> str:
    """Join the primary category with any categories/N slots, most specific first."""
    values = []
    primary = _first(row, CATEGORY_COLUMNS)
    if primary:
        values.append(primary)
    for i in range(_MAX_CATEGORY_SLOTS):
        value = (row.get(f"categories/{i}") or "").strip()
        if value and value not in values:
            values.append(value)
    return ", ".join(values)


def _map_row(row: dict, row_num: int) -> dict:
    """Map one Google Places export row to a canonical Bullseye record."""
    practice_name = _first(row, TITLE_COLUMNS)
    if not practice_name:
        raise ValueError("row has no practice name (expected a 'title' column)")

    categories = _collect_categories(row)
    specialty = infer_specialty(categories, practice_name)

    street = _first(row, STREET_COLUMNS)
    address_city = _first(row, CITY_COLUMNS)
    address_state = _normalize_state(_first(row, STATE_COLUMNS))
    address_zip = _first(row, ZIP_COLUMNS)
    full_address = _first(row, ADDRESS_COLUMNS)

    # Fall back to parsing the formatted address for whichever parts are absent.
    # Google's `city` column is sometimes the SEARCH city rather than the
    # listing's own, so the formatted address is the more trustworthy source
    # when they disagree on a missing field.
    if full_address and not (address_city and address_state and address_zip):
        parsed = _parse_full_address(full_address)
        address_city = address_city or parsed["address_city"]
        address_state = address_state or _normalize_state(parsed["address_state"])
        address_zip = address_zip or parsed["address_zip"]

    if not full_address:
        full_address = ", ".join(
            p for p in (street, address_city, f"{address_state} {address_zip}".strip()) if p
        )

    website_url = _normalize_url(_first(row, WEBSITE_COLUMNS))
    if _is_directory_url(website_url):
        website_url = ""   # a maps/aggregator link is not the practice's site
    phone = _clean_phone(_first(row, PHONE_COLUMNS))
    google_place_id = _first(row, PLACE_ID_COLUMNS)
    npi = (row.get("npi") or "").strip()

    record_id = _generate_record_id(npi, practice_name, address_state, address_zip)

    return {
        "id": record_id,
        "practice_name": practice_name,
        "provider_names": [],          # Place listings carry no provider roster
        "specialty": specialty,
        "npi_optional": npi or None,
        "google_place_id": google_place_id,
        "website_url": website_url,
        "phone": phone,
        "address_city": address_city,
        "address_state": address_state,
        "address_zip": address_zip,
        "metro_region_tag": address_city,
        "state_mandate_status": "",
        "raw_input_source": "",
        "_source_type": "google_places",
        "_row_num": row_num,
        "_full_address": full_address,
    }


def load_google_places_csv(filepath: str) -> list[dict]:
    """
    Load a Google Places export CSV and return canonical Bullseye records.

    Args:
        filepath: Path to the Google Places / Apify CSV file.

    Returns:
        List of canonical record dicts. Rows without a practice name and
        permanently-closed listings are skipped, both with a printed count.
    """
    records: list[dict] = []
    skipped: list[dict] = []
    closed_count = 0

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = [
            {(k.strip().lower() if k else k): v for k, v in row.items()}
            for row in reader
        ]

    if rows:
        headers = list(rows[0].keys())
        found_url_col = next((c for c in WEBSITE_COLUMNS if c in headers), None)
        print(f"[google_places_adapter] {len(headers)} CSV columns detected")
        if found_url_col:
            print(f"[google_places_adapter] Using URL column: '{found_url_col}'")
        else:
            print(
                "[google_places_adapter] WARNING: no website column found "
                f"(expected one of: {WEBSITE_COLUMNS}). All records will have an "
                "empty website_url and may be excluded as no_web_presence."
            )

    for row_num, row in enumerate(rows, start=2):  # row 1 is the header
        if _is_true(row.get("permanentlyclosed", "")):
            closed_count += 1
            continue
        try:
            records.append(_map_row(row, row_num))
        except Exception as e:
            skipped.append({"row": row_num, "error": str(e)})

    if closed_count:
        print(
            f"[google_places_adapter] Skipped {closed_count} permanently-closed "
            "listing(s) — not prospects, so they are never crawled."
        )
    if skipped:
        print(f"[google_places_adapter] Skipped {len(skipped)} row(s) due to errors:")
        for s in skipped:
            print(f"  Row {s['row']}: {s['error']}")

    no_url_count = sum(1 for r in records if not r.get("website_url"))
    print(
        f"[google_places_adapter] Loaded {len(records)} records — "
        f"{len(records) - no_url_count} with URL, {no_url_count} without URL"
    )
    return records
