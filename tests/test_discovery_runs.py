"""
tests/test_discovery_runs.py
Tests for the API Discovery Run support (pipeline-api/discovery_runs.py) and the
discovery_cli.py subprocess wrapper.

Two layers:
  - Engine/CLI integration: actually runs discovery_cli.py as a subprocess. This
    is deterministic (no network, no LLM) and proves the wrapper + real discovery
    engine write the four files and never mutate the registry source.
  - Route/orchestration: drives the FastAPI routes via TestClient with the
    subprocess boundary stubbed, so the orchestration (run dir, status.json,
    summary, results) is exercised without spawning a process.
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

# Required env BEFORE importing config-bound API modules.
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO_ROOT))

sys.path.insert(0, str(_API_DIR))

import runs  # noqa: E402
import runner  # noqa: E402
import discovery_runs  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _csv_bytes(rows: list[dict], fieldnames: list[str]) -> bytes:
    """Build an Outscraper-style CSV from row dicts."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue().encode("utf-8")


_FIELDS = ["name", "phone", "site", "full_address", "city", "state", "postal_code"]


def _two_row_csv() -> bytes:
    """One row that matches the seeded registry entry, one brand-new row."""
    return _csv_bytes(
        [
            {"name": "Existing Practice", "phone": "(555) 123-4567", "site": "",
             "full_address": "", "city": "Atlanta", "state": "GA", "postal_code": "30301"},
            {"name": "Brand New Clinic", "phone": "(555) 999-8888", "site": "newclinic.com",
             "full_address": "1 Main St", "city": "Atlanta", "state": "GA", "postal_code": "30302"},
        ],
        _FIELDS,
    )


def _seed_registry(path: Path) -> None:
    """Write a registry with a single entry matching 'Existing Practice' by phone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    registry = {
        "version": "1",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "entry_count": 1,
        "entries": {
            "e1": {
                "entry_id": "e1",
                "google_place_id": "",
                "website_domain": "",
                "phone_digits": "5551234567",
                "name_normalized": "existing practice",
                "address_normalized": "",
                "practice_name": "Existing Practice",
            }
        },
    }
    path.write_text(json.dumps(registry, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Engine / CLI integration (real subprocess — deterministic, no network)
# ---------------------------------------------------------------------------

class TestDiscoveryCliIntegration:
    def _run_cli(self, input_csv, registry, output_dir, run_id):
        return subprocess.run(
            [sys.executable, str(_REPO_ROOT / "discovery_cli.py"),
             "--input", str(input_csv), "--registry", str(registry),
             "--output-dir", str(output_dir), "--run-id", run_id],
            capture_output=True, cwd=str(_REPO_ROOT), timeout=120,
        )

    def test_cli_writes_four_files_and_summary(self, tmp_path):
        input_csv = tmp_path / "input.csv"
        input_csv.write_bytes(_two_row_csv())
        registry = tmp_path / "master_practice_registry.json"
        _seed_registry(registry)
        out_dir = tmp_path / "run"
        out_dir.mkdir()

        proc = self._run_cli(input_csv, registry, out_dir, "RUN-20260615-120000-aaaa")
        assert proc.returncode == 0, proc.stderr.decode()

        summary = json.loads(proc.stdout.decode())
        assert summary["total_imported"] == 2
        assert summary["new_count"] == 1
        assert summary["known_count"] == 1
        assert summary["status"] == "complete"

        for fname in ("discovery_results.json", "discovery_results.csv",
                      "discovery_run_log.json", "updated_registry_preview.json"):
            assert (out_dir / fname).exists(), f"missing {fname}"

    def test_cli_does_not_mutate_registry_source(self, tmp_path):
        input_csv = tmp_path / "input.csv"
        input_csv.write_bytes(_two_row_csv())
        registry = tmp_path / "master_practice_registry.json"
        _seed_registry(registry)
        before = registry.read_bytes()
        out_dir = tmp_path / "run"
        out_dir.mkdir()

        proc = self._run_cli(input_csv, registry, out_dir, "RUN-20260615-120000-bbbb")
        assert proc.returncode == 0, proc.stderr.decode()

        # Source registry is byte-identical; only the preview reflects the new entry.
        assert registry.read_bytes() == before
        preview = json.loads((out_dir / "updated_registry_preview.json").read_text())
        assert preview.get("is_preview") is True
        assert len(preview["entries"]) == 2  # original + the NEW row

    def test_cli_missing_input_fails_cleanly(self, tmp_path):
        proc = self._run_cli(tmp_path / "nope.csv", tmp_path / "reg.json",
                             tmp_path, "RUN-20260615-120000-cccc")
        assert proc.returncode == 1
        assert "error" in json.loads(proc.stdout.decode())


# ---------------------------------------------------------------------------
# Route / orchestration (subprocess boundary stubbed)
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with runs rooted at a tmp dir and the discovery subprocess stubbed."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_dir)

    # Stub the subprocess: write a minimal results file and return a summary,
    # mirroring what discovery_cli.py would produce.
    def fake_spawn(input_csv, registry, output_dir, run_id):
        (Path(output_dir) / "discovery_results.json").write_text(
            json.dumps({"run_id": run_id, "records": [
                {"row_idx": 0, "classification": "NEW"},
                {"row_idx": 1, "classification": "KNOWN"},
            ]}), encoding="utf-8")
        summary = {
            "run_id": run_id, "status": "complete", "total_imported": 2,
            "new_count": 1, "changed_count": 0, "known_count": 1,
            "possible_duplicate_count": 0, "insufficient_data_count": 0,
        }
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(summary).encode(), stderr=b"")

    monkeypatch.setattr(discovery_runs, "_spawn_discovery", fake_spawn)

    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        yield c


def _upload(client, content: bytes, filename="prospects.csv"):
    return client.post(
        "/discovery-runs",
        files={"file": (filename, content, "text/csv")},
    )


def test_create_discovery_run(client):
    r = _upload(client, _two_row_csv())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["run_type"] == "discovery"
    assert body["status"] == "complete"
    assert body["total_imported"] == 2
    assert body["new_count"] == 1
    assert body["known_count"] == 1
    assert body["output_paths"]["results_json"] == "discovery_results.json"
    assert body["run_id"].startswith("RUN-")


def test_get_discovery_status(client):
    run_id = _upload(client, _two_row_csv()).json()["run_id"]
    r = client.get(f"/discovery-runs/{run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert body["run_type"] == "discovery"
    assert body["total_imported"] == 2


def test_get_discovery_status_unknown_run(client):
    r = client.get("/discovery-runs/RUN-20260615-120000-9999")
    assert r.status_code == 404


def test_get_discovery_results(client):
    run_id = _upload(client, _two_row_csv()).json()["run_id"]
    r = client.get(f"/discovery-runs/{run_id}/results")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert len(body["records"]) == 2


def test_malformed_csv_fails_cleanly(client):
    # Missing the required 'phone' column for outscraper source.
    bad = _csv_bytes([{"name": "No Phone Clinic"}], ["name"])
    r = _upload(client, bad)
    assert r.status_code == 400
    assert "detail" in r.json()


def test_not_a_csv_fails_cleanly(client):
    r = _upload(client, b"\x00\x01\x02 not a csv", filename="junk.bin")
    assert r.status_code == 400


def test_discovery_run_requires_auth(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_dir)
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:  # no login
        r = c.get("/discovery-runs/RUN-20260615-120000-0000", follow_redirects=False)
        assert r.status_code in (302, 401, 403)


# ---------------------------------------------------------------------------
# Isolation: discovery runs must not appear in the enrichment run listing
# ---------------------------------------------------------------------------

def test_list_runs_excludes_discovery_runs(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_dir)

    # An enrichment run (no run_type) and a discovery run.
    enr = runs_dir / "RUN-20260615-100000-aaaa"
    enr.mkdir()
    (enr / "status.json").write_text(json.dumps({
        "run_id": "RUN-20260615-100000-aaaa", "status": "complete",
        "source_type": "outscraper", "records_input": 5, "bullseye_count": 1,
        "contender_count": 0, "excluded_count": 0, "error_count": 0,
        "created_at": "2026-06-15T10:00:00+00:00",
    }))
    disc = runs_dir / "RUN-20260615-110000-bbbb"
    disc.mkdir()
    (disc / "status.json").write_text(json.dumps({
        "run_id": "RUN-20260615-110000-bbbb", "run_type": "discovery",
        "status": "complete", "created_at": "2026-06-15T11:00:00+00:00",
    }))

    listed = {s.run_id for s in runs.list_runs()}
    assert "RUN-20260615-100000-aaaa" in listed
    assert "RUN-20260615-110000-bbbb" not in listed


def test_registry_not_mutated_by_run(client, tmp_path, monkeypatch):
    """Through the stubbed route the registry source file is never written."""
    reg = discovery_runs.registry_path()
    _seed_registry(reg)
    before = reg.read_bytes()
    _upload(client, _two_row_csv())
    assert reg.read_bytes() == before


# ---------------------------------------------------------------------------
# Send selected discovery records to enrichment
# ---------------------------------------------------------------------------

_DISCOVERY_RECORDS = [
    {"row_idx": 0, "classification": "NEW", "match_basis": "", "entry_id": "",
     "changed_fields": [], "duplicate_of_row_idx": None,
     "practice_name": "Alpha Clinic", "website_url": "alpha.com", "phone": "(404) 111-0000",
     "address_full": "1 Peachtree St", "address_city": "Atlanta", "address_state": "GA",
     "address_zip": "30301", "google_place_id": "PID-ALPHA", "google_category": "Fertility clinic",
     "npi": "1111111111"},
    {"row_idx": 1, "classification": "CHANGED", "match_basis": "phone", "entry_id": "e1",
     "changed_fields": [{"field": "website_domain", "label": "Website",
                         "old": "old.com", "new": "beta.com"}],
     "duplicate_of_row_idx": None,
     "practice_name": "Beta Practice", "website_url": "beta.com", "phone": "(404) 222-0000",
     "address_full": "2 Main St", "address_city": "Decatur", "address_state": "GA",
     "address_zip": "30030", "google_place_id": "", "google_category": "OBGYN", "npi": ""},
    {"row_idx": 2, "classification": "KNOWN", "match_basis": "domain", "entry_id": "e2",
     "changed_fields": [], "duplicate_of_row_idx": None,
     "practice_name": "Gamma Group", "website_url": "gamma.com", "phone": "(404) 333-0000",
     "address_full": "", "address_city": "Marietta", "address_state": "GA",
     "address_zip": "30060", "google_place_id": "", "google_category": "OBGYN", "npi": ""},
    {"row_idx": 3, "classification": "POSSIBLE_DUPLICATE", "match_basis": "", "entry_id": "",
     "changed_fields": [], "duplicate_of_row_idx": 0,
     "practice_name": "Alpha Clinic East", "website_url": "alpha.com", "phone": "(404) 111-0001",
     "address_full": "", "address_city": "Atlanta", "address_state": "GA",
     "address_zip": "30301", "google_place_id": "", "google_category": "Fertility clinic",
     "npi": ""},
    {"row_idx": 4, "classification": "INSUFFICIENT_DATA", "match_basis": "", "entry_id": "",
     "changed_fields": [], "duplicate_of_row_idx": None,
     "practice_name": "", "website_url": "", "phone": "",
     "address_full": "", "address_city": "", "address_state": "", "address_zip": "",
     "google_place_id": "", "google_category": "", "npi": ""},
]


def _seed_discovery_run(runs_dir, run_id, *, status="complete", run_type="discovery",
                        records=None):
    """Write a discovery status.json + discovery_results.json into runs_dir/run_id."""
    rd = runs_dir / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "status.json").write_text(json.dumps({
        "run_id": run_id, "run_type": run_type, "status": status,
        "created_at": "2026-06-15T12:00:00+00:00",
        "completed_at": "2026-06-15T12:00:05+00:00",
        "total_imported": len(records or _DISCOVERY_RECORDS),
    }), encoding="utf-8")
    (rd / "discovery_results.json").write_text(json.dumps({
        "run_id": run_id, "records": records if records is not None else _DISCOVERY_RECORDS,
    }), encoding="utf-8")
    return rd


@pytest.fixture
def handoff_client(tmp_path, monkeypatch):
    """TestClient with the enrichment runner stubbed (no real subprocess).

    Yields (client, runs_dir, captured) where captured records the runner call.
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_dir)

    captured: dict = {"calls": 0}

    async def fake_ingest(file, source_type, project_id, operator, background_tasks):
        captured["calls"] += 1
        captured["csv"] = await file.read()
        captured["source_type"] = source_type
        captured["project_id"] = project_id
        captured["operator"] = operator
        rid = runs.generate_run_id()
        runs.create_run(rid, project_id, source_type, file.filename, operator, 0)
        captured["enrichment_run_id"] = rid
        return rid, 0

    monkeypatch.setattr(runner, "orchestrate_ingest", fake_ingest)

    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        yield c, runs_dir, captured


def _send(client, run_id, **body):
    return client.post(f"/discovery-runs/{run_id}/send-to-enrichment", json=body)


def test_send_explicit_new_record(handoff_client):
    client, runs_dir, cap = handoff_client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d001")
    r = _send(client, "RUN-20260615-120000-d001",
              project_id="femasys", selected_record_ids=[0])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["selected_count"] == 1
    assert body["discovery_run_id"] == "RUN-20260615-120000-d001"
    assert body["enrichment_run_id"] == cap["enrichment_run_id"]
    assert body["handoff_csv_path"].endswith("enrichment_handoff.csv")
    assert cap["calls"] == 1
    assert cap["source_type"] == "outscraper"


def test_selection_mode_new_only(handoff_client):
    client, runs_dir, cap = handoff_client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d002")
    r = _send(client, "RUN-20260615-120000-d002",
              project_id="femasys", selection_mode="new_only")
    assert r.status_code == 201, r.text
    assert r.json()["selected_count"] == 1  # only the NEW record


def test_selection_mode_new_and_changed(handoff_client):
    client, runs_dir, cap = handoff_client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d003")
    r = _send(client, "RUN-20260615-120000-d003",
              project_id="femasys", selection_mode="new_and_changed")
    assert r.status_code == 201, r.text
    assert r.json()["selected_count"] == 2  # NEW + CHANGED


def test_rejects_incomplete_discovery_run(handoff_client):
    client, runs_dir, cap = handoff_client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d004", status="running")
    r = _send(client, "RUN-20260615-120000-d004",
              project_id="femasys", selection_mode="new_only")
    assert r.status_code == 400
    assert "complete" in r.json()["detail"].lower()
    assert cap["calls"] == 0


def test_rejects_wrong_run_type(handoff_client):
    client, runs_dir, cap = handoff_client
    # An enrichment run, not a discovery run.
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d005", run_type="enrichment")
    r = _send(client, "RUN-20260615-120000-d005",
              project_id="femasys", selection_mode="new_only")
    assert r.status_code == 404
    assert cap["calls"] == 0


def test_rejects_no_actionable_records(handoff_client):
    client, runs_dir, cap = handoff_client
    only_known = [r for r in _DISCOVERY_RECORDS if r["classification"] == "KNOWN"]
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d006", records=only_known)
    r = _send(client, "RUN-20260615-120000-d006",
              project_id="femasys", selection_mode="new_and_changed")
    assert r.status_code == 400
    assert cap["calls"] == 0


def test_explicit_known_record_rejected(handoff_client):
    client, runs_dir, cap = handoff_client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d007")
    r = _send(client, "RUN-20260615-120000-d007",
              project_id="femasys", selected_record_ids=[2])  # KNOWN
    assert r.status_code == 400
    assert cap["calls"] == 0


def test_possible_duplicate_requires_explicit_selection(handoff_client):
    client, runs_dir, cap = handoff_client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d008")
    # all_actionable mode does NOT include POSSIBLE_DUPLICATE → only NEW + CHANGED.
    r1 = _send(client, "RUN-20260615-120000-d008",
               project_id="femasys", selection_mode="all_actionable")
    assert r1.status_code == 201
    assert r1.json()["selected_count"] == 2

    # Explicit selection of the POSSIBLE_DUPLICATE row is allowed.
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d009")
    r2 = _send(client, "RUN-20260615-120000-d009",
               project_id="femasys", selected_record_ids=[3])
    assert r2.status_code == 201, r2.text
    assert r2.json()["selected_count"] == 1


def test_requires_selection_input(handoff_client):
    client, runs_dir, cap = handoff_client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d010")
    r = _send(client, "RUN-20260615-120000-d010", project_id="femasys")
    assert r.status_code == 400
    assert cap["calls"] == 0


def test_requires_project_id(handoff_client):
    client, runs_dir, cap = handoff_client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d011")
    r = _send(client, "RUN-20260615-120000-d011", selection_mode="new_only")
    assert r.status_code == 400
    assert cap["calls"] == 0


def test_handoff_csv_contains_traceability_fields(handoff_client):
    client, runs_dir, cap = handoff_client
    disc_id = "RUN-20260615-120000-d012"
    _seed_discovery_run(runs_dir, disc_id)
    r = _send(client, disc_id, project_id="femasys", selection_mode="new_and_changed")
    assert r.status_code == 201

    handoff_bytes = (runs_dir / disc_id / "enrichment_handoff.csv").read_bytes()
    handoff = handoff_bytes.decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(handoff)))
    assert len(rows) == 2
    for col in ("discovery_run_id", "discovery_status", "discovery_reason",
                "matched_existing_record_id", "changed_fields",
                "name", "site", "phone", "place_id", "npi"):
        assert col in rows[0]
    new_row = next(r for r in rows if r["discovery_status"] == "NEW")
    assert new_row["discovery_run_id"] == disc_id
    assert new_row["name"] == "Alpha Clinic"
    assert new_row["place_id"] == "PID-ALPHA"
    changed_row = next(r for r in rows if r["discovery_status"] == "CHANGED")
    assert changed_row["matched_existing_record_id"] == "e1"
    assert "Website" in changed_row["changed_fields"]

    # The CSV handed to the runner matches the CSV written to the discovery folder.
    assert cap["csv"] == handoff_bytes


def test_enrichment_status_has_traceability(handoff_client):
    client, runs_dir, cap = handoff_client
    disc_id = "RUN-20260615-120000-d013"
    _seed_discovery_run(runs_dir, disc_id)
    _send(client, disc_id, project_id="femasys", selection_mode="new_only")

    enr_id = cap["enrichment_run_id"]
    status = json.loads((runs_dir / enr_id / "status.json").read_text())
    assert status["run_type"] == "enrichment"
    assert status["source_discovery_run_id"] == disc_id
    assert status["source_discovery_selection_count"] == 1
    assert status["source_discovery_selection_mode"] == "new_only"


def test_registry_byte_identical_after_handoff(handoff_client):
    client, runs_dir, cap = handoff_client
    reg = discovery_runs.registry_path()
    _seed_registry(reg)
    before = reg.read_bytes()
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d014")
    r = _send(client, "RUN-20260615-120000-d014",
              project_id="femasys", selection_mode="new_and_changed")
    assert r.status_code == 201
    assert reg.read_bytes() == before


def test_runner_invoked_through_existing_path(handoff_client):
    """The handoff must go through runner.orchestrate_ingest, not a direct import."""
    client, runs_dir, cap = handoff_client
    _seed_discovery_run(runs_dir, "RUN-20260615-120000-d015")
    _send(client, "RUN-20260615-120000-d015",
          project_id="femasys", selected_record_ids=[0])
    assert cap["calls"] == 1  # the existing runner entrypoint was used


def test_send_to_missing_discovery_run(handoff_client):
    client, runs_dir, cap = handoff_client
    r = _send(client, "RUN-20260615-120000-d999",
              project_id="femasys", selection_mode="new_only")
    assert r.status_code == 404
    assert cap["calls"] == 0
