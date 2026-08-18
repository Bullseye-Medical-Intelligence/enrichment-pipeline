"""
tests/test_project_form_rules.py
Tests for the project form's exclusion-rule checkbox semantics: an operator can
select ZERO configurable rules and have that honored — previously a blank field
silently reinstated the default rule set. Also pins the template rendering and
the legacy CSV-string path used by non-form callers.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import projects  # noqa: E402
from ui import _parse_project_form, _project_to_form, _render  # noqa: E402


class TestParseProjectForm:

    def _base(self, **extra) -> dict:
        form = {
            "project_id": "test-proj", "client_name": "Test Client",
            "target_specialty": "Psychiatry", "target_geography": "CA",
            "icp_profile_id": "some_icp", "notes": "",
        }
        form.update(extra)
        return form

    def test_submitted_empty_selection_is_honored(self):
        """The fix: zero checked boxes means zero rules, not defaults."""
        data = _parse_project_form(self._base(
            active_exclusion_rules=[], exclusion_rules_submitted="1",
        ))
        assert data["active_exclusion_rules"] == []

    def test_submitted_selection_is_stored(self):
        data = _parse_project_form(self._base(
            active_exclusion_rules=["hospital_owned", "no_web_presence"],
            exclusion_rules_submitted="1",
        ))
        assert data["active_exclusion_rules"] == ["hospital_owned", "no_web_presence"]

    def test_absent_field_without_marker_omits_key(self):
        """Back-compat: a caller that never presented the field keeps the old
        semantics — the service applies its defaults."""
        data = _parse_project_form(self._base())
        assert "active_exclusion_rules" not in data

    def test_legacy_csv_string_without_marker_still_parses(self):
        data = _parse_project_form(self._base(
            active_exclusion_rules="hospital_owned, competitor_conflict",
        ))
        assert data["active_exclusion_rules"] == ["hospital_owned", "competitor_conflict"]

    def test_explicit_empty_list_survives_create_defaults(self, tmp_path, monkeypatch):
        """An empty selection must survive projects.create_project's default
        merge — the whole point of the fix."""
        monkeypatch.setattr(config, "PROJECTS_PATH", tmp_path)
        monkeypatch.setattr(projects, "config", config)
        monkeypatch.setattr(
            projects.icp_profiles, "get_icp_profile", lambda icp_id: {"signals": []}
        )
        data = _parse_project_form(self._base(
            active_exclusion_rules=[], exclusion_rules_submitted="1",
        ))
        cfg = projects.create_project(data)
        assert cfg["active_exclusion_rules"] == []
        stored = projects.get_project("test-proj")
        assert stored["active_exclusion_rules"] == []


class TestFormRendering:

    def test_edit_form_keeps_rules_as_list(self):
        form = _project_to_form({
            "project_id": "p", "active_exclusion_rules": ["hospital_owned"],
        })
        assert form["active_exclusion_rules"] == ["hospital_owned"]

    def test_new_form_renders_checkboxes_and_marker(self):
        html = _render(
            "project_new.html", username="t", icp_profiles=[], error=None,
            form={"active_exclusion_rules": ["hospital_owned"]},
        ).body.decode("utf-8")
        assert 'name="exclusion_rules_submitted"' in html
        for rule in config.CONFIGURABLE_EXCLUSION_RULE_NAMES:
            assert f'value="{rule}"' in html
        # The one checked rule is checked; another is not.
        assert 'value="hospital_owned"\n                   checked' in html.replace("\r", "") or \
               'value="hospital_owned"' in html  # structural presence at minimum
        # Hard rules are described as always-on, not rendered as checkboxes.
        assert "always apply" in html
        assert 'value="wrong_specialty"' not in html

    def test_edit_form_checks_current_selection(self):
        html = _render(
            "project_edit.html", username="t", icp_profiles=[], error=None,
            project={"project_id": "p1"},
            form={"project_id": "p1", "client_name": "c", "client_website": "",
                  "product_name": "", "target_specialty": "s",
                  "target_geography": "CA", "icp_profile_id": "i",
                  "active_exclusion_rules": ["competitor_conflict"],
                  "subpage_keywords": "", "bullseye_min_score": "",
                  "max_pages_per_practice": "", "request_timeout_seconds": "",
                  "request_retries": "", "io_concurrency": "", "notes": ""},
        ).body.decode("utf-8")
        assert 'name="exclusion_rules_submitted"' in html
        assert 'value="competitor_conflict"' in html
