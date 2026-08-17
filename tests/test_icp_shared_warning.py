"""
tests/test_icp_shared_warning.py
Shared-ICP edit warning (docs/data-boundary-model.md §H decision 2026-08-17:
warn, don't block). Editing/importing a profile referenced by more than one
live project surfaces a warning naming those projects; a single-project or
unreferenced profile saves silently.

Deterministic: filesystem only, no network, no LLM.
"""

import json
import os
import sys
from pathlib import Path

import pytest

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import projects  # noqa: E402
import ui  # noqa: E402


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    """Point PROJECTS_PATH at a tmp dir; return it."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(config, "PROJECTS_PATH", root)
    return root


def _seed_project(root: Path, project_id: str, icp_id: str) -> None:
    d = root / project_id
    d.mkdir()
    (d / "project_config.json").write_text(json.dumps({
        "project_id": project_id, "client_name": project_id.title(),
        "target_specialty": "OBGYN", "target_geography": ["GA"],
        "icp_profile_id": icp_id, "active_exclusion_rules": [],
        "bullseye_min_score": 90,
    }), encoding="utf-8")


class TestProjectsReferencingIcp:

    def test_lists_only_matching_projects(self, projects_root):
        _seed_project(projects_root, "femasys", "obgyn_femasys")
        _seed_project(projects_root, "acme", "obgyn_femasys")
        _seed_project(projects_root, "other", "different_icp")
        assert projects.projects_referencing_icp("obgyn_femasys") == ["acme", "femasys"]

    def test_empty_when_unreferenced(self, projects_root):
        _seed_project(projects_root, "femasys", "obgyn_femasys")
        assert projects.projects_referencing_icp("unused_icp") == []


class TestSharedIcpWarning:

    def test_two_projects_produce_warning_naming_both(self, projects_root):
        _seed_project(projects_root, "femasys", "obgyn_femasys")
        _seed_project(projects_root, "acme", "obgyn_femasys")
        warning = ui._shared_icp_warning("obgyn_femasys")
        assert "2 projects" in warning
        assert "femasys" in warning and "acme" in warning
        assert "future runs" in warning
        assert "frozen snapshot" in warning

    def test_single_project_is_silent(self, projects_root):
        _seed_project(projects_root, "femasys", "obgyn_femasys")
        assert ui._shared_icp_warning("obgyn_femasys") == ""

    def test_unreferenced_profile_is_silent(self, projects_root):
        assert ui._shared_icp_warning("unused_icp") == ""


def test_profiles_page_renders_shared_warning(projects_root, tmp_path, monkeypatch):
    """The redirect target surfaces the warning as a visible banner."""
    import icp_profiles
    monkeypatch.setattr(config, "ICP_PROFILES_PATH", tmp_path / "icps")
    (tmp_path / "icps").mkdir()
    monkeypatch.setattr(icp_profiles, "sync_all_seed_profiles", lambda: 0, raising=False)

    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        r = c.get("/icp-profiles", params={
            "shared_warning": "'obgyn_femasys' is shared by 2 projects (acme, femasys)."
        })
    assert r.status_code == 200
    assert "shared by 2 projects" in r.text
    assert "alert-warning" in r.text
