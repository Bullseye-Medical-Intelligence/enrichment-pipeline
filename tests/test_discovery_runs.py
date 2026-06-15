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

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "pipeline-api"

# Required env BEFORE importing config-bound API modules.
os.environ.setdefault("PIPELINE_API_KEY", "test-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO_ROOT))

sys.path.insert(0, str(_API_DIR))

import runs  # noqa: E402
import discovery_runs  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _csv_bytes(rows: list[dict], fieldnames: list[str]) -> bytes:
    """Build an Outscraper-style CSV from row dicts."""
    import csv
    import io
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
