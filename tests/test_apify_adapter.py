"""
tests/test_apify_adapter.py
Deterministic tests for the Apify Google Places CSV source. Fixtures are
synthetic (never real client data, per CLAUDE.md RULE 2) but mirror the real
export's shape: utf-8 BOM, camelCase headers, full state names, string
booleans, and a Google Maps link in `url` that must never become website_url.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ingestion.apify_places_adapter import load_apify_places_csv
from ingestion.outscraper_adapter import _generate_record_id

_HEADERS = (
    "title,address,street,city,state,postalCode,phone,phoneUnformatted,"
    "website,url,categoryName,categories/0,categories/1,placeId,"
    "permanentlyClosed,temporarilyClosed,countryCode"
)


def _write_csv(tmp_path: Path, rows: list[str], bom: bool = True) -> str:
    body = _HEADERS + "\n" + "\n".join(rows) + "\n"
    path = tmp_path / "apify.csv"
    prefix = "﻿" if bom else ""
    path.write_text(prefix + body, encoding="utf-8")
    return str(path)


def _row(
    title="Acme Mental Health",
    city="Sacramento",
    state="California",
    postal="95823",
    phone="(916) 555-0100",
    phone_unf="+19165550100",
    website="https://acme-mh.example.com/",
    maps_url="https://www.google.com/maps/search/?api=1&query=Acme",
    category="Mental health clinic",
    place_id="ChIJtestplaceid001",
    perm_closed="false",
    temp_closed="false",
) -> str:
    cells = [
        title, "1 Main St, Sacramento, CA 95823", "1 Main St", city, state,
        postal, phone, phone_unf, website, maps_url, category, category, "",
        place_id, perm_closed, temp_closed, "US",
    ]
    return ",".join(f'"{c}"' for c in cells)


class TestMapping:

    def test_canonical_fields_mapped(self, tmp_path):
        records = load_apify_places_csv(_write_csv(tmp_path, [_row()]))
        assert len(records) == 1
        r = records[0]
        assert r["practice_name"] == "Acme Mental Health"
        assert r["address_city"] == "Sacramento"
        assert r["address_state"] == "CA"          # full name normalized
        assert r["address_zip"] == "95823"
        assert r["website_url"].startswith("https://acme-mh.example.com")
        assert r["phone"]
        assert r["google_place_id"] == "ChIJtestplaceid001"
        assert r["specialty"]                       # inferred from categoryName
        assert r["_source_type"] == "apify_places"
        assert r["npi_optional"] is None

    def test_bom_headers_are_readable(self, tmp_path):
        """Apify writes a UTF-8 BOM; the first header must not become '\\ufefftitle'."""
        records = load_apify_places_csv(_write_csv(tmp_path, [_row()], bom=True))
        assert records and records[0]["practice_name"] == "Acme Mental Health"

    def test_maps_url_never_becomes_website(self, tmp_path):
        """`url` is the Google Maps link — a site-less practice must get an
        empty website_url, not google.com/maps as its crawl target."""
        records = load_apify_places_csv(_write_csv(tmp_path, [_row(website="")]))
        assert records[0]["website_url"] == ""

    def test_phone_falls_back_to_unformatted(self, tmp_path):
        records = load_apify_places_csv(
            _write_csv(tmp_path, [_row(phone="", phone_unf="+19165550199")])
        )
        assert "9165550199" in records[0]["phone"].replace("-", "").replace(" ", "")

    def test_category_fallback_to_categories_0(self, tmp_path):
        records = load_apify_places_csv(_write_csv(tmp_path, [_row(category="")]))
        # categoryName empty -> categories/0 (also empty here) -> specialty Unknown,
        # which the structural pre-filter treats as "not a confirmed mismatch".
        assert records[0]["specialty"] == "Unknown"

    def test_record_id_matches_shared_generator(self, tmp_path):
        """Ids must be identical to what any other source would produce for the
        same practice, so cross-source dedup and registry matching hold."""
        records = load_apify_places_csv(_write_csv(tmp_path, [_row()]))
        expected = _generate_record_id(None, "Acme Mental Health", "CA", "95823")
        assert records[0]["id"] == expected


class TestRowFiltering:

    def test_permanently_closed_dropped(self, tmp_path):
        rows = [_row(), _row(title="Closed Clinic", perm_closed="true")]
        records = load_apify_places_csv(_write_csv(tmp_path, rows))
        names = [r["practice_name"] for r in records]
        assert names == ["Acme Mental Health"]

    def test_temporarily_closed_kept(self, tmp_path):
        rows = [_row(title="Paused Clinic", temp_closed="true")]
        records = load_apify_places_csv(_write_csv(tmp_path, rows))
        assert records and records[0]["practice_name"] == "Paused Clinic"

    def test_missing_title_skipped_not_fatal(self, tmp_path):
        rows = [_row(title=""), _row(title="Good Clinic")]
        records = load_apify_places_csv(_write_csv(tmp_path, rows))
        assert [r["practice_name"] for r in records] == ["Good Clinic"]


class TestValidatorIntegration:

    def test_source_type_accepted(self):
        api_dir = REPO_ROOT / "pipeline-api"
        if str(api_dir) not in sys.path:
            sys.path.insert(0, str(api_dir))
        from config import REQUIRED_COLUMNS_BY_SOURCE, VALID_SOURCE_TYPES
        assert "apify_places" in VALID_SOURCE_TYPES
        assert REQUIRED_COLUMNS_BY_SOURCE["apify_places"] == frozenset({"title"})

    def test_preflight_summary_counts_titles(self):
        api_dir = REPO_ROOT / "pipeline-api"
        if str(api_dir) not in sys.path:
            sys.path.insert(0, str(api_dir))
        import validator
        content = (_HEADERS + "\n" + _row() + "\n" + _row(title="Second") + "\n").encode("utf-8")
        summary = validator.preflight_summary(content, "apify_places")
        assert summary["importable"] == 2
