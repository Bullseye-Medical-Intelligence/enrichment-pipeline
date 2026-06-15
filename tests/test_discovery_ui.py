"""
tests/test_discovery_ui.py
Server-rendered Market Radar / Discovery dashboard UI tests.

Drives the HTML routes via TestClient. The discovery subprocess and the
enrichment runner are stubbed so no process is spawned and no budget is spent.
"""

import csv
import io
import json
import os
import subprocess
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

import runs  # noqa: E402
import runner  # noqa: E402
import discovery_runs  # noqa: E402


def _outscraper_csv() -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["name", "phone", "site"])
    w.writeheader()
    w.writerow({"name": "Alpha Clinic", "phone": "4041110000", "site": "alpha.com"})
    return buf.getvalue().encode("utf-8")


_RECORDS = [
    {"row_idx": 0, "classification": "NEW", "match_basis": "", "entry_id": "",
     "changed_fields": [], "duplicate_of_row_idx": None, "practice_name": "Alpha Clinic",
     "website_url": "https://alpha.com", "phone": "404-111-0000",
     "address_city": "Atlanta", "address_state": "GA", "address_zip": "30301",
     "google_place_id": "PID-A", "google_category": "Fertility", "npi": ""},
    {"row_idx": 1, "classification": "KNOWN", "match_basis": "domain", "entry_id": "e1",
     "changed_fields": [], "duplicate_of_row_idx": None, "practice_name": "Beta Group",
     "website_url": "https://beta.com", "phone": "404-222-0000",
     "address_city": "Decatur", "address_state": "GA", "address_zip": "30030",
     "google_place_id": "", "google_category": "OBGYN", "npi": ""},
]


def _seed_discovery_run(runs_dir, run_id, records=_RECORDS):
    rd = runs_dir / run_id
    rd.mkdir(parents=True, exist_ok=True)
    counts = {}
    for r in records:
        counts[r["classification"]] = counts.get(r["classification"], 0) + 1
    (rd / "status.json").write_text(json.dumps({
        "run_id": run_id, "run_type": "discovery", "status": "complete",
        "created_at": "2026-06-15T12:00:00+00:00",
        "completed_at": "2026-06-15T12:00:05+00:00",
        "total_imported": len(records),
        "new_count": counts.get("NEW", 0), "changed_count": counts.get("CHANGED", 0),
        "known_count": counts.get("KNOWN", 0),
        "possible_duplicate_count": counts.get("POSSIBLE_DUPLICATE", 0),
        "insufficient_data_count": counts.get("INSUFFICIENT_DATA", 0),
    }), encoding="utf-8")
    (rd / "discovery_results.json").write_text(
        json.dumps({"run_id": run_id, "records": records}), encoding="utf-8")
    return rd


@pytest.fixture
def client(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_dir)

    captured = {"ingest_calls": 0}

    def fake_spawn(input_csv, registry, output_dir, run_id):
        (Path(output_dir) / "discovery_results.json").write_text(
            json.dumps({"run_id": run_id, "records": _RECORDS}), encoding="utf-8")
        summary = {"run_id": run_id, "status": "complete", "total_imported": 2,
                   "new_count": 1, "changed_count": 0, "known_count": 1,
                   "possible_duplicate_count": 0, "insufficient_data_count": 0}
        return subprocess.CompletedProcess([], 0, json.dumps(summary).encode(), b"")

    monkeypatch.setattr(discovery_runs, "_spawn_discovery", fake_spawn)

    async def fake_ingest(file, source_type, project_id, operator, background_tasks):
        captured["ingest_calls"] += 1
        await file.read()
        rid = runs.generate_run_id()
        runs.create_run(rid, project_id, source_type, file.filename, operator, 1)
        runs.update_run_status(rid, status="ingested")
        captured["enrichment_run_id"] = rid
        return rid, 1

    monkeypatch.setattr(runner, "orchestrate_ingest", fake_ingest)

    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        yield c, runs_dir, captured


# ---------------------------------------------------------------------------
# Landing + upload
# ---------------------------------------------------------------------------

def test_market_radar_page_loads(client):
    c, runs_dir, _ = client
    r = c.get("/discovery")
    assert r.status_code == 200
    assert "Market Radar" in r.text
    assert "New Discovery Run" in r.text
    assert "Recent Discovery Runs" in r.text


def test_market_radar_lists_recent_runs(client):
    c, runs_dir, _ = client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-aaaa")
    r = c.get("/discovery")
    assert r.status_code == 200
    assert "RUN-20260615-120000-aaaa" in r.text


def test_discovery_upload_redirects_to_results(client):
    c, runs_dir, _ = client
    r = c.post("/discovery/upload",
               files={"file": ("prospects.csv", _outscraper_csv(), "text/csv")},
               follow_redirects=False)
    assert r.status_code == 303
    assert "/discovery/runs/RUN-" in r.headers["location"]


def test_discovery_upload_rejects_bad_csv(client):
    c, runs_dir, _ = client
    bad = b"name\nNo Phone Clinic\n"  # missing required 'phone' column
    r = c.post("/discovery/upload",
               files={"file": ("bad.csv", bad, "text/csv")})
    assert r.status_code == 400
    assert "Could not process CSV" in r.text


# ---------------------------------------------------------------------------
# Results page
# ---------------------------------------------------------------------------

def test_results_page_renders_counts_and_table(client):
    c, runs_dir, _ = client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-bbbb")
    r = c.get("/discovery/runs/RUN-20260615-120000-bbbb")
    assert r.status_code == 200
    assert "Discovery Results" in r.text
    assert "Alpha Clinic" in r.text          # NEW record row
    assert "Beta Group" in r.text            # KNOWN record row
    assert "Send NEW" in r.text
    assert "Imported" in r.text


def test_results_page_unknown_run_404(client):
    c, runs_dir, _ = client
    r = c.get("/discovery/runs/RUN-20260615-120000-9999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Send to enrichment
# ---------------------------------------------------------------------------

def test_send_to_enrichment_creates_ingested_run(client):
    c, runs_dir, captured = client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-cccc")
    r = c.post("/discovery/runs/RUN-20260615-120000-cccc/send",
               data={"project_id": "femasys", "operator": "tester",
                     "selection_mode": "new_only"},
               follow_redirects=False)
    assert r.status_code == 303
    assert captured["ingest_calls"] == 1
    enr_id = captured["enrichment_run_id"]
    assert r.headers["location"] == f"/dashboard/{enr_id}"
    # The created run is an ingested enrichment run with discovery traceability.
    st = json.loads((runs_dir / enr_id / "status.json").read_text())
    assert st["status"] == "ingested"
    assert st["run_type"] == "enrichment"
    assert st["source_discovery_run_id"] == "RUN-20260615-120000-cccc"


def test_send_no_selection_redirects_with_error(client):
    c, runs_dir, captured = client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-dddd")
    r = c.post("/discovery/runs/RUN-20260615-120000-dddd/send",
               data={"project_id": "femasys", "operator": "tester",
                     "selection_mode": ""},  # no checkboxes, no mode
               follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert captured["ingest_calls"] == 0


# ---------------------------------------------------------------------------
# Registry update entry point (explicit, never automatic)
# ---------------------------------------------------------------------------

def _seed_enrichment_run(runs_dir, run_id):
    rd = runs_dir / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "status.json").write_text(json.dumps({
        "run_id": run_id, "project_id": "femasys", "source_type": "outscraper",
        "input_filename": "in.csv", "status": "complete", "operator": "tester",
        "created_at": "2026-06-15T10:00:00+00:00", "run_type": "enrichment",
    }), encoding="utf-8")
    (rd / "enriched_targets.json").write_text(json.dumps({"run_id": run_id, "records": [
        {"id": "T-1", "practice_name": "Alpha Clinic", "website_url": "https://alpha.com",
         "phone": "404-111-0000", "address_city": "Atlanta", "address_state": "GA",
         "address_zip": "30301", "specialty": "OBGYN", "bullseye_score": 92,
         "exclusion_status": "CLEAR", "target_tier": "Bullseye",
         "enrichment_status": "complete", "source_pipeline_version": "v1.0"},
    ]}), encoding="utf-8")
    return rd


def test_registry_update_page_is_explicit_not_automatic(client):
    c, runs_dir, _ = client
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-eeee")
    r = c.get("/dashboard/RUN-20260615-100000-eeee/registry-update")
    assert r.status_code == 200
    assert "Update Master Practice Registry" in r.text
    assert "never" in r.text.lower()  # copy states it never runs automatically
    # It is a button/form action, not something already applied.
    assert 'action="/dashboard/RUN-20260615-100000-eeee/registry-update"' in r.text


def test_registry_update_action_applies_and_shows_summary(client):
    c, runs_dir, _ = client
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-ffff")
    r = c.post("/dashboard/RUN-20260615-100000-ffff/registry-update")
    assert r.status_code == 200
    assert "Registry Updated" in r.text
    assert "Inserted" in r.text
    # status.json now records the explicit update.
    st = json.loads((runs_dir / "RUN-20260615-100000-ffff" / "status.json").read_text())
    assert st["registry_update_count"] == 1


def test_results_page_has_registry_update_link(client):
    """The enrichment run page exposes the explicit registry-update entry point."""
    c, runs_dir, _ = client
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-abcd")
    r = c.get("/dashboard/RUN-20260615-100000-abcd")
    assert r.status_code == 200
    assert "/dashboard/RUN-20260615-100000-abcd/registry-update" in r.text
    assert "Update Registry" in r.text
