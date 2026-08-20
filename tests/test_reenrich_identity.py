"""
tests/test_reenrich_identity.py

A re-enrich scratch run rebuilds one record from a reconstructed one-row CSV.
Two things must survive that round trip: the record's id, so the merge can find
it, and the consolidated provider roster, so the merge does not destroy it.

Both were broken. Step 1d re-derived the location identity from the scratch row
and stamped the result over `id`, which produced "the browser re-crawl did not
return the expected record (id mismatch)". And the merge replaced the record
wholesale, so a location built from several provider rows came back as one row.

Deterministic — no network, no subprocess, no pipeline spawn.
"""

import csv
import io
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_API_DIR = _REPO / "pipeline-api"
sys.path.insert(0, str(_API_DIR))
sys.path.insert(0, str(_REPO))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO))

import runner  # noqa: E402
from ingestion.consolidator import consolidate_records  # noqa: E402
from ingestion.manual_adapter import load_manual_csv  # noqa: E402

_LOCATION = {
    "practice_name": "Forever Care OB/GYN LLC",
    "website_url": "https://www.forevercareobgyn.com",
    "phone": "+1 770-495-4935",
    "address_street": "3855 Pleasant Hill Rd",
    "address_unit": "Suite 200",
    "address_city": "Duluth",
    "address_state": "GA",
    "address_zip": "30096",
    "specialty": "OBGYN",
}


def _consolidate(rows, tmp_path, name="in.csv"):
    """Ingest rows through the manual adapter + Step 1d, as a real run does."""
    path = tmp_path / name
    fields = sorted({k for row in rows for k in row})
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    path.write_text(buf.getvalue(), encoding="utf-8")
    records, _ = consolidate_records(load_manual_csv(str(path)), {})
    return records


def _reingest_scratch_csv(scratch_csv: Path, run_config: dict):
    """Ingest a scratch input.csv the way the spawned pipeline would."""
    records, _ = consolidate_records(load_manual_csv(str(scratch_csv)), run_config)
    return records


# ---------------------------------------------------------------------------
# The id survives the scratch round trip
# ---------------------------------------------------------------------------

def test_scratch_csv_round_trip_keeps_the_record_id(tmp_path):
    """The reported failure. The source record's id must come back unchanged, or
    the merge cannot find it and reports an id mismatch."""
    source = _consolidate([{**_LOCATION, "npi_optional": "1043654130"}], tmp_path)[0]

    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    scratch_csv = runner._write_single_record_csv(
        scratch_dir, source, source["website_url"])
    config = json.loads(
        runner._write_scratch_config(scratch_dir, _config_file(tmp_path)).read_text())

    rebuilt = _reingest_scratch_csv(scratch_csv, config)

    assert len(rebuilt) == 1
    assert rebuilt[0]["id"] == source["id"]


def test_scratch_csv_carries_street_and_unit(tmp_path):
    """The address fields the location identity is derived from. Without them the
    identity fell back to domain or phone and produced a different id."""
    source = _consolidate([_LOCATION], tmp_path)[0]
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    scratch_csv = runner._write_single_record_csv(
        scratch_dir, source, source["website_url"])

    row = next(csv.DictReader(io.StringIO(scratch_csv.read_text(encoding="utf-8"))))
    assert row["address_street"] == "3855 Pleasant Hill Rd"
    assert row["address_unit"] == "Suite 200"


def test_id_survives_even_if_consolidation_were_left_on(tmp_path):
    """Belt and braces: carrying the address keeps the derived id stable, so the
    fix does not rest on the config override alone."""
    source = _consolidate([_LOCATION], tmp_path)[0]
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    scratch_csv = runner._write_single_record_csv(
        scratch_dir, source, source["website_url"])

    rebuilt = _reingest_scratch_csv(scratch_csv, {})  # consolidation ON

    assert rebuilt[0]["id"] == source["id"]


def test_multi_record_scratch_csv_carries_street_and_unit(tmp_path):
    """The batch re-enrich path writes its own CSV and had the same gap."""
    source = _consolidate([_LOCATION], tmp_path)[0]
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    scratch_csv = runner._write_multi_record_csv(scratch_dir, [source])

    row = next(csv.DictReader(io.StringIO(scratch_csv.read_text(encoding="utf-8"))))
    assert row["address_street"] == "3855 Pleasant Hill Rd"
    assert row["address_unit"] == "Suite 200"


# ---------------------------------------------------------------------------
# The scratch config turns consolidation off
# ---------------------------------------------------------------------------

def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "project_config_snapshot.json"
    path.write_text(json.dumps({
        "client_name": "TestClient", "target_specialty": "OBGYN",
        "target_geography": ["GA"], "max_pages_per_practice": 5,
    }), encoding="utf-8")
    return path


def test_scratch_config_disables_consolidation(tmp_path):
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    written = runner._write_scratch_config(scratch_dir, _config_file(tmp_path))

    assert json.loads(written.read_text())["consolidation"]["enabled"] is False


def test_scratch_config_preserves_the_frozen_snapshot(tmp_path):
    """Geography, specialty and client rules must survive — the re-crawl scores
    by the same rules as the original run."""
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    data = json.loads(
        runner._write_scratch_config(scratch_dir, _config_file(tmp_path)).read_text())

    assert data["target_specialty"] == "OBGYN"
    assert data["target_geography"] == ["GA"]
    assert data["client_name"] == "TestClient"


def test_scratch_config_applies_caller_overrides(tmp_path):
    """The excluded-record path clears specialty/geography through the same helper."""
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    data = json.loads(runner._write_scratch_config(
        scratch_dir, _config_file(tmp_path),
        target_specialty="", target_geography=[]).read_text())

    assert data["target_specialty"] == ""
    assert data["target_geography"] == []
    assert data["consolidation"]["enabled"] is False


# ---------------------------------------------------------------------------
# The merge refreshes enrichment, preserves identity
# ---------------------------------------------------------------------------

def _source_record():
    return {
        "id": "P-abc123", "practice_name": "Forever Care OB/GYN LLC",
        "address_street": "3855 Pleasant Hill Rd", "address_unit": "Suite 200",
        "providers": [{"name": "Xuan Shirley Cao", "npi": "1043654130"},
                      {"name": "Amara Osei", "npi": "1043654131"}],
        "provider_count": 2, "source_row_ids": ["T-1", "T-2"],
        "consolidation": {"matched_fields": ["address", "unit"], "match_score": 10},
        "group_id": "G-1", "location_index": 0, "location_count": 2,
        "npi": "1043654130", "specialty": "OBGYN",
        "signals": [{"signal_id": "S-01", "signal_state": "not_found"}],
        "bullseye_score": 12, "target_tier": "Manual Review",
        "source_confidence": "limited", "sales_angle": [],
        "verification": {"verified_at": "2026-08-01T00:00:00Z",
                         "recommended_action": "hold"},
    }


def _scratch_result():
    """What the one-row scratch run produces: enrichment, but a thin roster."""
    return {
        "id": "P-abc123", "practice_name": "Forever Care OB/GYN LLC",
        "providers": [{"name": "Forever Care OB/GYN LLC"}],
        "provider_count": 1, "source_row_ids": ["T-scratch"],
        "consolidation": {"matched_fields": [], "match_score": 0},
        "group_id": "", "location_index": 0, "location_count": 0,
        "signals": [{"signal_id": "S-01", "signal_state": "yes"}],
        "bullseye_score": 84, "target_tier": "Bullseye",
        "source_confidence": "complete", "sales_angle": ["Publishes self-pay pricing."],
    }


def test_merge_preserves_the_consolidated_provider_roster():
    """A location built from several provider rows must not come back as one."""
    merged = runner._merge_reenriched_fields(_source_record(), _scratch_result())

    assert merged["provider_count"] == 2
    assert [p["name"] for p in merged["providers"]] == ["Xuan Shirley Cao", "Amara Osei"]
    assert merged["source_row_ids"] == ["T-1", "T-2"]


def test_merge_preserves_consolidation_and_group_fields():
    merged = runner._merge_reenriched_fields(_source_record(), _scratch_result())

    assert merged["consolidation"]["match_score"] == 10
    assert merged["group_id"] == "G-1"
    assert merged["location_count"] == 2
    assert merged["address_street"] == "3855 Pleasant Hill Rd"
    assert merged["npi"] == "1043654130"


def test_merge_refreshes_the_enrichment_fields():
    """The point of the re-crawl still has to land."""
    merged = runner._merge_reenriched_fields(_source_record(), _scratch_result())

    assert merged["signals"][0]["signal_state"] == "yes"
    assert merged["bullseye_score"] == 84
    assert merged["target_tier"] == "Bullseye"
    assert merged["source_confidence"] == "complete"
    assert merged["sales_angle"] == ["Publishes self-pay pricing."]


def test_merge_drops_a_stale_verification_verdict():
    """The verification object judged the signals that were just replaced."""
    merged = runner._merge_reenriched_fields(_source_record(), _scratch_result())

    assert "verification" not in merged


def test_merge_does_not_mutate_either_input():
    source, scratch = _source_record(), _scratch_result()
    before = json.dumps(source, sort_keys=True)
    runner._merge_reenriched_fields(source, scratch)
    assert json.dumps(source, sort_keys=True) == before
