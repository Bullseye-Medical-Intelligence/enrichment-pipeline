"""
Tests for the Project + ICP Run Setup layer.

Deterministic — no network, no real subprocess. Covers project creation,
ICP listing/loading, upload rejection for a missing project, run-folder
snapshotting, the pipeline command flags, and graceful rendering of older
runs that predate projects.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# pipeline-api modules import each other by bare name; put the dir on the path.
_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

# Configure required env BEFORE importing config-bound modules.
os.environ.setdefault("PIPELINE_API_KEY", "test-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import icp_profiles  # noqa: E402
import projects  # noqa: E402
import runner  # noqa: E402
import runs  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_VALID_ICP = {
    "icp_id": "test-icp",
    "name": "Test ICP",
    "version": "icp-v1",
    "signals": [
        {
            "signal_id": "S-1",
            "signal_label": "Service listed",
            "prompt_instruction": "Is the service listed?",
            "positive_weight": 10,
        }
    ],
}

_OUTSCRAPER_CSV = (
    b"name,full_address,phone,site,type\n"
    b"Acme Clinic,123 Main St,555-1000,https://acme.example,clinic\n"
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point project + ICP + runs storage at isolated temp directories."""
    projects_dir = tmp_path / "projects"
    icp_dir = tmp_path / "icp_profiles"
    runs_dir = tmp_path / "runs"
    for d in (projects_dir, icp_dir, runs_dir):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "PROJECTS_PATH", projects_dir)
    monkeypatch.setattr(config, "ICP_PROFILES_PATH", icp_dir)
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_dir)
    monkeypatch.setattr(runner, "OUTPUT_RUNS_PATH", runs_dir)
    return {"projects": projects_dir, "icp": icp_dir, "runs": runs_dir}


def _write_icp(store, profile=_VALID_ICP):
    (store["icp"] / f"{profile['icp_id']}.json").write_text(json.dumps(profile))


class _FakeUpload:
    def __init__(self, content: bytes, filename: str = "leads.csv"):
        self._content = content
        self.filename = filename

    async def read(self) -> bytes:
        return self._content


class _FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


# ---------------------------------------------------------------------------
# Project creation
# ---------------------------------------------------------------------------

def test_create_project_writes_config(store):
    _write_icp(store)
    cfg = projects.create_project(
        project_id="P-001",
        client_name="Acme Co.",
        target_specialty="OBGYN",
        target_geography=["TX", "FL"],
        icp_profile_id="test-icp",
        created_by="tester",
    )
    assert cfg["project_id"] == "P-001"
    assert cfg["icp_profile_id"] == "test-icp"
    # Persisted and re-readable.
    again = projects.get_project("P-001")
    assert again["client_name"] == "Acme Co."
    assert again["target_geography"] == ["TX", "FL"]
    # Generic defaults applied, no specialty-specific values.
    assert again["bullseye_min_score"] == config.DEFAULT_BULLSEYE_MIN_SCORE


def test_create_project_rejects_duplicate(store):
    _write_icp(store)
    projects.create_project("P-001", "Acme", "OBGYN", ["TX"], "test-icp", "tester")
    with pytest.raises(ValueError):
        projects.create_project("P-001", "Acme", "OBGYN", ["TX"], "test-icp", "tester")


def test_create_project_rejects_missing_icp(store):
    with pytest.raises(ValueError):
        projects.create_project("P-002", "Acme", "OBGYN", ["TX"], "no-such-icp", "tester")


@pytest.mark.parametrize("bad_id", ["../escape", "has space", "", "a/b", "x" * 65])
def test_create_project_rejects_bad_id(store, bad_id):
    _write_icp(store)
    with pytest.raises(ValueError):
        projects.create_project(bad_id, "Acme", "OBGYN", ["TX"], "test-icp", "tester")


def test_validate_config_missing_fields():
    with pytest.raises(ValueError):
        projects.validate_config({"project_id": "P-1"})  # missing the rest


def test_list_projects(store):
    _write_icp(store)
    projects.create_project("P-001", "Acme", "OBGYN", ["TX"], "test-icp", "tester")
    projects.create_project("P-002", "Beta", "Cardiology", ["CA"], "test-icp", "tester")
    ids = [p["project_id"] for p in projects.list_projects()]
    assert ids == ["P-001", "P-002"]


def test_project_dir_rejects_traversal(store):
    with pytest.raises(ValueError):
        projects.project_dir("../../etc")


# ---------------------------------------------------------------------------
# ICP listing / loading
# ---------------------------------------------------------------------------

def test_icp_list_and_load(store):
    _write_icp(store)
    listed = icp_profiles.list_profiles()
    assert listed == [{
        "icp_id": "test-icp",
        "name": "Test ICP",
        "version": "icp-v1",
        "signal_count": 1,
    }]
    loaded = icp_profiles.load_profile("test-icp")
    assert loaded["signals"][0]["signal_id"] == "S-1"


def test_icp_load_missing_raises(store):
    with pytest.raises(ValueError):
        icp_profiles.load_profile("ghost")


def test_icp_load_malformed_raises(store):
    (store["icp"] / "broken.json").write_text("{ not valid json ")
    with pytest.raises(ValueError):
        icp_profiles.load_profile("broken")


def test_icp_load_missing_signals_raises(store):
    (store["icp"] / "empty.json").write_text(json.dumps(
        {"icp_id": "empty", "name": "E", "version": "v1", "signals": []}
    ))
    with pytest.raises(ValueError):
        icp_profiles.load_profile("empty")


def test_icp_list_skips_malformed(store):
    _write_icp(store)
    (store["icp"] / "broken.json").write_text("nonsense")
    ids = [p["icp_id"] for p in icp_profiles.list_profiles()]
    assert ids == ["test-icp"]


def test_icp_path_rejects_traversal(store):
    with pytest.raises(ValueError):
        icp_profiles.icp_profile_path("../../secret")


# ---------------------------------------------------------------------------
# Upload rejection for a missing project
# ---------------------------------------------------------------------------

def test_orchestrate_rejects_missing_project(store):
    upload = _FakeUpload(_OUTSCRAPER_CSV)
    with pytest.raises(ValueError, match="does not exist"):
        asyncio.run(runner.orchestrate_run(
            upload, "outscraper", "P-DOES-NOT-EXIST", "tester", _FakeBackgroundTasks()
        ))


# ---------------------------------------------------------------------------
# Run folder snapshots + pipeline command flags
# ---------------------------------------------------------------------------

def test_orchestrate_snapshots_and_command_flags(store, monkeypatch):
    _write_icp(store)
    projects.create_project("P-001", "Acme", "OBGYN", ["TX"], "test-icp", "tester")

    captured = {}

    class _FakeProc:
        returncode = 0

        def communicate(self):
            return (b"", b"")

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    upload = _FakeUpload(_OUTSCRAPER_CSV)
    bg = _FakeBackgroundTasks()
    run_id, row_count = asyncio.run(
        runner.orchestrate_run(upload, "outscraper", "P-001", "tester", bg)
    )

    assert row_count == 1
    run_dir = store["runs"] / run_id

    # Snapshots written into the run folder.
    snap_cfg = run_dir / config.PROJECT_CONFIG_SNAPSHOT_FILENAME
    snap_icp = run_dir / config.ICP_SNAPSHOT_FILENAME
    assert snap_cfg.exists()
    assert snap_icp.exists()
    assert json.loads(snap_cfg.read_text())["project_id"] == "P-001"
    assert json.loads(snap_icp.read_text())["icp_id"] == "test-icp"

    # Pipeline command carries --config and --icp pointing at the snapshots.
    cmd = captured["cmd"]
    assert "--config" in cmd and "--icp" in cmd
    assert str(snap_cfg) == cmd[cmd.index("--config") + 1]
    assert str(snap_icp) == cmd[cmd.index("--icp") + 1]

    # A monitor task was registered (run proceeds normally).
    assert bg.tasks


# ---------------------------------------------------------------------------
# Existing runs (pre-projects) still list and render
# ---------------------------------------------------------------------------

def test_snapshot_readers_none_for_legacy_run(store):
    legacy = store["runs"] / "RUN-20260101-090000-aaaa"
    legacy.mkdir(parents=True)
    # No snapshots present — readers must degrade gracefully, not raise.
    assert projects.read_config_snapshot(legacy) is None
    assert icp_profiles.read_snapshot(legacy) is None


def test_legacy_run_lists(store):
    legacy = store["runs"] / "RUN-20260101-090000-bbbb"
    legacy.mkdir(parents=True)
    (legacy / "status.json").write_text(json.dumps({
        "run_id": "RUN-20260101-090000-bbbb",
        "project_id": "old",
        "source_type": "outscraper",
        "status": "complete",
        "created_at": "2026-01-01T09:00:00Z",
        "operator": "tester",
        "input_filename": "old.csv",
    }))
    listed = [r.run_id for r in runs.list_runs()]
    assert "RUN-20260101-090000-bbbb" in listed
