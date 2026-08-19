"""
tests/test_project_exclusion_picker.py
Project form exclusion-rule picker: an operator can switch configurable rules
OFF (including all of them), while rules the engine always applies are shown
locked rather than offered as a control that does nothing.

Deterministic: filesystem only, no network, no LLM.
"""

import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO_ROOT))

import config  # noqa: E402
import projects  # noqa: E402
import ui  # noqa: E402

_ICP_ID = "test_icp"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated projects + ICP profile directories with one valid profile."""
    projects_root = tmp_path / "projects"
    icp_root = tmp_path / "icps"
    projects_root.mkdir()
    icp_root.mkdir()
    monkeypatch.setattr(config, "PROJECTS_PATH", projects_root)
    monkeypatch.setattr(config, "ICP_PROFILES_PATH", icp_root)
    import icp_profiles
    monkeypatch.setattr(icp_profiles.config, "ICP_PROFILES_PATH", icp_root)
    monkeypatch.setattr(projects.config, "PROJECTS_PATH", projects_root)
    (icp_root / f"{_ICP_ID}.json").write_text(json.dumps({
        "icp_id": _ICP_ID, "name": "Test ICP", "version": "1.0",
        "signals": [{"signal_id": "S-1", "signal_label": "x",
                     "prompt_instruction": "y", "positive_weight": 10}],
    }), encoding="utf-8")
    return projects_root


def _form(**over) -> dict:
    base = {
        "project_id": "p-test", "client_name": "Test Client",
        "target_specialty": "Psychiatry", "target_geography": "CA",
        "icp_profile_id": _ICP_ID, "client_website": "", "product_name": "",
        "active_exclusion_rules": "", "subpage_keywords": "",
        "bullseye_min_score": "", "max_pages_per_practice": "",
        "request_timeout_seconds": "", "request_retries": "",
        "io_concurrency": "", "notes": "",
    }
    base.update(over)
    return base


class TestSelectedRulesResolution:

    def test_no_picker_falls_back_to_defaults(self):
        """A JSON/API caller that sends no picker keeps the previous behavior."""
        assert ui._selected_exclusion_rules(_form()) is None

    def test_ticked_rules_are_kept(self):
        picked = ui._selected_exclusion_rules(_form(
            exclusion_rules_present="1",
            exclusion_rules=["hospital_owned", "no_web_presence"]))
        assert "hospital_owned" in picked
        assert "no_web_presence" in picked
        assert "health_system_affiliated" not in picked

    def test_all_unticked_means_zero_configurable_rules(self):
        """The regression this feature exists for: a blank selection used to
        silently fall back to the defaults, so rules could not be turned off."""
        picked = ui._selected_exclusion_rules(_form(
            exclusion_rules_present="1", exclusion_rules=[]))
        assert not (set(picked) & set(config.CONFIGURABLE_EXCLUSION_RULE_NAMES))

    def test_hard_rules_always_retained(self):
        picked = ui._selected_exclusion_rules(_form(
            exclusion_rules_present="1", exclusion_rules=[]))
        assert set(config.HARD_EXCLUSION_RULE_NAMES) <= set(picked)

    def test_unknown_rule_names_ignored(self):
        picked = ui._selected_exclusion_rules(_form(
            exclusion_rules_present="1",
            exclusion_rules=["hospital_owned", "not_a_rule"]))
        assert "not_a_rule" not in picked

    def test_legacy_text_field_still_honored(self):
        data = ui._parse_project_form(
            _form(active_exclusion_rules="hospital_owned, no_web_presence"),
            created_by="tester")
        assert data["active_exclusion_rules"] == ["hospital_owned", "no_web_presence"]

    def test_picker_wins_over_legacy_text(self):
        data = ui._parse_project_form(
            _form(active_exclusion_rules="hospital_owned",
                  exclusion_rules_present="1",
                  exclusion_rules=["no_web_presence"]),
            created_by="tester")
        assert "no_web_presence" in data["active_exclusion_rules"]
        assert "hospital_owned" not in data["active_exclusion_rules"]


class TestPickerOptions:

    def test_defaults_ticked_when_no_selection(self):
        opts = ui._exclusion_rule_options(None)
        checked = {r["name"] for r in opts["configurable"] if r["checked"]}
        assert checked == (set(config.DEFAULT_EXCLUSION_RULES)
                           & set(config.CONFIGURABLE_EXCLUSION_RULE_NAMES))

    def test_stored_selection_reflected(self):
        opts = ui._exclusion_rule_options(["hospital_owned"])
        by_name = {r["name"]: r for r in opts["configurable"]}
        assert by_name["hospital_owned"]["checked"] is True
        assert by_name["no_web_presence"]["checked"] is False

    def test_empty_selection_ticks_nothing(self):
        opts = ui._exclusion_rule_options([])
        assert not any(r["checked"] for r in opts["configurable"])

    def test_hard_rules_listed_separately_and_locked(self):
        opts = ui._exclusion_rule_options(None)
        assert {r["name"] for r in opts["hard"]} == set(config.HARD_EXCLUSION_RULE_NAMES)
        assert all(r["checked"] for r in opts["hard"])
        # A hard rule must never appear as a switchable checkbox.
        assert not (set(config.HARD_EXCLUSION_RULE_NAMES)
                    & {r["name"] for r in opts["configurable"]})

    def test_every_rule_has_operator_facing_copy(self):
        opts = ui._exclusion_rule_options(None)
        for rule in opts["configurable"] + opts["hard"]:
            assert rule["label"] and rule["label"] != rule["name"]
            assert rule["description"]


class TestPersistence:

    def test_project_saved_with_no_configurable_rules(self, env):
        data = ui._parse_project_form(
            _form(exclusion_rules_present="1", exclusion_rules=[]),
            created_by="tester")
        projects.create_project(data)
        stored = projects.get_project("p-test")
        assert not (set(stored["active_exclusion_rules"])
                    & set(config.CONFIGURABLE_EXCLUSION_RULE_NAMES))

    def test_edit_can_turn_a_rule_off(self, env):
        projects.create_project(ui._parse_project_form(
            _form(exclusion_rules_present="1",
                  exclusion_rules=["hospital_owned", "no_web_presence"]),
            created_by="tester"))
        projects.update_project("p-test", ui._parse_project_form(
            _form(exclusion_rules_present="1", exclusion_rules=["hospital_owned"]),
            created_by=None))
        stored = projects.get_project("p-test")
        assert "hospital_owned" in stored["active_exclusion_rules"]
        assert "no_web_presence" not in stored["active_exclusion_rules"]


class TestFormRendering:

    @pytest.fixture
    def client(self, env):
        from fastapi.testclient import TestClient
        import main
        with TestClient(main.app, follow_redirects=False) as c:
            r = c.post("/login", data={"username": "tester", "password": "secret-pw"})
            assert r.status_code in (200, 302, 303)
            yield c

    def test_new_project_form_renders_checkboxes(self, client):
        html = client.get("/projects/new").text
        assert 'name="exclusion_rules_present"' in html
        for rule in config.CONFIGURABLE_EXCLUSION_RULE_NAMES:
            assert f'value="{rule}"' in html
        assert "cannot be switched off" in html

    def test_no_free_text_rules_input_remains(self, client):
        html = client.get("/projects/new").text
        assert 'name="active_exclusion_rules"' not in html

    def test_posting_with_no_rules_ticked_creates_project(self, client, env):
        r = client.post("/projects", data={
            **{k: v for k, v in _form().items() if k != "active_exclusion_rules"},
            "exclusion_rules_present": "1",
        })
        assert r.status_code == 303
        stored = projects.get_project("p-test")
        assert not (set(stored["active_exclusion_rules"])
                    & set(config.CONFIGURABLE_EXCLUSION_RULE_NAMES))
