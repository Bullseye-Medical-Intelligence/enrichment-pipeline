"""
apify_places_adapter.py
Load an Apify "Google Places crawler" CSV export and normalize it to the
Bullseye canonical target schema.

Apify's flattened export differs from Outscraper's in every load-bearing
column: the practice name is `title` (not `name`), the ZIP is `postalCode`,
the business category is `categoryName` (with `categories/N` spillover), the
Google identifier is `placeId`, and `state` carries the FULL state name
("California"). Its `url` column is the Google Maps link, never the practice
website — only `website` may become website_url, or a Maps URL would poison
the crawl target for every site-less practice.

Rows with `permanentlyClosed` true are skipped at load (counted and printed):
a permanently closed practice is not a prospect, and unlike Outscraper exports
this source states the fact outright. Temporarily closed practices are kept —
they can still buy.

Reuses the Outscraper adapter's normalization helpers so record ids, state
abbreviations, phone cleaning, and specialty inference stay identical across
sources (a practice imported from either source produces the same id).
"""

from __future__ import annotations

import csv

from ingestion.outscraper_adapter import (
    _clean_phone,
    _generate_record_id,
    _normalize_state,
    _normalize_url,
    infer_specialty,
)


def _is_true(value: str | None) -> bool:
    """Apify booleans arrive as the strings 'true'/'false' (or empty)."""
    return (value or "").strip().lower() == "true"


def _map_row(row: dict, row_num: int) -> dict:
    """Map one lowercased Apify Places CSV row to a canonical record.

    Raises ValueError when the row cannot become a record (no title).
    """
    practice_name = (row.get("title") or "").strip()
    if not practice_name:
        raise ValueError(f"Row {row_num}: missing practice name (title column)")

    # Only `website` is a practice site. `url` is the Google Maps link — using
    # it as a fallback would send the crawler to google.com/maps.
    website_url = _normalize_url(row.get("website") or "")
    phone = _clean_phone(row.get("phone") or row.get("phoneunformatted") or "")

    # Apify Places exports a parsed `street` plus a full `address`; the street
    # line is what practice-location consolidation blocks on with the ZIP.
    address_street = (row.get("street") or "").strip()
    address_unit = (row.get("unit") or "").strip()
    address_city = (row.get("city") or "").strip()
    address_state = _normalize_state((row.get("state") or "").strip())
    address_zip = (row.get("postalcode") or "").strip()

    type_raw = (row.get("categoryname") or row.get("categories/0") or "").strip()
    google_place_id = (row.get("placeid") or "").strip()

    specialty = infer_specialty(type_raw, practice_name)
    record_id = _generate_record_id(None, practice_name, address_state, address_zip)

    return {
        "id": record_id,
        "practice_name": practice_name,
        "provider_names": [],          # Apify Places doesn't provide this
        "specialty": specialty,
        "npi_optional": None,          # not present in Apify Places exports
        "google_place_id": google_place_id,
        "website_url": website_url,
        "phone": phone,
        "address_street": address_street,
        "address_unit": address_unit,
        "address_city": address_city,
        "address_state": address_state,
        "address_zip": address_zip,
        "metro_region_tag": address_city,
        "state_mandate_status": "",
        "raw_input_source": "",
        "_source_type": "apify_places",
        "_row_num": row_num,
    }


def load_apify_places_csv(filepath: str) -> list[dict]:
    """Load an Apify Google Places CSV and return canonical records.

    Mirrors load_outscraper_csv's contract: lowercased headers (Apify emits
    camelCase), utf-8-sig for the BOM Apify writes, per-row failures skipped
    with a printed reason rather than aborting the batch, permanently closed
    practices dropped with a count.
    """
    records: list[dict] = []
    skipped: list[str] = []
    closed_dropped = 0

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = [
            {(k.strip().lower() if k else k): v for k, v in row.items()}
            for row in reader
        ]

    if rows:
        headers = rows[0].keys()
        if "title" not in headers:
            print(
                "[apify_places_adapter] WARNING: no 'title' column found — "
                "is this really an Apify Google Places export?"
            )
        if "website" not in headers:
            print(
                "[apify_places_adapter] WARNING: no 'website' column found. "
                "All records will have empty website_url and may be excluded "
                "as no_web_presence."
            )

    for row_num, row in enumerate(rows, start=2):  # row 1 is the header
        if _is_true(row.get("permanentlyclosed")):
            closed_dropped += 1
            continue
        try:
            records.append(_map_row(row, row_num))
        except Exception as e:
            skipped.append(str(e))
            print(f"[apify_places_adapter] Skipping row {row_num}: {e}")

    if closed_dropped:
        print(
            f"[apify_places_adapter] Dropped {closed_dropped} permanently "
            "closed practice(s)."
        )
    if skipped:
        print(f"[apify_places_adapter] Skipped {len(skipped)} unusable row(s).")

    return records
