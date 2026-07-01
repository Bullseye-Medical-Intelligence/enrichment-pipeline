"""
outscraper_adapter.py
Maps Outscraper CSV exports to the Bullseye canonical target schema.

Outscraper field names must NOT leak into pipeline logic downstream.
This adapter is the only place they exist.
"""

import csv
import hashlib
import re
import urllib.parse
from typing import Optional

from ingestion import zip_lookup


# ---------------------------------------------------------------------------
# US state full name → 2-letter abbreviation
# Outscraper exports often use full state names; pipeline always stores abbreviations
# ---------------------------------------------------------------------------
US_STATE_ABBREV = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
    # US territories (Outscraper occasionally emits these as full names)
    "puerto rico": "PR", "guam": "GU", "u.s. virgin islands": "VI",
    "virgin islands": "VI", "american samoa": "AS",
    "northern mariana islands": "MP",
}


def _normalize_state(state: str) -> str:
    """Convert full state name to 2-letter abbreviation. Pass-through if already abbreviated."""
    if not state:
        return ""
    s = state.strip()
    # Already an abbreviation
    if len(s) == 2:
        return s.upper()
    # Look up full name
    return US_STATE_ABBREV.get(s.lower(), s.upper())


# ---------------------------------------------------------------------------
# Outscraper → Bullseye field mapping
# Keys are Outscraper column names; values are Bullseye canonical field names
# Only fields we actively map are listed here. All others are discarded.
# ---------------------------------------------------------------------------
OUTSCRAPER_FIELD_MAP = {
    "name": "practice_name",
    "full_address": "_full_address",   # parsed separately
    "state": "address_state",
    "city": "address_city",
    "postal_code": "address_zip",
    "phone": "phone",
    "site": "website_url",
    "website": "website_url",          # alternate column name in some exports
    "type": "_type_raw",               # used for specialty matching, then discarded
    "npi": "npi_optional",
    "place_id": "google_place_id",
    "google_place_id": "google_place_id",
    # Additional Outscraper columns that sometimes appear:
    "owner_name": "_owner_name_raw",
    "description": "_description_raw",
    "subtypes": "_subtypes_raw",
    # Additional URL column names seen in different Outscraper export formats:
    "website_url": "website_url",
    "url": "website_url",
    "web": "website_url",
    "web_url": "website_url",
    "website_address": "website_url",
}

# Specialty keywords for matching Outscraper "type" to canonical specialty
SPECIALTY_KEYWORD_MAP = {
    "OBGYN": [
        "obgyn", "ob/gyn", "ob-gyn", "gynecolog", "obstetric",
        "women's health", "womens health", "women's care", "womens care",
        "reproductive",
    ],
    "Urology": ["urol"],
    "Dermatology": ["dermatol"],
    "Cardiology": ["cardiolog"],
    "Orthodontics": ["orthodont"],
    "Orthopedics": ["orthoped", "orthopaed"],
    "Geriatric Medicine": ["geriatric medicine", "geriatrics", "geriatric"],
    "Internal Medicine": ["internal medicine"],
    "Family Medicine": ["family medicine", "family practice"],
}


def _generate_record_id(npi: Optional[str], practice_name: str,
                         address_state: str, address_zip: str) -> str:
    """
    Generate a stable, deterministic record ID.
    Prefers NPI if present; otherwise hashes practice_name + state + zip.
    """
    if npi and npi.strip():
        return f"T-{npi.strip()}"
    raw = f"{practice_name.lower().strip()}|{address_state.lower().strip()}|{address_zip.strip()}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"T-{h}"


def _parse_full_address(full_address: str) -> dict:
    """
    Parse city, state, and (optional) zip from a free-form US address string.
    Returns a dict with address_city, address_state, address_zip; each field is
    "" when it cannot be determined. Never raises.

    Reads the trailing comma segments, which for US addresses are
    "..., City, ST ZIP" (or "..., City, State"). This resolves multi-segment
    addresses like "123 Oak Ave, Suite 5, Miami, FL 33101" to Miami / FL / 33101
    rather than an earlier street or suite fragment. The state is returned as
    written; the caller normalizes it to a 2-letter abbreviation.
    """
    result = {"address_city": "", "address_state": "", "address_zip": ""}
    if not full_address:
        return result

    parts = [p.strip() for p in full_address.split(",") if p.strip()]
    if len(parts) < 2:
        return result

    # The last segment carries the state and an optional zip: "FL", "FL 33101",
    # "TX 75201-1234", or a full state name like "Texas".
    m = re.match(r"^([A-Za-z][A-Za-z\s\.]*?)(?:\s+(\d{5}(?:-\d{4})?))?$", parts[-1])
    if not m:
        return result

    result["address_state"] = m.group(1).strip()
    result["address_zip"] = (m.group(2) or "").strip()
    result["address_city"] = parts[-2]
    return result


def infer_specialty(type_raw: str, practice_name: str = "") -> str:
    """
    Map the Outscraper 'type' field to a canonical specialty string, falling
    back to keywords in the practice name when 'type' is absent or unmatched.
    Returns "Unknown" if neither yields a match.
    """
    for text in (type_raw, practice_name):
        lower = (text or "").lower()
        if not lower:
            continue
        for specialty, keywords in SPECIALTY_KEYWORD_MAP.items():
            for kw in keywords:
                if kw in lower:
                    return specialty
    # 'type' present but unmatched: keep it as a titlecased label.
    if (type_raw or "").strip():
        return type_raw.title()
    return "Unknown"


# Common placeholder strings that mean "no website" in export tools
_URL_PLACEHOLDERS = frozenset({
    "n/a", "na", "none", "null", "-", "--", "no website",
    "no url", "not available", "not applicable", "#n/a",
})


def _normalize_url(url: str) -> str:
    """Decode percent-encoding and strip tracking query params / fragments.

    Keeps scheme + netloc + path so that a practice with a specific location
    page (e.g. a franchise sub-URL) is validated and crawled from that page
    rather than the generic domain root. Stripping the path caused URL
    validation failures for sub-page URLs, producing false "limited" results.

    Returns empty string for blank/placeholder values and malformed URLs with no netloc.
    """
    if not url:
        return ""
    url = urllib.parse.unquote(url.strip())
    if url.lower() in _URL_PLACEHOLDERS:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    # Keep scheme + netloc + path; drop query string and fragment (tracking params).
    # Reject URLs with no netloc (malformed input).
    if not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/") or ""
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _clean_phone(phone: str) -> str:
    """Return phone string as-is from Outscraper (already formatted)."""
    return (phone or "").strip()


def load_outscraper_csv(filepath: str) -> list[dict]:
    """
    Load an Outscraper CSV file and return a list of records normalized
    to the Bullseye canonical target schema.

    Args:
        filepath: Path to the Outscraper CSV file.

    Returns:
        List of canonical record dicts.
    """
    records = []
    skipped = []

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Normalize header case so case-variant columns (e.g. "Name", "Site")
        # map the same way pre-flight validation reads them (it lowercases too).
        rows = [{(k.strip().lower() if k else k): v for k, v in row.items()} for row in reader]

    if rows:
        headers = list(rows[0].keys())
        print(f"[outscraper_adapter] CSV columns detected: {headers}")
        _URL_COLS = ("site", "website", "website_url", "url", "web", "web_url", "website_address")
        found_url_col = next((c for c in _URL_COLS if c in headers), None)
        if found_url_col:
            print(f"[outscraper_adapter] Using URL column: '{found_url_col}'")
        else:
            print(
                f"[outscraper_adapter] WARNING: No URL column found in CSV headers. "
                f"Expected one of: {_URL_COLS}. "
                f"All records will have empty website_url and may be excluded as no_web_presence."
            )

    for row_num, row in enumerate(rows, start=2):  # row 1 is header
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
        print(f"[outscraper_adapter] Skipped {len(skipped)} rows due to errors:")
        for s in skipped:
            print(f"  Row {s['row']}: {s['error']}")

    no_url_count = sum(1 for r in records if not r.get("website_url"))
    print(
        f"[outscraper_adapter] Loaded {len(records)} records — "
        f"{len(records) - no_url_count} with URL, {no_url_count} without URL"
    )
    if no_url_count:
        sample = [r for r in records if not r.get("website_url")][:5]
        print(f"[outscraper_adapter] Sample records with no URL (first {len(sample)}):")
        for r in sample:
            raw_website = rows[r["_row_num"] - 2].get("website", "") if r["_row_num"] - 2 < len(rows) else "?"
            print(f"  Row {r['_row_num']}: '{r['practice_name']}' — raw website cell: {raw_website!r}")
    return records


def _map_row(row: dict, row_num: int) -> dict:
    """Map a single Outscraper CSV row to the canonical schema."""

    # Pull raw values using Outscraper field names (only place this happens)
    practice_name = (row.get("name") or "").strip()
    full_address = (row.get("full_address") or row.get("address") or "").strip()
    # Check every state/city column name seen across Outscraper export formats
    address_state = _normalize_state(
        row.get("state") or row.get("region") or row.get("address_state")
        or row.get("state_code") or row.get("state_name") or ""
    )
    address_city = (
        row.get("city") or row.get("locality") or row.get("address_city")
        or row.get("city_name") or ""
    ).strip()
    address_zip = (row.get("postal_code") or row.get("zip") or row.get("zip_code") or "").strip()
    phone = _clean_phone(row.get("phone") or "")
    # Check every URL column name seen across different Outscraper export formats
    website_url = _normalize_url(
        row.get("site")
        or row.get("website")
        or row.get("website_url")
        or row.get("url")
        or row.get("web")
        or row.get("web_url")
        or row.get("website_address")
        or ""
    )
    type_raw = (row.get("type") or row.get("category") or row.get("business_type") or "").strip()
    npi = (row.get("npi") or "").strip() or None
    google_place_id = (row.get("place_id") or row.get("google_place_id") or "").strip()

    # If city/state/zip are missing, try to parse from full_address
    if not address_city or not address_state or not address_zip:
        parsed = _parse_full_address(full_address)
        address_city = address_city or parsed["address_city"]
        address_state = _normalize_state(parsed["address_state"]) if not address_state else address_state
        address_zip = address_zip or parsed["address_zip"]

    # Last resort: derive city/state from the ZIP via the bundled offline lookup
    # (deterministic, no network, no LLM) when the source list gave only a ZIP.
    if address_zip and (not address_city or not address_state):
        zip_city, zip_state = zip_lookup.infer_city_state(address_zip)
        address_city = address_city or zip_city
        address_state = address_state or zip_state

    # Require at minimum a practice name
    if not practice_name:
        raise ValueError(f"Row {row_num}: missing practice name")

    specialty = infer_specialty(type_raw, practice_name)
    record_id = _generate_record_id(npi, practice_name, address_state, address_zip)

    # Build canonical record — all downstream pipeline steps use ONLY these fields
    return {
        "id": record_id,
        "practice_name": practice_name,
        "provider_names": [],          # Outscraper doesn't reliably provide this
        "specialty": specialty,
        "npi_optional": npi or None,
        "google_place_id": google_place_id,
        "website_url": website_url,
        "phone": phone,
        "address_city": address_city,
        "address_state": address_state,
        "address_zip": address_zip,
        "metro_region_tag": address_city,  # Default to city; can be overridden by config
        "state_mandate_status": "",        # Populated by enrichment step if needed
        # Pipeline tracking fields (populated downstream)
        "raw_input_source": "",
        "_source_type": "outscraper",
        "_row_num": row_num,
    }
