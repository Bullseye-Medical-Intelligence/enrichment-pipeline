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
    "description": "A test ICP.",
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
    b"name,address,phone,site,type\n"
    b"Acme Clinic,123 Main St,555-1000,https://acme.example,clinic\n"
)


def _project_data(project_id="p-001", client_name="Acme", specialty="OBGYN",
                  geography=None, icp="test-icp"):
    return {
        "project_id": project_id,
        "client_name": client_name,
        "target_specialty": specialty,
        "target_geography": geography if geography is not None else ["TX"],
        "icp_profile_id": icp,
        "created_by": "tester",
    }


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
    cfg = projects.create_project(_project_data(geography=["TX", "FL"]))
    assert cfg["project_id"] == "p-001"
    assert cfg["icp_profile_id"] == "test-icp"
    again = projects.get_project("p-001")
    assert again["client_name"] == "Acme"
    assert again["target_geography"] == ["TX", "FL"]
    # Generic defaults applied.
    assert again["bullseye_min_score"] == config.DEFAULT_BULLSEYE_MIN_SCORE
    assert again["io_concurrency"] == config.DEFAULT_IO_CONCURRENCY


def test_create_project_rejects_duplicate(store):
    _write_icp(store)
    projects.create_project(_project_data())
    with pytest.raises(ValueError):
        projects.create_project(_project_data())


def test_create_project_rejects_missing_icp(store):
    with pytest.raises(ValueError):
        projects.create_project(_project_data(project_id="p-002", icp="no-such-icp"))


@pytest.mark.parametrize("bad_id", ["../escape", "has space", "", "a/b", "P-001", "x" * 65])
def test_create_project_rejects_bad_id(store, bad_id):
    _write_icp(store)
    with pytest.raises(ValueError):
        projects.create_project(_project_data(project_id=bad_id))


def test_validate_project_config_missing_fields():
    with pytest.raises(ValueError):
        projects.validate_project_config({"project_id": "p-1"})  # missing the rest


def test_validate_project_config_bad_score(store):
    _write_icp(store)
    data = projects.default_project_config()
    data.update(_project_data())
    data["bullseye_min_score"] = 150  # out of 0-100 range
    with pytest.raises(ValueError):
        projects.validate_project_config(data)


def test_list_projects(store):
    _write_icp(store)
    projects.create_project(_project_data(project_id="p-001"))
    projects.create_project(_project_data(project_id="p-002", client_name="Beta"))
    ids = [p["project_id"] for p in projects.list_projects()]
    assert ids == ["p-001", "p-002"]


def test_update_project(store):
    _write_icp(store)
    projects.create_project(_project_data())
    updated = projects.update_project("p-001", {"client_name": "Acme Renamed"})
    assert updated["client_name"] == "Acme Renamed"
    assert projects.get_project("p-001")["client_name"] == "Acme Renamed"


def test_project_dir_rejects_traversal(store):
    with pytest.raises(ValueError):
        projects.project_dir("../../etc")


# ---------------------------------------------------------------------------
# ICP listing / loading
# ---------------------------------------------------------------------------

def test_icp_list_and_load(store):
    _write_icp(store)
    listed = icp_profiles.list_icp_profiles()
    assert len(listed) == 1
    entry = listed[0]
    assert entry["icp_id"] == "test-icp"
    assert entry["name"] == "Test ICP"
    assert entry["version"] == "icp-v1"
    assert entry["signal_count"] == 1
    loaded = icp_profiles.get_icp_profile("test-icp")
    assert loaded["signals"][0]["signal_id"] == "S-1"


def test_icp_load_missing_raises(store):
    with pytest.raises(ValueError):
        icp_profiles.get_icp_profile("ghost")


def test_icp_load_malformed_raises(store):
    (store["icp"] / "broken.json").write_text("{ not valid json ")
    with pytest.raises(ValueError):
        icp_profiles.get_icp_profile("broken")


def test_icp_load_missing_signals_raises(store):
    (store["icp"] / "empty.json").write_text(json.dumps(
        {"icp_id": "empty", "name": "E", "version": "v1", "signals": []}
    ))
    with pytest.raises(ValueError):
        icp_profiles.get_icp_profile("empty")


def test_icp_load_bad_signal_shape_raises(store):
    (store["icp"] / "bad.json").write_text(json.dumps({
        "icp_id": "bad", "name": "Bad", "version": "v1",
        "signals": [{"signal_id": "S-1", "signal_label": "x", "prompt_instruction": "y",
                     "positive_weight": "not-a-number"}],
    }))
    with pytest.raises(ValueError):
        icp_profiles.get_icp_profile("bad")


def test_icp_list_skips_malformed(store):
    _write_icp(store)
    (store["icp"] / "broken.json").write_text("nonsense")
    ids = [p["icp_id"] for p in icp_profiles.list_icp_profiles()]
    assert ids == ["test-icp"]


def test_icp_path_rejects_traversal(store):
    with pytest.raises(ValueError):
        icp_profiles.icp_profile_path("../../secret")


def test_save_icp_profile_overwrites_readonly_destination(store):
    """Importing over a read-only seeded profile must succeed, not raise.

    Regression: on Windows, os.replace cannot overwrite a read-only file (a
    seeded profile inherits the repo file's read-only bit via shutil.copy2),
    which surfaced as a 500 on the import route. _replace_atomic clears the bit.
    """
    import stat as _stat

    icp_profiles.save_icp_profile(dict(_VALID_ICP), overwrite=True)
    dest = store["icp"] / "test-icp.json"
    dest.chmod(_stat.S_IREAD)  # simulate the seeded read-only destination

    updated = dict(_VALID_ICP, name="Renamed ICP")
    icp_profiles.save_icp_profile(updated, overwrite=True)

    assert icp_profiles.get_icp_profile("test-icp")["name"] == "Renamed ICP"


def test_replace_atomic_recovers_from_transient_permission_error(tmp_path, monkeypatch):
    """_replace_atomic retries a PermissionError (Windows lock) and then succeeds."""
    src = tmp_path / "src.tmp"
    dest = tmp_path / "dest.json"
    src.write_text("new")
    dest.write_text("old")

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("WinError 5")
        return real_replace(a, b)

    monkeypatch.setattr(icp_profiles.os, "replace", flaky_replace)
    monkeypatch.setattr(icp_profiles.time, "sleep", lambda *_: None)

    icp_profiles._replace_atomic(src, dest)

    assert calls["n"] == 2
    assert dest.read_text() == "new"


def _icp_with_signal(**signal_extra):
    base = {
        "signal_id": "S-1", "signal_label": "x",
        "prompt_instruction": "y", "positive_weight": 10,
    }
    base.update(signal_extra)
    return {"icp_id": "t", "name": "T", "version": "v1", "signals": [base]}


def test_icp_validation_accepts_optional_tiering_fields():
    icp_profiles.validate_icp_profile(_icp_with_signal(
        not_found_weight=-15, verification_required=True, cap_tier="Contender",
        no_weight=-15, required_for_bullseye=True, exclude_if_yes=True,
    ))


def test_icp_validation_rejects_non_bool_exclude_if_yes():
    with pytest.raises(ValueError):
        icp_profiles.validate_icp_profile(_icp_with_signal(exclude_if_yes="yes"))


def test_icp_validation_rejects_non_numeric_not_found_weight():
    with pytest.raises(ValueError):
        icp_profiles.validate_icp_profile(_icp_with_signal(not_found_weight="lots"))


def test_icp_validation_rejects_non_numeric_no_weight():
    with pytest.raises(ValueError):
        icp_profiles.validate_icp_profile(_icp_with_signal(no_weight="lots"))


def test_icp_validation_rejects_non_bool_verification_required():
    with pytest.raises(ValueError):
        icp_profiles.validate_icp_profile(_icp_with_signal(verification_required="yes"))


def test_icp_validation_rejects_non_bool_required_for_bullseye():
    with pytest.raises(ValueError):
        icp_profiles.validate_icp_profile(_icp_with_signal(required_for_bullseye="yes"))


def test_icp_validation_rejects_bad_cap_tier():
    with pytest.raises(ValueError):
        icp_profiles.validate_icp_profile(_icp_with_signal(cap_tier="Excluded"))


def test_icp_validation_accepts_reinforces_referencing_known_signal():
    profile = {
        "icp_id": "t", "name": "T", "version": "v1",
        "signals": [
            {"signal_id": "S-cash", "signal_label": "Cash pay",
             "prompt_instruction": "?", "positive_weight": 30,
             "verification_required": True},
            {"signal_id": "S-elective", "signal_label": "Elective",
             "prompt_instruction": "?", "positive_weight": 20,
             "reinforces": "S-cash"},
        ],
    }
    icp_profiles.validate_icp_profile(profile)


def test_icp_validation_rejects_reinforces_unknown_signal():
    with pytest.raises(ValueError):
        icp_profiles.validate_icp_profile(_icp_with_signal(reinforces="S-does-not-exist"))


def test_icp_validation_rejects_non_string_reinforces():
    with pytest.raises(ValueError):
        icp_profiles.validate_icp_profile(_icp_with_signal(reinforces=123))


def test_save_icp_profile_rejects_duplicate_without_overwrite(store):
    icp_profiles.save_icp_profile(dict(_VALID_ICP))
    with pytest.raises(ValueError):
        icp_profiles.save_icp_profile(dict(_VALID_ICP))  # id exists, no overwrite


def test_save_icp_profile_overwrites_when_editing(store):
    icp_profiles.save_icp_profile(dict(_VALID_ICP))
    edited = dict(_VALID_ICP)
    edited["signals"] = [
        {"signal_id": "S-1", "signal_label": "Kept", "prompt_instruction": "?",
         "positive_weight": 10, "cap_tier": "Contender"},
    ]
    icp_profiles.save_icp_profile(edited, overwrite=True)
    reloaded = icp_profiles.get_icp_profile(_VALID_ICP["icp_id"])
    assert len(reloaded["signals"]) == 1
    assert reloaded["signals"][0]["signal_label"] == "Kept"
    assert reloaded["signals"][0]["cap_tier"] == "Contender"


# ---------------------------------------------------------------------------
# Upload rejection for a missing project
# ---------------------------------------------------------------------------

def test_orchestrate_rejects_missing_project(store):
    upload = _FakeUpload(_OUTSCRAPER_CSV)
    with pytest.raises(ValueError, match="does not exist"):
        asyncio.run(runner.orchestrate_run(
            upload, "outscraper", "does-not-exist", "tester", _FakeBackgroundTasks()
        ))


def test_orchestrate_rejects_missing_icp(store):
    # Project exists but its ICP file was removed.
    _write_icp(store)
    projects.create_project(_project_data())
    (store["icp"] / "test-icp.json").unlink()
    upload = _FakeUpload(_OUTSCRAPER_CSV)
    with pytest.raises(ValueError):
        asyncio.run(runner.orchestrate_run(
            upload, "outscraper", "p-001", "tester", _FakeBackgroundTasks()
        ))


# ---------------------------------------------------------------------------
# Run folder snapshots + pipeline command flags
# ---------------------------------------------------------------------------

def test_orchestrate_snapshots_and_command_flags(store, monkeypatch):
    _write_icp(store)
    projects.create_project(_project_data())

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
        runner.orchestrate_run(upload, "outscraper", "p-001", "tester", bg)
    )

    assert row_count == 1
    run_dir = store["runs"] / run_id

    # Snapshots written into the run folder.
    snap_cfg = run_dir / config.PROJECT_CONFIG_SNAPSHOT_FILENAME
    snap_icp = run_dir / config.ICP_SNAPSHOT_FILENAME
    assert snap_cfg.exists()
    assert snap_icp.exists()
    assert json.loads(snap_cfg.read_text())["project_id"] == "p-001"
    assert json.loads(snap_icp.read_text())["icp_id"] == "test-icp"

    # status.json carries project/ICP metadata.
    status = runs.get_run(run_id)
    assert status.client_name == "Acme"
    assert status.icp_profile_id == "test-icp"
    assert status.icp_profile_name == "Test ICP"

    # Pipeline command carries --config and --icp pointing at the snapshots.
    cmd = captured["cmd"]
    assert "--config" in cmd and "--icp" in cmd
    assert str(snap_cfg) == cmd[cmd.index("--config") + 1]
    assert str(snap_icp) == cmd[cmd.index("--icp") + 1]
    assert bg.tasks  # monitor registered


# ---------------------------------------------------------------------------
# Existing runs (pre-projects) still list and render
# ---------------------------------------------------------------------------

def test_snapshot_readers_none_for_legacy_run(store):
    legacy = store["runs"] / "RUN-20260101-090000-aaaa"
    legacy.mkdir(parents=True)
    assert projects.read_config_snapshot(legacy) is None
    assert icp_profiles.read_snapshot(legacy) is None


def test_legacy_run_lists_and_parses(store):
    legacy = store["runs"] / "RUN-20260101-090000-bbbb"
    legacy.mkdir(parents=True)
    # Status.json written before project metadata existed (no client_name, etc.).
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
    # Old status.json still parses with the extended schema.
    status = runs.get_run("RUN-20260101-090000-bbbb")
    assert status.client_name is None
    assert status.target_geography == []
