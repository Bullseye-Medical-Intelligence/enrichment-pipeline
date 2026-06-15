"""
tests/test_discovery.py
Unit tests for the discovery package.

All tests are deterministic — no API calls, no HTTP, no file I/O except where
the tmp_path fixture provides an isolated directory.
"""

import io
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from discovery.matcher import (
    normalize_domain,
    normalize_phone,
    normalize_name,
    normalize_address,
    name_address_key,
    build_indexes,
    find_match,
)
from discovery.classifier import (
    NEW, CHANGED, KNOWN, POSSIBLE_DUPLICATE, INSUFFICIENT_DATA,
    has_sufficient_data,
    detect_changes,
    classify,
)
from discovery.outscraper_discovery_adapter import parse_csv, extract_fields
from discovery.registry import empty_registry, load_registry, save_registry
from discovery.writer import write_results, _build_preview_registry
from discovery import run_discovery


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_csv(*rows: dict) -> bytes:
    """Build a minimal CSV bytes object from a list of row dicts."""
    import csv, io
    if not rows:
        return b"name,phone,site,place_id\n"
    fieldnames = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _registry_with(*entries: dict) -> dict:
    """Build a registry dict from a list of entry dicts (must include entry_id)."""
    reg = empty_registry()
    for e in entries:
        reg["entries"][e["entry_id"]] = e
    reg["entry_count"] = len(entries)
    return reg


def _entry(
    *,
    entry_id: str = "e001",
    google_place_id: str = "",
    website_domain: str = "",
    phone_digits: str = "",
    name_normalized: str = "",
    address_normalized: str = "",
    practice_name: str = "Test Practice",
    google_category: str = "",
    last_tier: str = "Bullseye",
) -> dict:
    """Minimal registry entry factory."""
    return {
        "entry_id": entry_id,
        "google_place_id": google_place_id,
        "website_domain": website_domain,
        "phone_digits": phone_digits,
        "name_normalized": name_normalized,
        "address_normalized": address_normalized,
        "practice_name": practice_name,
        "google_category": google_category,
        "last_tier": last_tier,
        "last_score": 0,
        "website_url": "",
        "phone": "",
        "address_city": "",
        "address_state": "",
        "address_zip": "",
        "npi": "",
        "first_seen_run_id": "run_old",
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_run_id": "run_old",
        "last_seen_at": "2026-01-01T00:00:00+00:00",
        "runs_seen": ["run_old"],
        "change_log": [],
    }


# ---------------------------------------------------------------------------
# Normalization tests
# ---------------------------------------------------------------------------

class TestNormalization:
    def test_domain_strips_www(self):
        assert normalize_domain("https://www.example.com") == "example.com"

    def test_domain_strips_scheme(self):
        assert normalize_domain("http://example.com/path") == "example.com"

    def test_domain_adds_scheme_when_missing(self):
        assert normalize_domain("example.com") == "example.com"

    def test_domain_strips_port(self):
        assert normalize_domain("https://example.com:8443") == "example.com"

    def test_domain_empty(self):
        assert normalize_domain("") == ""

    def test_phone_last_10_digits(self):
        assert normalize_phone("+1 (404) 555-1234") == "4045551234"

    def test_phone_strips_non_digits(self):
        assert normalize_phone("(404) 555.1234") == "4045551234"

    def test_phone_too_short(self):
        assert normalize_phone("555-1234") == "5551234"

    def test_phone_empty(self):
        assert normalize_phone("") == ""

    def test_name_lowercases_and_strips_punctuation(self):
        assert normalize_name("Dr. Smith's Clinic!") == "dr smith s clinic"

    def test_name_collapses_whitespace(self):
        assert normalize_name("  Atlanta   OB/GYN  ") == "atlanta ob gyn"

    def test_address_uses_full_address_when_available(self):
        result = normalize_address("123 Main St", "Atlanta", "GA", "30301")
        assert "123 main st" in result

    def test_address_falls_back_to_parts(self):
        result = normalize_address("", "Atlanta", "GA", "30301")
        assert "atlanta" in result and "ga" in result


# ---------------------------------------------------------------------------
# Matcher tests
# ---------------------------------------------------------------------------

class TestMatcher:
    def _indexes_from(self, *entries):
        reg = _registry_with(*entries)
        return build_indexes(reg["entries"]), reg["entries"]

    def test_match_by_place_id(self):
        indexes, entries = self._indexes_from(
            _entry(entry_id="e1", google_place_id="ChIJabc123")
        )
        fields = {"google_place_id": "ChIJabc123", "website_domain": "", "phone_digits": "",
                  "name_normalized": "", "address_normalized": ""}
        eid, basis = find_match(fields, indexes)
        assert eid == "e1"
        assert basis == "google_place_id"

    def test_match_by_domain(self):
        indexes, entries = self._indexes_from(
            _entry(entry_id="e2", website_domain="atlantaobgyn.com")
        )
        fields = {"google_place_id": "", "website_domain": "atlantaobgyn.com",
                  "phone_digits": "", "name_normalized": "", "address_normalized": ""}
        eid, basis = find_match(fields, indexes)
        assert eid == "e2"
        assert basis == "website_domain"

    def test_match_by_phone(self):
        indexes, entries = self._indexes_from(
            _entry(entry_id="e3", phone_digits="4045551234")
        )
        fields = {"google_place_id": "", "website_domain": "", "phone_digits": "4045551234",
                  "name_normalized": "", "address_normalized": ""}
        eid, basis = find_match(fields, indexes)
        assert eid == "e3"
        assert basis == "phone"

    def test_match_by_name_address(self):
        indexes, entries = self._indexes_from(
            _entry(entry_id="e4", name_normalized="atlanta ob gyn",
                   address_normalized="123 peachtree st atlanta ga 30301")
        )
        fields = {"google_place_id": "", "website_domain": "", "phone_digits": "",
                  "name_normalized": "atlanta ob gyn",
                  "address_normalized": "123 peachtree st atlanta ga 30301"}
        eid, basis = find_match(fields, indexes)
        assert eid == "e4"
        assert basis == "name_address"

    def test_no_match(self):
        indexes, entries = self._indexes_from(
            _entry(entry_id="e5", google_place_id="ChIJxxx")
        )
        fields = {"google_place_id": "ChIJyyy", "website_domain": "other.com",
                  "phone_digits": "9999999999", "name_normalized": "unknown",
                  "address_normalized": "nowhere"}
        eid, basis = find_match(fields, indexes)
        assert eid is None
        assert basis is None

    def test_place_id_takes_priority_over_domain(self):
        indexes, entries = self._indexes_from(
            _entry(entry_id="e1", google_place_id="ChIJabc", website_domain=""),
            _entry(entry_id="e2", google_place_id="", website_domain="clinic.com"),
        )
        # Row has both place_id (matches e1) and domain (matches e2)
        fields = {"google_place_id": "ChIJabc", "website_domain": "clinic.com",
                  "phone_digits": "", "name_normalized": "", "address_normalized": ""}
        eid, basis = find_match(fields, indexes)
        assert eid == "e1"
        assert basis == "google_place_id"

    def test_short_phone_does_not_match(self):
        indexes, _ = self._indexes_from(_entry(entry_id="e1", phone_digits="1234"))
        fields = {"google_place_id": "", "website_domain": "", "phone_digits": "1234",
                  "name_normalized": "", "address_normalized": ""}
        eid, _ = find_match(fields, indexes)
        assert eid is None


# ---------------------------------------------------------------------------
# Classifier tests
# ---------------------------------------------------------------------------

class TestClassifier:
    def _run(self, row_fields, entries=None, seen=None):
        entries = entries or {}
        indexes = build_indexes(entries)
        seen = seen if seen is not None else {}
        return classify(0, row_fields, indexes, entries, seen)

    # ---------- New Place ID ----------

    def test_new_place_id(self):
        """Row with a place_id not in the registry → NEW."""
        result = self._run({
            "google_place_id": "ChIJbrand_new",
            "website_domain": "newclinic.com",
            "phone_digits": "4045550001",
            "name_normalized": "new clinic",
            "address_normalized": "1 new st atlanta ga",
        })
        assert result["classification"] == NEW
        assert result["entry_id"] is None
        assert result["match_basis"] is None

    # ---------- Known Place ID ----------

    def test_known_place_id(self):
        """Row with a place_id that IS in the registry, nothing changed → KNOWN."""
        entry = _entry(entry_id="e1", google_place_id="ChIJknown",
                       website_domain="known.com", phone_digits="4045550002")
        entries = {"e1": entry}
        result = self._run(
            {
                "google_place_id": "ChIJknown",
                "website_domain": "known.com",
                "phone_digits": "4045550002",
                "name_normalized": "test practice",
                "address_normalized": "",
                "practice_name": "Test Practice",
                "google_category": "",
            },
            entries=entries,
        )
        assert result["classification"] == KNOWN
        assert result["entry_id"] == "e1"
        assert result["match_basis"] == "google_place_id"
        assert result["changed_fields"] == []

    # ---------- Changed website ----------

    def test_changed_website(self):
        """Row matches by place_id but website domain changed → CHANGED."""
        entry = _entry(entry_id="e1", google_place_id="ChIJchanged",
                       website_domain="old-domain.com")
        entries = {"e1": entry}
        result = self._run(
            {
                "google_place_id": "ChIJchanged",
                "website_domain": "new-domain.com",
                "phone_digits": "",
                "name_normalized": "",
                "address_normalized": "",
                "google_category": "",
                "practice_name": "",
            },
            entries=entries,
        )
        assert result["classification"] == CHANGED
        assert result["entry_id"] == "e1"
        website_changes = [c for c in result["changed_fields"] if c["field"] == "website_domain"]
        assert len(website_changes) == 1
        assert website_changes[0]["old"] == "old-domain.com"
        assert website_changes[0]["new"] == "new-domain.com"

    # ---------- Changed phone ----------

    def test_changed_phone(self):
        """Row matches by place_id but phone number changed → CHANGED."""
        entry = _entry(entry_id="e1", google_place_id="ChIJph",
                       phone_digits="4045550001")
        entries = {"e1": entry}
        result = self._run(
            {
                "google_place_id": "ChIJph",
                "website_domain": "",
                "phone_digits": "4045559999",
                "name_normalized": "",
                "address_normalized": "",
                "google_category": "",
                "practice_name": "",
            },
            entries=entries,
        )
        assert result["classification"] == CHANGED
        phone_changes = [c for c in result["changed_fields"] if c["field"] == "phone_digits"]
        assert len(phone_changes) == 1
        assert phone_changes[0]["old"] == "4045550001"
        assert phone_changes[0]["new"] == "4045559999"

    # ---------- Multiple changes ----------

    def test_changed_multiple_fields(self):
        """Both website and phone changed — both appear in changed_fields."""
        entry = _entry(entry_id="e1", google_place_id="ChIJmulti",
                       website_domain="old.com", phone_digits="4045550001")
        entries = {"e1": entry}
        result = self._run(
            {
                "google_place_id": "ChIJmulti",
                "website_domain": "new.com",
                "phone_digits": "4045559999",
                "name_normalized": "",
                "address_normalized": "",
                "google_category": "",
                "practice_name": "",
            },
            entries=entries,
        )
        assert result["classification"] == CHANGED
        changed_field_names = {c["field"] for c in result["changed_fields"]}
        assert "website_domain" in changed_field_names
        assert "phone_digits" in changed_field_names

    # ---------- Possible duplicate ----------

    def test_possible_duplicate_by_name_address(self):
        """
        Two rows in the same upload with the same name + address.
        The second row → POSSIBLE_DUPLICATE pointing to the first row.
        """
        shared_fields = {
            "google_place_id": "",
            "website_domain": "",
            "phone_digits": "",
            "name_normalized": "atlanta ob gyn",
            "address_normalized": "123 peachtree atlanta ga",
            "practice_name": "Atlanta OB/GYN",
            "google_category": "",
        }
        indexes = build_indexes({})
        seen: dict = {}

        # Row 0 — not in registry → NEW, gets registered in seen
        r0 = classify(0, shared_fields, indexes, {}, seen)
        assert r0["classification"] == NEW

        # Row 1 — same name+address, not in registry → POSSIBLE_DUPLICATE of row 0
        r1 = classify(1, shared_fields, indexes, {}, seen)
        assert r1["classification"] == POSSIBLE_DUPLICATE
        assert r1["duplicate_of_row_idx"] == 0
        assert r1["match_basis"] == "name_address"

    def test_possible_duplicate_by_place_id(self):
        """Intra-upload duplicate via google_place_id."""
        fields = {
            "google_place_id": "ChIJdup",
            "website_domain": "dup.com",
            "phone_digits": "4045550001",
            "name_normalized": "dup practice",
            "address_normalized": "1 dup st ga",
        }
        indexes = build_indexes({})
        seen: dict = {}

        r0 = classify(0, fields, indexes, {}, seen)
        assert r0["classification"] == NEW

        r1 = classify(1, fields, indexes, {}, seen)
        assert r1["classification"] == POSSIBLE_DUPLICATE
        assert r1["match_basis"] == "google_place_id"

    # ---------- Insufficient data ----------

    def test_insufficient_data_no_identifiers(self):
        """Row with no place_id, no website, no phone, no name → INSUFFICIENT_DATA."""
        result = self._run({
            "google_place_id": "",
            "website_domain": "",
            "phone_digits": "",
            "name_normalized": "",
            "address_normalized": "",
        })
        assert result["classification"] == INSUFFICIENT_DATA

    def test_insufficient_data_name_without_address(self):
        """Name alone without address is not sufficient."""
        result = self._run({
            "google_place_id": "",
            "website_domain": "",
            "phone_digits": "",
            "name_normalized": "some clinic",
            "address_normalized": "",
        })
        assert result["classification"] == INSUFFICIENT_DATA

    def test_sufficient_data_phone_alone(self):
        """10-digit phone alone is sufficient."""
        assert has_sufficient_data({
            "google_place_id": "",
            "website_domain": "",
            "phone_digits": "4045551234",
            "name_normalized": "",
            "address_normalized": "",
        })

    def test_sufficient_data_domain_alone(self):
        """Website domain alone is sufficient."""
        assert has_sufficient_data({
            "google_place_id": "",
            "website_domain": "clinic.com",
            "phone_digits": "",
            "name_normalized": "",
            "address_normalized": "",
        })

    # ---------- Registry takes priority over intra-upload match ----------

    def test_registry_match_beats_intra_upload(self):
        """
        If row matches the registry AND another row in the upload, registry wins.
        The row should be KNOWN/CHANGED, not POSSIBLE_DUPLICATE.
        """
        entry = _entry(entry_id="e1", website_domain="clinic.com")
        entries = {"e1": entry}
        indexes = build_indexes(entries)
        seen: dict = {}

        # Seed seen_in_upload as if an earlier row used the same domain
        seen.setdefault("domain", {})["clinic.com"] = 0

        result = classify(
            1,
            {
                "google_place_id": "",
                "website_domain": "clinic.com",
                "phone_digits": "",
                "name_normalized": "",
                "address_normalized": "",
                "practice_name": "Clinic",
                "google_category": "",
            },
            indexes,
            entries,
            seen,
        )
        assert result["classification"] in (KNOWN, CHANGED)
        assert result["entry_id"] == "e1"


# ---------------------------------------------------------------------------
# Outscraper adapter tests
# ---------------------------------------------------------------------------

class TestOutscraperAdapter:
    def test_parse_csv_basic(self):
        csv_bytes = _make_csv(
            {"name": "Atlanta OBGYN", "phone": "(404) 555-1234",
             "site": "https://www.atlantaobgyn.com", "place_id": "ChIJabc"}
        )
        rows = parse_csv(csv_bytes)
        assert len(rows) == 1
        assert rows[0]["name"] == "Atlanta OBGYN"

    def test_parse_csv_lowercases_keys(self):
        csv_bytes = b"Name,Phone,Site\nClinic,5550001,clinic.com\n"
        rows = parse_csv(csv_bytes)
        assert "name" in rows[0]
        assert "Name" not in rows[0]

    def test_extract_fields_normalizes(self):
        row = {
            "name": "Atlanta OB/GYN",
            "phone": "(404) 555-1234",
            "site": "https://www.atlantaobgyn.com",
            "place_id": "ChIJabc",
            "city": "Atlanta",
            "state": "GA",
        }
        fields = extract_fields(row)
        assert fields["website_domain"] == "atlantaobgyn.com"
        assert fields["phone_digits"] == "4045551234"
        assert fields["name_normalized"] == "atlanta ob gyn"
        assert fields["google_place_id"] == "ChIJabc"

    def test_extract_fields_handles_missing_columns(self):
        """Missing columns should produce empty strings, not KeyErrors."""
        row = {"name": "Minimal Clinic"}
        fields = extract_fields(row)
        assert fields["website_domain"] == ""
        assert fields["phone_digits"] == ""
        assert fields["google_place_id"] == ""

    def test_extract_fields_alternative_url_columns(self):
        for col in ("website", "url", "web_url", "website_address"):
            row = {col: "https://clinic.com"}
            fields = extract_fields(row)
            assert fields["website_domain"] == "clinic.com", f"Failed for column {col!r}"


# ---------------------------------------------------------------------------
# Registry I/O tests
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_load_missing_file_returns_empty(self, tmp_path):
        reg = load_registry(tmp_path / "nonexistent.json")
        assert reg["entries"] == {}
        assert reg["entry_count"] == 0

    def test_save_and_reload(self, tmp_path):
        path = tmp_path / "registry.json"
        reg = empty_registry()
        reg["entries"]["e1"] = _entry(entry_id="e1", google_place_id="ChIJtest")
        save_registry(reg, path)
        loaded = load_registry(path)
        assert "e1" in loaded["entries"]
        assert loaded["entries"]["e1"]["google_place_id"] == "ChIJtest"

    def test_save_is_atomic(self, tmp_path):
        """save_registry should not leave a .tmp file behind."""
        path = tmp_path / "registry.json"
        save_registry(empty_registry(), path)
        tmp = tmp_path / "registry.json.tmp"
        assert not tmp.exists()

    def test_load_tolerates_missing_entries_key(self, tmp_path):
        path = tmp_path / "registry.json"
        path.write_text('{"version": "1", "updated_at": "2026-01-01T00:00:00+00:00"}',
                        encoding="utf-8")
        reg = load_registry(path)
        assert reg["entries"] == {}


# ---------------------------------------------------------------------------
# Writer tests
# ---------------------------------------------------------------------------

class TestWriter:
    def _run_write(self, records, fields_by_idx, registry, tmp_path):
        write_results(
            records=records,
            row_fields_by_idx=fields_by_idx,
            registry=registry,
            output_dir=tmp_path,
            run_id="disc_test_001",
            started_at="2026-06-15T00:00:00+00:00",
            finished_at="2026-06-15T00:01:00+00:00",
        )

    def test_all_four_files_written(self, tmp_path):
        records = [{"row_idx": 0, "classification": NEW, "match_basis": None,
                    "entry_id": None, "changed_fields": [], "duplicate_of_row_idx": None}]
        fields = {0: {"practice_name": "Test Clinic", "website_url": "test.com",
                      "website_domain": "test.com", "phone": "", "phone_digits": "",
                      "google_place_id": "", "google_category": "", "npi": "",
                      "address_city": "Atlanta", "address_state": "GA", "address_zip": "",
                      "name_normalized": "test clinic", "address_normalized": ""}}
        self._run_write(records, fields, empty_registry(), tmp_path)

        assert (tmp_path / "discovery_results.json").exists()
        assert (tmp_path / "discovery_results.csv").exists()
        assert (tmp_path / "discovery_run_log.json").exists()
        assert (tmp_path / "updated_registry_preview.json").exists()

    def test_run_log_counts(self, tmp_path):
        records = [
            {"row_idx": 0, "classification": NEW, "match_basis": None,
             "entry_id": None, "changed_fields": [], "duplicate_of_row_idx": None},
            {"row_idx": 1, "classification": KNOWN, "match_basis": "google_place_id",
             "entry_id": "e1", "changed_fields": [], "duplicate_of_row_idx": None},
        ]
        self._run_write(records, {0: {}, 1: {}}, empty_registry(), tmp_path)
        log = json.loads((tmp_path / "discovery_run_log.json").read_text())
        assert log["classification_counts"][NEW] == 1
        assert log["classification_counts"][KNOWN] == 1

    def test_registry_preview_does_not_overwrite_source_registry(self, tmp_path):
        """
        The source registry must not be modified — only updated_registry_preview.json
        is written to output_dir.
        """
        src_dir = tmp_path / "data"
        src_dir.mkdir()
        registry_path = src_dir / "master_practice_registry.json"

        reg = empty_registry()
        reg["entries"]["existing_e1"] = _entry(entry_id="existing_e1",
                                               google_place_id="ChIJexisting")
        save_registry(reg, registry_path)
        original_text = registry_path.read_text()

        # Run a discovery that would add a NEW record
        records = [{"row_idx": 0, "classification": NEW, "match_basis": None,
                    "entry_id": None, "changed_fields": [], "duplicate_of_row_idx": None}]
        fields = {0: {"practice_name": "Brand New Clinic", "website_domain": "new.com",
                      "website_url": "https://new.com", "phone": "", "phone_digits": "",
                      "google_place_id": "ChIJnew", "google_category": "", "npi": "",
                      "address_city": "Atlanta", "address_state": "GA", "address_zip": "",
                      "name_normalized": "brand new clinic", "address_normalized": ""}}

        output_dir = tmp_path / "run_output"
        write_results(
            records=records,
            row_fields_by_idx=fields,
            registry=reg,
            output_dir=output_dir,
            run_id="disc_test_002",
            started_at="2026-06-15T00:00:00+00:00",
            finished_at="2026-06-15T00:01:00+00:00",
        )

        # Source registry unchanged
        assert registry_path.read_text() == original_text

        # Preview exists in output dir and has more entries
        preview = json.loads((output_dir / "updated_registry_preview.json").read_text())
        assert preview["is_preview"] is True
        assert len(preview["entries"]) == 2  # existing + new
        assert "existing_e1" in preview["entries"]

    def test_preview_includes_changed_fields_in_change_log(self, tmp_path):
        """CHANGED records update the entry in the preview and append to change_log."""
        entry = _entry(entry_id="e1", google_place_id="ChIJch",
                       website_domain="old.com", phone_digits="")
        reg = _registry_with(entry)

        records = [{
            "row_idx": 0,
            "classification": CHANGED,
            "match_basis": "google_place_id",
            "entry_id": "e1",
            "changed_fields": [{"field": "website_domain", "label": "Website",
                                 "old": "old.com", "new": "new.com"}],
            "duplicate_of_row_idx": None,
        }]
        fields = {0: {"website_domain": "new.com", "name_normalized": "",
                      "address_normalized": "", "phone_digits": "",
                      "google_category": "", "practice_name": "",
                      "google_place_id": "ChIJch"}}

        preview = _build_preview_registry(records, fields, reg, "disc_test_003")
        assert preview["entries"]["e1"]["website_domain"] == "new.com"
        assert len(preview["entries"]["e1"]["change_log"]) == 1


# ---------------------------------------------------------------------------
# End-to-end run_discovery tests
# ---------------------------------------------------------------------------

class TestRunDiscovery:
    def test_new_record_classified_correctly(self, tmp_path):
        csv_bytes = _make_csv({
            "name": "Brand New Clinic",
            "phone": "(404) 555-0001",
            "site": "https://brandnew.com",
            "place_id": "ChIJbrandnew",
        })
        registry_path = tmp_path / "registry.json"
        output_dir = tmp_path / "run"
        result = run_discovery(csv_bytes, registry_path, output_dir)
        assert result.counts[NEW] == 1
        assert result.counts[KNOWN] == 0

    def test_known_record_classified_correctly(self, tmp_path):
        registry_path = tmp_path / "registry.json"
        entry = _entry(entry_id="e1", google_place_id="ChIJknown",
                       website_domain="knownpractice.com", practice_name="Known Practice")
        save_registry(_registry_with(entry), registry_path)

        csv_bytes = _make_csv({
            "name": "Known Practice",
            "phone": "",
            "site": "https://www.knownpractice.com",
            "place_id": "ChIJknown",
        })
        result = run_discovery(csv_bytes, registry_path, tmp_path / "run")
        assert result.counts[KNOWN] == 1
        assert result.counts[NEW] == 0

    def test_changed_website_end_to_end(self, tmp_path):
        registry_path = tmp_path / "registry.json"
        entry = _entry(entry_id="e1", google_place_id="ChIJch",
                       website_domain="oldsite.com")
        save_registry(_registry_with(entry), registry_path)

        csv_bytes = _make_csv({
            "name": "The Practice",
            "phone": "",
            "site": "https://newsite.com",
            "place_id": "ChIJch",
        })
        result = run_discovery(csv_bytes, registry_path, tmp_path / "run")
        assert result.counts[CHANGED] == 1
        changed = result.changed
        assert len(changed) == 1
        assert any(c["field"] == "website_domain" for c in changed[0]["changed_fields"])

    def test_possible_duplicate_end_to_end(self, tmp_path):
        """Two identical rows in the CSV → first is NEW, second is POSSIBLE_DUPLICATE."""
        csv_bytes = _make_csv(
            {"name": "Dupe Clinic", "phone": "(770) 555-1234",
             "site": "https://dupeclinic.com", "place_id": ""},
            {"name": "Dupe Clinic", "phone": "(770) 555-1234",
             "site": "https://dupeclinic.com", "place_id": ""},
        )
        result = run_discovery(csv_bytes, tmp_path / "registry.json", tmp_path / "run")
        assert result.counts[NEW] == 1
        assert result.counts[POSSIBLE_DUPLICATE] == 1

    def test_insufficient_data_end_to_end(self, tmp_path):
        csv_bytes = _make_csv({"name": "", "phone": "", "site": "", "place_id": ""})
        result = run_discovery(csv_bytes, tmp_path / "registry.json", tmp_path / "run")
        assert result.counts[INSUFFICIENT_DATA] == 1

    def test_output_files_exist(self, tmp_path):
        csv_bytes = _make_csv({"name": "Test Clinic", "phone": "4045550001",
                                "site": "test.com", "place_id": "ChIJtest"})
        output_dir = tmp_path / "run"
        run_discovery(csv_bytes, tmp_path / "registry.json", output_dir)
        assert (output_dir / "discovery_results.json").exists()
        assert (output_dir / "discovery_results.csv").exists()
        assert (output_dir / "discovery_run_log.json").exists()
        assert (output_dir / "updated_registry_preview.json").exists()

    def test_source_registry_not_overwritten(self, tmp_path):
        """run_discovery must never write to the source registry path."""
        registry_path = tmp_path / "registry.json"
        entry = _entry(entry_id="e1", google_place_id="ChIJsrc")
        save_registry(_registry_with(entry), registry_path)
        before = registry_path.read_text()

        csv_bytes = _make_csv({"name": "New Practice", "phone": "4045550001",
                                "site": "newpractice.com", "place_id": "ChIJbrandnew"})
        run_discovery(csv_bytes, registry_path, tmp_path / "run")
        assert registry_path.read_text() == before

    def test_run_id_propagates_to_output(self, tmp_path):
        csv_bytes = _make_csv({"name": "Test", "phone": "4045550001",
                                "site": "t.com", "place_id": "ChIJt"})
        output_dir = tmp_path / "run"
        result = run_discovery(csv_bytes, tmp_path / "r.json", output_dir,
                               run_id="disc_custom_id")
        log = json.loads((output_dir / "discovery_run_log.json").read_text())
        assert log["run_id"] == "disc_custom_id"
        assert result.run_id == "disc_custom_id"

    def test_empty_registry_all_new(self, tmp_path):
        """When no registry exists, every row should be NEW."""
        csv_bytes = _make_csv(
            {"name": "Clinic A", "phone": "4045550001", "site": "a.com", "place_id": "ChIJa"},
            {"name": "Clinic B", "phone": "4045550002", "site": "b.com", "place_id": "ChIJb"},
        )
        result = run_discovery(csv_bytes, tmp_path / "no_registry.json", tmp_path / "run")
        assert result.counts[NEW] == 2
        assert result.counts[KNOWN] == 0

    def test_preview_not_written_to_source_registry_path(self, tmp_path):
        """The preview must be in output_dir, not at the source registry path."""
        registry_path = tmp_path / "data" / "registry.json"
        registry_path.parent.mkdir()
        save_registry(empty_registry(), registry_path)

        csv_bytes = _make_csv({"name": "New", "phone": "4045550001",
                                "site": "new.com", "place_id": "ChIJnew"})
        output_dir = tmp_path / "run"
        run_discovery(csv_bytes, registry_path, output_dir)

        # Preview is in output_dir
        assert (output_dir / "updated_registry_preview.json").exists()
        # Source registry path has no preview marker
        src_data = json.loads(registry_path.read_text())
        assert "is_preview" not in src_data
