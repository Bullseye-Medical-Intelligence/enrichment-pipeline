"""
tests/test_lifecycle.py
Full four-stage lifecycle integration test: discovery → send-to-enrichment →
enrichment completion (simulated) → registry update → idempotency → place_id match.

All subprocesses and enrichment I/O are monkeypatched — no real CLI, no LLM, no
network. Validates that the field contracts and status.json stamps hold across the
entire pipeline boundary.
"""

import csv
import io
import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "pipeline-api"

os.environ.setdefault("PIPELINE_API_KEY", "test-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO_ROOT))

sys.path.insert(0, str(_API_DIR))

import runs          # noqa: E402
import runner        # noqa: E402
import discovery_runs  # noqa: E402
import registry_update  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PLACE_ID_BULLSEYE = "ChIJLifecycleBS"

_DISCOVERY_RECORDS = [
    {
        "row_idx": 0, "classification": "NEW", "match_basis": "", "entry_id": "",
        "changed_fields": [], "duplicate_of_row_idx": None,
        "practice_name": "Bullseye Women's Health", "website_url": "bullseye-wh.com",
        "phone": "(404) 100-0001", "address_full": "1 Bull St",
        "address_city": "Atlanta", "address_state": "GA", "address_zip": "30301",
        "google_place_id": _PLACE_ID_BULLSEYE, "google_category": "OBGYN", "npi": "1234567890",
    },
    {
        "row_idx": 1, "classification": "CHANGED", "match_basis": "domain", "entry_id": "e-existing",
        "changed_fields": [{"field": "phone", "label": "Phone", "old": "555-0000", "new": "555-9999"}],
        "duplicate_of_row_idx": None,
        "practice_name": "Contender Practice", "website_url": "contender.com",
        "phone": "(404) 100-0002", "address_full": "2 Contend Ave",
        "address_city": "Decatur", "address_state": "GA", "address_zip": "30030",
        "google_place_id": "", "google_category": "OBGYN", "npi": "",
    },
    {
        "row_idx": 2, "classification": "KNOWN", "match_basis": "phone", "entry_id": "e-known",
        "changed_fields": [], "duplicate_of_row_idx": None,
        "practice_name": "Known Clinic", "website_url": "known.com",
        "phone": "(404) 100-0003", "address_full": "", "address_city": "Marietta",
        "address_state": "GA", "address_zip": "30060",
        "google_place_id": "", "google_category": "OBGYN", "npi": "",
    },
    {
        "row_idx": 3, "classification": "POSSIBLE_DUPLICATE", "match_basis": "", "entry_id": "",
        "changed_fields": [], "duplicate_of_row_idx": 0,
        "practice_name": "Bullseye WH East", "website_url": "bullseye-wh.com",
        "phone": "(404) 100-0004", "address_full": "",
        "address_city": "Atlanta", "address_state": "GA", "address_zip": "30301",
        "google_place_id": "", "google_category": "OBGYN", "npi": "",
    },
    {
        "row_idx": 4, "classification": "INSUFFICIENT_DATA", "match_basis": "", "entry_id": "",
        "changed_fields": [], "duplicate_of_row_idx": None,
        "practice_name": "", "website_url": "", "phone": "",
        "address_full": "", "address_city": "", "address_state": "", "address_zip": "",
        "google_place_id": "", "google_category": "", "npi": "",
    },
]

_ENRICHED_RECORDS = [
    {
        "id": "T-bull",
        "practice_name": "Bullseye Women's Health",
        "specialty": "OBGYN",
        "website_url": "https://bullseye-wh.com",
        "phone": "(404) 100-0001",
        "address_city": "Atlanta",
        "address_state": "GA",
        "address_zip": "30301",
        "npi_optional": "1234567890",
        "google_place_id": _PLACE_ID_BULLSEYE,
        "bullseye_score": 92,
        "exclusion_status": "CLEAR",
        "target_tier": "Bullseye",
        "enrichment_status": "complete",
        "source_pipeline_version": "v1.0",
    },
    {
        "id": "T-cont",
        "practice_name": "Contender Practice",
        "specialty": "OBGYN",
        "website_url": "https://contender.com",
        "phone": "(404) 100-0002",
        "address_city": "Decatur",
        "address_state": "GA",
        "address_zip": "30030",
        "npi_optional": "",
        "google_place_id": "",
        "bullseye_score": 68,
        "exclusion_status": "CLEAR",
        "target_tier": "Contender",
        "enrichment_status": "complete",
        "source_pipeline_version": "v1.0",
    },
]


def _csv_bytes(rows, fields):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def _outscraper_csv():
    fields = ["name", "phone", "site", "full_address", "city", "state", "postal_code"]
    return _csv_bytes([
        {"name": "Bullseye Women's Health", "phone": "(404) 100-0001", "site": "bullseye-wh.com",
         "full_address": "1 Bull St", "city": "Atlanta", "state": "GA", "postal_code": "30301"},
        {"name": "Contender Practice", "phone": "(404) 100-0002", "site": "contender.com",
         "full_address": "2 Contend Ave", "city": "Decatur", "state": "GA", "postal_code": "30030"},
    ], fields)


@pytest.fixture
def lifecycle_client(tmp_path, monkeypatch):
    """TestClient with both subprocess boundaries stubbed for lifecycle testing."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_dir)

    def fake_spawn(input_csv, registry, output_dir, run_id):
        import subprocess
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "discovery_results.json").write_text(
            json.dumps({"run_id": run_id, "records": _DISCOVERY_RECORDS}),
            encoding="utf-8")
        summary = {
            "run_id": run_id, "status": "complete", "total_imported": 5,
            "new_count": 1, "changed_count": 1, "known_count": 1,
            "possible_duplicate_count": 1, "insufficient_data_count": 1,
        }
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(summary).encode(), stderr=b"")

    monkeypatch.setattr(discovery_runs, "_spawn_discovery", fake_spawn)

    async def fake_ingest(file, source_type, project_id, operator, background_tasks):
        csv_bytes = await file.read()
        rid = runs.generate_run_id()
        runs.create_run(rid, project_id, source_type, file.filename, operator, 2)
        runs.update_run_status(rid, status="ingested")
        (runs_dir / rid / "input.csv").write_bytes(csv_bytes)
        return rid, 2

    monkeypatch.setattr(runner, "orchestrate_ingest", fake_ingest)

    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        yield c, runs_dir


# ---------------------------------------------------------------------------
# Stage 1 — Discovery run
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_stage1_discovery_run_counts(self, lifecycle_client):
        c, runs_dir = lifecycle_client

        r = c.post("/discovery-runs",
                   files={"file": ("prospects.csv", _outscraper_csv(), "text/csv")})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["run_id"].startswith("RUN-")
        assert body["status"] == "complete"
        assert body["new_count"] == 1
        assert body["changed_count"] == 1
        assert body["known_count"] == 1
        assert body["possible_duplicate_count"] == 1
        assert body["insufficient_data_count"] == 1

    # -------------------------------------------------------------------
    # Stage 2 — Send to enrichment (new_and_changed selection)
    # -------------------------------------------------------------------

    def test_stage2_send_to_enrichment(self, lifecycle_client):
        c, runs_dir = lifecycle_client

        # Create discovery run first.
        disc = c.post("/discovery-runs",
                      files={"file": ("prospects.csv", _outscraper_csv(), "text/csv")})
        disc_id = disc.json()["run_id"]

        r = c.post(f"/discovery-runs/{disc_id}/send-to-enrichment",
                   json={"project_id": "femasys", "selection_mode": "new_and_changed"})
        assert r.status_code == 201, r.text
        body = r.json()

        # Only NEW and CHANGED — KNOWN, POSSIBLE_DUPLICATE, INSUFFICIENT_DATA excluded.
        assert body["selected_count"] == 2
        assert body["discovery_run_id"] == disc_id

        enr_id = body["enrichment_run_id"]
        assert enr_id

        # Handoff CSV must exist with traceability columns.
        handoff_path = runs.run_dir(disc_id) / "enrichment_handoff.csv"
        assert handoff_path.exists()
        rows = list(csv.DictReader(io.StringIO(handoff_path.read_text(encoding="utf-8"))))
        assert len(rows) == 2
        for col in ("discovery_run_id", "discovery_status", "name", "place_id"):
            assert col in rows[0], f"handoff missing column: {col}"
        new_row = next(r for r in rows if r["discovery_status"] == "NEW")
        assert new_row["place_id"] == _PLACE_ID_BULLSEYE

        # Enrichment run stamped with source discovery run.
        enr_status = runs.get_run(enr_id)
        assert enr_status.status == "ingested"
        assert enr_status.source_discovery_run_id == disc_id

    # -------------------------------------------------------------------
    # Stage 3 — Simulate enrichment completion (write enriched_targets.json)
    # -------------------------------------------------------------------

    def _simulate_enrichment_complete(self, enr_id):
        runs.update_run_status(enr_id, status="complete")
        rd = runs.run_dir(enr_id)
        (rd / "enriched_targets.json").write_text(
            json.dumps({"run_id": enr_id, "records": _ENRICHED_RECORDS}),
            encoding="utf-8")

    def test_stage3_enriched_targets_written(self, lifecycle_client):
        c, runs_dir = lifecycle_client
        disc = c.post("/discovery-runs",
                      files={"file": ("prospects.csv", _outscraper_csv(), "text/csv")})
        disc_id = disc.json()["run_id"]
        send = c.post(f"/discovery-runs/{disc_id}/send-to-enrichment",
                      json={"project_id": "femasys", "selection_mode": "new_and_changed"})
        enr_id = send.json()["enrichment_run_id"]
        self._simulate_enrichment_complete(enr_id)

        targets = json.loads((runs.run_dir(enr_id) / "enriched_targets.json").read_text())
        assert len(targets["records"]) == 2
        bull = next(r for r in targets["records"] if r["target_tier"] == "Bullseye")
        assert bull["google_place_id"] == _PLACE_ID_BULLSEYE

    # -------------------------------------------------------------------
    # Stage 4 — Registry update: 2 records inserted
    # -------------------------------------------------------------------

    def _full_setup(self, lifecycle_client):
        """Run stages 1–3 and return (client, runs_dir, disc_id, enr_id)."""
        c, runs_dir = lifecycle_client
        disc = c.post("/discovery-runs",
                      files={"file": ("prospects.csv", _outscraper_csv(), "text/csv")})
        disc_id = disc.json()["run_id"]
        send = c.post(f"/discovery-runs/{disc_id}/send-to-enrichment",
                      json={"project_id": "femasys", "selection_mode": "new_and_changed"})
        enr_id = send.json()["enrichment_run_id"]
        self._simulate_enrichment_complete(enr_id)
        return c, runs_dir, disc_id, enr_id

    def test_stage4_registry_update_inserts_two_records(self, lifecycle_client):
        c, runs_dir, disc_id, enr_id = self._full_setup(lifecycle_client)

        r = c.post(f"/enrichment-runs/{enr_id}/update-registry",
                   json={"selection_mode": "all_reviewable"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["registry_update_count"] == 2
        assert body["inserted_count"] == 2

        reg = json.loads(registry_update.registry_path().read_text())
        assert reg["entry_count"] == 2

        log_path = runs.run_dir(enr_id) / "registry_update_log.json"
        assert log_path.exists()

        # No change_history on first insert.
        for entry in reg["entries"].values():
            assert entry.get("change_history") == []

    # -------------------------------------------------------------------
    # Stage 5 — Idempotency: re-run update, change_history stays empty
    # -------------------------------------------------------------------

    def test_stage5_idempotent_update(self, lifecycle_client):
        c, runs_dir, disc_id, enr_id = self._full_setup(lifecycle_client)
        c.post(f"/enrichment-runs/{enr_id}/update-registry",
               json={"selection_mode": "all_reviewable"})
        # Run again.
        r = c.post(f"/enrichment-runs/{enr_id}/update-registry",
                   json={"selection_mode": "all_reviewable"})
        assert r.status_code == 200, r.text
        reg = json.loads(registry_update.registry_path().read_text())
        assert reg["entry_count"] == 2
        for entry in reg["entries"].values():
            assert entry.get("change_history") == []

    # -------------------------------------------------------------------
    # Stage 6 — place_id match: Bullseye entry carries google_place_id
    # -------------------------------------------------------------------

    def test_stage6_place_id_preserved_in_registry(self, lifecycle_client):
        c, runs_dir, disc_id, enr_id = self._full_setup(lifecycle_client)
        c.post(f"/enrichment-runs/{enr_id}/update-registry",
               json={"selection_mode": "all_reviewable"})

        reg = json.loads(registry_update.registry_path().read_text())
        entries = list(reg["entries"].values())
        bull = next(
            (e for e in entries if e.get("practice_name") == "Bullseye Women's Health"),
            None,
        )
        assert bull is not None, "Bullseye entry missing from registry"
        assert bull["google_place_id"] == _PLACE_ID_BULLSEYE

    # -------------------------------------------------------------------
    # Stage boundary: KNOWN and INSUFFICIENT_DATA never reach enrichment
    # -------------------------------------------------------------------

    def test_known_and_insufficient_excluded_from_handoff(self, lifecycle_client):
        c, runs_dir = lifecycle_client
        disc = c.post("/discovery-runs",
                      files={"file": ("prospects.csv", _outscraper_csv(), "text/csv")})
        disc_id = disc.json()["run_id"]
        send = c.post(f"/discovery-runs/{disc_id}/send-to-enrichment",
                      json={"project_id": "femasys", "selection_mode": "new_and_changed"})
        assert send.status_code == 201

        handoff = (runs.run_dir(disc_id) / "enrichment_handoff.csv").read_text(encoding="utf-8")
        rows = list(csv.DictReader(io.StringIO(handoff)))
        statuses = {r["discovery_status"] for r in rows}
        assert "KNOWN" not in statuses
        assert "INSUFFICIENT_DATA" not in statuses
        assert "POSSIBLE_DUPLICATE" not in statuses
