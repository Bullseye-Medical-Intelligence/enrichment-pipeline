"""
tests/test_google_places_adapter.py
Google Places / Apify export ingestion (ingestion/google_places_adapter.py)
and its API-side upload validation.

Deterministic: CSV fixtures on disk, no network, no LLM.
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "pipeline-api"
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO_ROOT))

from ingestion.google_places_adapter import (  # noqa: E402
    _is_directory_url,
    load_google_places_csv,
)
from ingestion.outscraper_adapter import infer_specialty  # noqa: E402

# A realistic slice of an Apify crawler-google-places export: camelCase headers,
# a full state name, a Maps url alongside the real website, extra noise columns.
_HEADERS = [
    "title", "categoryName", "categories/0", "categories/1", "address", "street",
    "city", "state", "postalCode", "phone", "phoneUnformatted", "website",
    "placeId", "url", "permanentlyClosed", "temporarilyClosed", "totalScore",
    "additionalInfo/Amenities/0/Restroom",
]


def _row(**over) -> dict:
    base = {
        "title": "Sierra Behavioral Health",
        "categoryName": "Psychiatrist",
        "categories/0": "Psychiatrist",
        "categories/1": "Mental health clinic",
        "address": "8001 Bruceville Rd, Sacramento, CA 95823",
        "street": "8001 Bruceville Rd",
        "city": "Sacramento",
        "state": "California",
        "postalCode": "95823",
        "phone": "(916) 288-0300",
        "phoneUnformatted": "+19162880300",
        "website": "https://sierrabehavioral.example/",
        "placeId": "ChIJy-T0YcPFmoARzsIl3ihZcoo",
        "url": "https://www.google.com/maps/search/?api=1&query=Sierra",
        "permanentlyClosed": "",
        "temporarilyClosed": "",
        "totalScore": "4.6",
        "additionalInfo/Amenities/0/Restroom": "true",
    }
    base.update(over)
    return base


def _write_csv(tmp_path: Path, rows: list[dict], name="places.csv") -> str:
    path = tmp_path / name
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_HEADERS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return str(path)


class TestFieldMapping:

    def test_maps_apify_columns_to_canonical_schema(self, tmp_path):
        rec = load_google_places_csv(_write_csv(tmp_path, [_row()]))[0]
        assert rec["practice_name"] == "Sierra Behavioral Health"
        assert rec["website_url"] == "https://sierrabehavioral.example"
        assert rec["phone"] == "(916) 288-0300"
        assert rec["address_city"] == "Sacramento"
        assert rec["address_zip"] == "95823"
        assert rec["google_place_id"] == "ChIJy-T0YcPFmoARzsIl3ihZcoo"
        assert rec["_source_type"] == "google_places"
        assert rec["id"].startswith("T-")

    def test_full_state_name_normalized_to_code(self, tmp_path):
        rec = load_google_places_csv(_write_csv(tmp_path, [_row()]))[0]
        assert rec["address_state"] == "CA"

    def test_address_parsed_when_parts_missing(self, tmp_path):
        rec = load_google_places_csv(_write_csv(tmp_path, [
            _row(city="", state="", postalCode="")]))[0]
        assert rec["address_city"] == "Sacramento"
        assert rec["address_state"] == "CA"
        assert rec["address_zip"] == "95823"

    def test_secondary_category_feeds_specialty(self, tmp_path):
        """A generic primary category still resolves via a categories/N slot."""
        rec = load_google_places_csv(_write_csv(tmp_path, [
            _row(categoryName="Doctor", **{"categories/0": "Doctor",
                                           "categories/1": "Neurologist"})]))[0]
        assert rec["specialty"] == "Neurology"

    def test_id_is_stable_across_reloads(self, tmp_path):
        path = _write_csv(tmp_path, [_row()])
        assert load_google_places_csv(path)[0]["id"] == load_google_places_csv(path)[0]["id"]


class TestMapsUrlNeverBecomesWebsite:
    """A Google Maps link is not a practice website.

    Every listing carries one, so treating the `url` column as a site made 100%
    of rows look crawlable and pointed the crawler at google.com.
    """

    def test_missing_website_stays_empty_despite_maps_url(self, tmp_path):
        rec = load_google_places_csv(_write_csv(tmp_path, [_row(website="")]))[0]
        assert rec["website_url"] == ""

    def test_maps_url_in_website_column_is_rejected(self, tmp_path):
        rec = load_google_places_csv(_write_csv(tmp_path, [
            _row(website="https://maps.app.goo.gl/abc123")]))[0]
        assert rec["website_url"] == ""

    @pytest.mark.parametrize("url,expected", [
        ("https://www.google.com/maps/place/x", True),
        ("https://maps.app.goo.gl/abc", True),
        ("https://clinic.business.site", True),
        ("https://sierrabehavioral.example", False),
        ("", False),
    ])
    def test_directory_url_detection(self, url, expected):
        assert _is_directory_url(url) is expected


class TestClosedListings:

    def test_permanently_closed_dropped(self, tmp_path):
        recs = load_google_places_csv(_write_csv(tmp_path, [
            _row(title="Open Clinic"),
            _row(title="Shuttered Clinic", permanentlyClosed="true"),
        ]))
        assert [r["practice_name"] for r in recs] == ["Open Clinic"]

    def test_temporarily_closed_kept(self, tmp_path):
        recs = load_google_places_csv(_write_csv(tmp_path, [
            _row(title="Paused Clinic", temporarilyClosed="true")]))
        assert len(recs) == 1

    def test_row_without_title_skipped_not_fatal(self, tmp_path):
        recs = load_google_places_csv(_write_csv(tmp_path, [
            _row(title=""), _row(title="Real Clinic")]))
        assert [r["practice_name"] for r in recs] == ["Real Clinic"]


class TestSpecialtyWordBoundary:
    """Regression: a substring match made "neurologist" resolve to Urology,
    which structurally excluded every neurology practice as wrong_specialty."""

    @pytest.mark.parametrize("raw,expected", [
        ("Neurologist", "Neurology"),
        ("Pediatric neurologist", "Neurology"),
        ("Neurosurgeon", "Neurosurgery"),
        ("Urologist", "Urology"),
        ("Psychiatrist", "Psychiatry"),
        ("Child psychiatrist", "Psychiatry"),
        ("Psychologist", "Psychology"),
        ("Mental health clinic", "Mental Health"),
        ("Behavioral health service", "Mental Health"),
        ("Obstetrician-gynecologist", "OBGYN"),
    ])
    def test_specialty_inference(self, raw, expected):
        assert infer_specialty(raw) == expected

    def test_unmatched_type_still_titlecased(self, tmp_path):
        # Neutral title: a name containing "behavioral health" would (correctly)
        # match via the practice-name fallback and mask the category path.
        rec = load_google_places_csv(_write_csv(tmp_path, [
            _row(title="Sierra Family Associates",
                 categoryName="Marriage or relationship counselor",
                 **{"categories/0": "Marriage or relationship counselor",
                    "categories/1": ""})]))[0]
        assert rec["specialty"].startswith("Marriage")


class TestUploadValidation:

    def _upload(self, path: str):
        import asyncio

        from fastapi import UploadFile

        import validator

        with open(path, "rb") as fh:
            file = UploadFile(filename="places.csv", file=fh)
            return asyncio.run(
                validator.validate_csv_upload(file, "google_places", "P-1"))

    def test_apify_export_passes_validation(self, tmp_path):
        content, rows = self._upload(_write_csv(tmp_path, [_row(), _row(title="B")]))
        assert rows == 2

    def test_missing_name_column_rejected(self, tmp_path):
        path = tmp_path / "bad.csv"
        path.write_text("website,city\nhttps://x.example,Sacramento\n", encoding="utf-8")
        with pytest.raises(ValueError, match="practice name column"):
            self._upload(str(path))

    def test_missing_website_column_rejected(self, tmp_path):
        path = tmp_path / "bad.csv"
        path.write_text("title,city\nAcme Clinic,Sacramento\n", encoding="utf-8")
        with pytest.raises(ValueError, match="website column"):
            self._upload(str(path))

    def test_google_places_is_a_valid_source_type(self):
        from config import REQUIRED_COLUMNS_BY_SOURCE, VALID_SOURCE_TYPES
        assert "google_places" in VALID_SOURCE_TYPES
        assert "google_places" in REQUIRED_COLUMNS_BY_SOURCE

    def test_preflight_summary_counts_rows_and_flags_closed(self, tmp_path):
        import validator
        path = _write_csv(tmp_path, [
            _row(title="A"), _row(title="B", permanentlyClosed="true")])
        summary = validator.preflight_summary(
            Path(path).read_bytes(), "google_places")
        assert summary["row_count"] == 2
        assert summary["importable"] == 2      # both have titles
        assert any("permanently closed" in w for w in summary["warnings"])
