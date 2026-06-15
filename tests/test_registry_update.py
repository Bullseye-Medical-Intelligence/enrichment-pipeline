"""
tests/test_registry_update.py
Tests for explicit, operator-triggered registry updates
(pipeline-api/registry_update.py).

Deterministic — no network, no subprocess. The enrichment run output and the
registry are seeded on the filesystem; the routes are driven via TestClient.
"""

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

import runs  # noqa: E402
import registry_update  # noqa: E402


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _record(rid, **over):
    base = {
        "id": rid,
        "practice_name": "Alpha Women's Health",
        "specialty": "OBGYN",
        "npi_optional": "",
        "website_url": "https://alpha-clinic.com",
        "phone": "(404) 555-1000",
        "address_city": "Atlanta",
        "address_state": "GA",
        "address_zip": "30301",
        "bullseye_score": 92,
        "exclusion_status": "CLEAR",
        "target_tier": "Bullseye",
        "enrichment_status": "complete",
        "source_pipeline_version": "v1.0",
    }
    base.update(over)
    return base


def _seed_enrichment_run(runs_dir, run_id, records, *, status="complete",
                         run_type="enrichment", source_discovery_run_id=None):
    rd = runs_dir / run_id
    rd.mkdir(parents=True, exist_ok=True)
    st = {
        "run_id": run_id, "project_id": "femasys", "source_type": "outscraper",
        "input_filename": "in.csv", "status": status, "operator": "tester",
        "created_at": "2026-06-15T10:00:00+00:00", "run_type": run_type,
    }
    if source_discovery_run_id:
        st["source_discovery_run_id"] = source_discovery_run_id
    (rd / "status.json").write_text(json.dumps(st), encoding="utf-8")
    (rd / "enriched_targets.json").write_text(
        json.dumps({"run_id": run_id, "records": records}), encoding="utf-8")
    return rd


@pytest.fixture
def client(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_dir)
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        yield c, runs_dir


def _update(client, run_id, **body):
    return client.post(f"/enrichment-runs/{run_id}/update-registry", json=body)


def _load_registry(runs_dir):
    return json.loads(registry_update.registry_path().read_text())


# ---------------------------------------------------------------------------
# Insert / update / history
# ---------------------------------------------------------------------------

def test_insert_new_bullseye_clear_record(client):
    c, runs_dir = client
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [_record("T-1")])
    r = _update(c, "RUN-20260615-100000-aaaa", selection_mode="bullseye_only")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registry_update_count"] == 1
    assert body["inserted_count"] == 1

    reg = _load_registry(runs_dir)
    assert reg["entry_count"] == 1
    entry = next(iter(reg["entries"].values()))
    assert entry["practice_name"] == "Alpha Women's Health"
    assert entry["current_tier"] == "Bullseye"
    assert entry["exclusion_status"] == "CLEAR"
    assert entry["last_enrichment_run_id"] == "RUN-20260615-100000-aaaa"
    assert "practice_registry_id" in entry
    assert entry["change_history"] == []


def test_update_changed_website_preserves_history(client):
    c, runs_dir = client
    # First run inserts.
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [_record("T-1")])
    _update(c, "RUN-20260615-100000-aaaa", selection_mode="bullseye_only")

    # Second run, same practice (matches by phone/name), changed website.
    _seed_enrichment_run(runs_dir, "RUN-20260615-110000-bbbb",
                         [_record("T-1", website_url="https://alpha-new.com")])
    r = _update(c, "RUN-20260615-110000-bbbb", selection_mode="bullseye_only")
    assert r.status_code == 200
    assert r.json()["updated_count"] == 1

    reg = _load_registry(runs_dir)
    assert reg["entry_count"] == 1  # matched, not duplicated
    entry = next(iter(reg["entries"].values()))
    assert entry["website_url"] == "https://alpha-new.com"
    hist = entry["change_history"]
    assert len(hist) == 1
    assert hist[0]["field"] == "website_url"
    assert hist[0]["old"] == "https://alpha-clinic.com"
    assert hist[0]["new"] == "https://alpha-new.com"


def test_idempotent_second_run_no_duplicate_history(client):
    c, runs_dir = client
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [_record("T-1")])
    _update(c, "RUN-20260615-100000-aaaa", selection_mode="bullseye_only")
    # Identical second push.
    _seed_enrichment_run(runs_dir, "RUN-20260615-110000-bbbb", [_record("T-1")])
    _update(c, "RUN-20260615-110000-bbbb", selection_mode="bullseye_only")

    reg = _load_registry(runs_dir)
    entry = next(iter(reg["entries"].values()))
    assert entry["change_history"] == []  # nothing meaningful changed


# ---------------------------------------------------------------------------
# Rejection rules
# ---------------------------------------------------------------------------

def test_excluded_rejected_by_default(client):
    c, runs_dir = client
    rec = _record("T-1", exclusion_status="EXCLUDED", target_tier="Excluded")
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [rec])
    r = _update(c, "RUN-20260615-100000-aaaa", selected_record_ids=["T-1"])
    assert r.status_code == 200
    body = r.json()
    assert body["registry_update_count"] == 0
    assert any(x["record_id"] == "T-1" for x in body["rejected"])
    assert not registry_update.registry_path().exists()  # nothing written


def test_include_excluded_allows_explicit_update(client):
    c, runs_dir = client
    rec = _record("T-1", exclusion_status="EXCLUDED", target_tier="Excluded")
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [rec])
    r = _update(c, "RUN-20260615-100000-aaaa",
                selected_record_ids=["T-1"], include_excluded=True)
    assert r.status_code == 200
    assert r.json()["registry_update_count"] == 1
    entry = next(iter(_load_registry(runs_dir)["entries"].values()))
    assert entry["exclusion_status"] == "EXCLUDED"


def test_needs_review_rejected_by_default(client):
    c, runs_dir = client
    rec = _record("T-1", enrichment_status="needs_review")
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [rec])
    r = _update(c, "RUN-20260615-100000-aaaa", selected_record_ids=["T-1"])
    assert r.json()["registry_update_count"] == 0
    assert any("needs_review" in x["reason"] for x in r.json()["rejected"])


def test_needs_review_allowed_with_flag(client):
    c, runs_dir = client
    rec = _record("T-1", enrichment_status="needs_review")
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [rec])
    r = _update(c, "RUN-20260615-100000-aaaa",
                selected_record_ids=["T-1"], include_needs_review=True)
    assert r.json()["registry_update_count"] == 1


def test_failed_record_always_rejected(client):
    c, runs_dir = client
    rec = _record("T-1", enrichment_status="failed")
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [rec])
    # Even with both override flags, failed is rejected.
    r = _update(c, "RUN-20260615-100000-aaaa", selected_record_ids=["T-1"],
                include_excluded=True, include_needs_review=True)
    assert r.json()["registry_update_count"] == 0
    assert any("failed" in x["reason"] for x in r.json()["rejected"])


def test_missing_identity_rejected(client):
    c, runs_dir = client
    rec = _record("T-1", practice_name="", website_url="", phone="",
                  address_city="", address_state="", address_zip="")
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [rec])
    r = _update(c, "RUN-20260615-100000-aaaa", selected_record_ids=["T-1"])
    assert r.json()["registry_update_count"] == 0
    assert any("identity" in x["reason"] for x in r.json()["rejected"])


# ---------------------------------------------------------------------------
# Ambiguous match
# ---------------------------------------------------------------------------

def test_ambiguous_duplicate_rejected(client):
    c, runs_dir = client
    # Seed a registry with two entries: one matches by domain, the other by phone.
    reg_path = registry_update.registry_path()
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps({
        "version": "1", "entries": {
            "A": {"entry_id": "A", "website_domain": "alpha-clinic.com",
                  "phone_digits": "", "name_normalized": "", "address_normalized": ""},
            "B": {"entry_id": "B", "website_domain": "", "phone_digits": "4045551000",
                  "name_normalized": "", "address_normalized": ""},
        }}), encoding="utf-8")
    before = reg_path.read_bytes()

    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [_record("T-1")])
    r = _update(c, "RUN-20260615-100000-aaaa", selected_record_ids=["T-1"])
    assert r.status_code == 200
    body = r.json()
    assert body["registry_update_count"] == 0
    assert any(x["record_id"] == "T-1" for x in body["needs_manual_merge"])
    assert reg_path.read_bytes() == before  # registry untouched


# ---------------------------------------------------------------------------
# Selection modes & run gating
# ---------------------------------------------------------------------------

def test_clear_only_excludes_excluded(client):
    c, runs_dir = client
    recs = [
        _record("T-1"),
        _record("T-2", exclusion_status="EXCLUDED", target_tier="Excluded",
                website_url="https://beta.com", phone="(404) 555-2000"),
    ]
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", recs)
    r = _update(c, "RUN-20260615-100000-aaaa", selection_mode="clear_only")
    assert r.json()["registry_update_count"] == 1  # only the CLEAR one


def test_rejects_non_enrichment_run(client):
    c, runs_dir = client
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [_record("T-1")],
                         run_type="discovery")
    r = _update(c, "RUN-20260615-100000-aaaa", selection_mode="bullseye_only")
    assert r.status_code == 400
    assert "not an enrichment run" in r.json()["detail"]


def test_rejects_incomplete_run(client):
    c, runs_dir = client
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [_record("T-1")],
                         status="running")
    r = _update(c, "RUN-20260615-100000-aaaa", selection_mode="bullseye_only")
    assert r.status_code == 400
    assert "not complete" in r.json()["detail"]


def test_rejects_unknown_run(client):
    c, runs_dir = client
    r = _update(c, "RUN-20260615-100000-9999", selection_mode="bullseye_only")
    assert r.status_code == 404


def test_requires_selection_input(client):
    c, runs_dir = client
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [_record("T-1")])
    r = _update(c, "RUN-20260615-100000-aaaa")
    assert r.status_code == 400


def test_require_source_discovery_run_id(client):
    c, runs_dir = client
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [_record("T-1")])
    r = _update(c, "RUN-20260615-100000-aaaa", selection_mode="bullseye_only",
                require_source_discovery_run_id=True)
    assert r.status_code == 400
    assert "source_discovery_run_id" in r.json()["detail"]

    # With a source discovery run id present, it proceeds.
    _seed_enrichment_run(runs_dir, "RUN-20260615-110000-bbbb", [_record("T-1")],
                         source_discovery_run_id="RUN-20260615-090000-dddd")
    r2 = _update(c, "RUN-20260615-110000-bbbb", selection_mode="bullseye_only",
                 require_source_discovery_run_id=True)
    assert r2.status_code == 200
    entry = next(iter(_load_registry(runs_dir)["entries"].values()))
    assert entry["last_discovery_run_id"] == "RUN-20260615-090000-dddd"


# ---------------------------------------------------------------------------
# Logging, status, atomicity
# ---------------------------------------------------------------------------

def test_registry_update_log_written(client):
    c, runs_dir = client
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [_record("T-1")])
    _update(c, "RUN-20260615-100000-aaaa", selection_mode="bullseye_only")
    log_path = runs_dir / "RUN-20260615-100000-aaaa" / "registry_update_log.json"
    assert log_path.exists()
    log = json.loads(log_path.read_text())
    assert log["registry_update_count"] == 1
    assert log["inserted"] == ["T-1"]
    assert log["selection_mode"] == "bullseye_only"


def test_status_json_updated(client):
    c, runs_dir = client
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [_record("T-1")])
    _update(c, "RUN-20260615-100000-aaaa", selection_mode="bullseye_only")
    status = json.loads((runs_dir / "RUN-20260615-100000-aaaa" / "status.json").read_text())
    assert status["registry_update_count"] == 1
    assert status["registry_updated_at"]
    assert status["registry_update_log_path"].endswith("registry_update_log.json")


def test_registry_write_atomic_source_valid_on_failure(client, monkeypatch):
    c, runs_dir = client
    # Pre-existing valid registry.
    reg_path = registry_update.registry_path()
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps({"version": "1", "entries": {}}), encoding="utf-8")
    before = reg_path.read_bytes()

    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [_record("T-1")])

    # Make the atomic replace fail mid-write.
    real_replace = os.replace

    def boom(src, dst):
        if str(dst).endswith("master_practice_registry.json"):
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(registry_update.os, "replace", boom)

    with pytest.raises(OSError):
        registry_update.update_registry_from_run(
            "RUN-20260615-100000-aaaa", selection_mode="bullseye_only")

    # Original registry is untouched and still valid; no stray temp files.
    assert reg_path.read_bytes() == before
    json.loads(reg_path.read_text())  # still parses
    assert not list(reg_path.parent.glob("*.tmp"))


def test_no_registry_update_on_empty_mode_selection(client):
    """A mode that matches nothing writes no registry and reports zero."""
    c, runs_dir = client
    rec = _record("T-1", target_tier="Contender")  # not Bullseye
    _seed_enrichment_run(runs_dir, "RUN-20260615-100000-aaaa", [rec])
    r = _update(c, "RUN-20260615-100000-aaaa", selection_mode="bullseye_only")
    assert r.status_code == 200
    assert r.json()["registry_update_count"] == 0
    assert not registry_update.registry_path().exists()
