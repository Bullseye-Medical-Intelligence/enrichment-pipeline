"""
tests/test_preflight.py
Tests for preflight.run_checks and individual check helpers.
No I/O beyond the tmp_path fixture; no network calls.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline-api"))

from preflight import (
    _check_anthropic_key,
    _check_icp_profiles,
    _check_openai_key,
    _check_output_dir,
    _check_pipeline_repo,
    _check_projects,
    _check_session_key,
    run_checks,
)


class TestAnthropicKey:

    def test_key_set_is_ok(self):
        r = _check_anthropic_key("sk-ant-test")
        assert r.status == "ok"

    def test_empty_key_is_error(self):
        r = _check_anthropic_key("")
        assert r.status == "error"

    def test_missing_key_message_mentions_step_4(self):
        r = _check_anthropic_key("")
        assert "Step 4" in r.message or "signal extraction" in r.message.lower()


class TestPipelineRepo:

    def test_missing_path_config_is_error(self, tmp_path):
        from pathlib import Path
        r = _check_pipeline_repo(Path(""), "pipeline.py")
        assert r.status == "error"

    def test_nonexistent_dir_is_error(self, tmp_path):
        r = _check_pipeline_repo(tmp_path / "no-such-dir", "pipeline.py")
        assert r.status == "error"

    def test_dir_exists_but_no_script_is_error(self, tmp_path):
        r = _check_pipeline_repo(tmp_path, "pipeline.py")
        assert r.status == "error"
        assert "pipeline.py" in r.message

    def test_dir_and_script_present_is_ok(self, tmp_path):
        (tmp_path / "pipeline.py").write_text("", encoding="utf-8")
        r = _check_pipeline_repo(tmp_path, "pipeline.py")
        assert r.status == "ok"


class TestOutputDir:

    def test_writable_existing_dir_is_ok(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        r = _check_output_dir(runs_dir)
        assert r.status == "ok"

    def test_probe_file_cleaned_up(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        _check_output_dir(runs_dir)
        assert not (runs_dir / ".preflight_probe").exists()

    def test_missing_path_config_is_error(self, tmp_path):
        from pathlib import Path
        r = _check_output_dir(Path(""))
        assert r.status == "error"

    def test_nonexistent_dir_created_and_ok(self, tmp_path):
        runs_dir = tmp_path / "runs" / "subruns"
        r = _check_output_dir(runs_dir)
        assert r.status == "ok"
        assert runs_dir.exists()


class TestIcpProfiles:

    def test_no_directory_is_warn(self, tmp_path):
        r = _check_icp_profiles(tmp_path / "no-profiles")
        assert r.status == "warn"

    def test_empty_directory_is_warn(self, tmp_path):
        d = tmp_path / "profiles"
        d.mkdir()
        r = _check_icp_profiles(d)
        assert r.status == "warn"

    def test_one_profile_is_ok(self, tmp_path):
        d = tmp_path / "profiles"
        d.mkdir()
        (d / "obgyn.json").write_text("{}", encoding="utf-8")
        r = _check_icp_profiles(d)
        assert r.status == "ok"
        assert "1" in r.message

    def test_multiple_profiles_counted(self, tmp_path):
        d = tmp_path / "profiles"
        d.mkdir()
        for name in ("a.json", "b.json", "c.json"):
            (d / name).write_text("{}", encoding="utf-8")
        r = _check_icp_profiles(d)
        assert r.status == "ok"
        assert "3" in r.message


class TestProjects:

    def test_no_directory_is_warn(self, tmp_path):
        r = _check_projects(tmp_path / "no-projects")
        assert r.status == "warn"

    def test_directory_with_no_projects_is_warn(self, tmp_path):
        d = tmp_path / "projects"
        d.mkdir()
        r = _check_projects(d)
        assert r.status == "warn"

    def test_one_project_is_ok(self, tmp_path):
        d = tmp_path / "projects"
        proj = d / "P-001"
        proj.mkdir(parents=True)
        (proj / "project_config.json").write_text("{}", encoding="utf-8")
        r = _check_projects(d)
        assert r.status == "ok"
        assert "1" in r.message

    def test_subdirs_without_config_not_counted(self, tmp_path):
        d = tmp_path / "projects"
        (d / "P-001").mkdir(parents=True)  # no project_config.json
        r = _check_projects(d)
        assert r.status == "warn"


class TestSessionKey:

    def test_key_set_is_ok(self):
        r = _check_session_key("supersecret")
        assert r.status == "ok"

    def test_missing_key_is_error(self):
        r = _check_session_key("")
        assert r.status == "error"

    def test_placeholder_key_is_error(self):
        r = _check_session_key("your-session-secret-key-here")
        assert r.status == "error"


class TestOpenAIKey:

    def test_key_set_is_ok(self):
        r = _check_openai_key("sk-proj-realkey123")
        assert r.status == "ok"

    def test_missing_key_is_warn(self):
        r = _check_openai_key("")
        assert r.status == "warn"
        assert "verification" in r.message.lower()

    def test_placeholder_key_is_warn(self):
        r = _check_openai_key("sk-...")
        assert r.status == "warn"


class TestPlaceholderDetection:

    def test_anthropic_placeholder_is_error(self):
        r = _check_anthropic_key("sk-ant-...")
        assert r.status == "error"
        assert "placeholder" in r.message.lower()


class TestRunChecks:

    def _full_ok_setup(self, tmp_path):
        """Build a tmp filesystem that passes all checks."""
        repo = tmp_path / "pipeline"
        repo.mkdir()
        (repo / "pipeline.py").write_text("", encoding="utf-8")

        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()

        profiles_dir = tmp_path / "icp_profiles"
        profiles_dir.mkdir()
        (profiles_dir / "test.json").write_text("{}", encoding="utf-8")

        projects_dir = tmp_path / "projects"
        proj = projects_dir / "P-001"
        proj.mkdir(parents=True)
        (proj / "project_config.json").write_text("{}", encoding="utf-8")

        return repo, runs_dir, profiles_dir, projects_dir

    def test_all_ok_returns_ok(self, tmp_path):
        repo, runs_dir, profiles_dir, projects_dir = self._full_ok_setup(tmp_path)
        result = run_checks(
            anthropic_api_key="sk-ant-test",
            pipeline_repo_path=repo,
            pipeline_script="pipeline.py",
            output_runs_path=runs_dir,
            icp_profiles_path=profiles_dir,
            projects_path=projects_dir,
            session_secret_key="secret",
            openai_api_key="sk-proj-realkey123",
        )
        assert result["status"] == "ok"
        assert all(c["status"] == "ok" for c in result["checks"])

    def test_one_error_escalates_overall(self, tmp_path):
        repo, runs_dir, profiles_dir, projects_dir = self._full_ok_setup(tmp_path)
        result = run_checks(
            anthropic_api_key="",        # error
            pipeline_repo_path=repo,
            pipeline_script="pipeline.py",
            output_runs_path=runs_dir,
            icp_profiles_path=profiles_dir,
            projects_path=projects_dir,
            session_secret_key="secret",
        )
        assert result["status"] == "error"

    def test_warn_only_stays_warn(self, tmp_path):
        repo, runs_dir, profiles_dir, projects_dir = self._full_ok_setup(tmp_path)
        result = run_checks(
            anthropic_api_key="sk-ant-test",
            pipeline_repo_path=repo,
            pipeline_script="pipeline.py",
            output_runs_path=runs_dir,
            icp_profiles_path=profiles_dir,
            projects_path=projects_dir,
            session_secret_key="secret",
            # openai_api_key omitted -> warn only
        )
        assert result["status"] == "warn"

    def test_missing_session_key_escalates_to_error(self, tmp_path):
        repo, runs_dir, profiles_dir, projects_dir = self._full_ok_setup(tmp_path)
        result = run_checks(
            anthropic_api_key="sk-ant-test",
            pipeline_repo_path=repo,
            pipeline_script="pipeline.py",
            output_runs_path=runs_dir,
            icp_profiles_path=profiles_dir,
            projects_path=projects_dir,
            session_secret_key="",
            openai_api_key="sk-proj-realkey123",
        )
        assert result["status"] == "error"

    def test_result_has_checks_list(self, tmp_path):
        repo, runs_dir, profiles_dir, projects_dir = self._full_ok_setup(tmp_path)
        result = run_checks(
            anthropic_api_key="sk",
            pipeline_repo_path=repo,
            pipeline_script="pipeline.py",
            output_runs_path=runs_dir,
            icp_profiles_path=profiles_dir,
            projects_path=projects_dir,
            session_secret_key="s",
        )
        assert isinstance(result["checks"], list)
        assert len(result["checks"]) == 7
        for c in result["checks"]:
            assert {"check", "label", "status", "message"} <= c.keys()
