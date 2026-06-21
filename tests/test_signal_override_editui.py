"""
test_signal_override_editui.py

Tests for Prompt 4b: the per-signal override EDIT UI in results.html. The UI is
template + inline JS only (no backend change). Tests use the FastAPI TestClient
to render the internal dashboard and assert on the markup/JS, plus an integration
POST proving the body shape the JS sends is accepted by the existing route.

Deterministic — no network, no subprocess.
"""

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_API_DIR = _REPO / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("PIPELINE_API_KEY", "test-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import runs  # noqa: E402

_RUN_ID = "RUN-20260621-120000-aaaa"


def _record():
    """Record with one crawl-confirmed yes and one not_found signal."""
    return {
        "id": "T-1", "record_id": "T-1", "practice_name": "Acme",
        "bullseye_score": 72, "fit_signal_score": 68, "confidence_score": 80,
        "target_tier": "Contender", "exclusion_status": "CLEAR",
        "enrichment_status": "complete",
        "signals": [
            {"signal_id": "S-ICP-001", "signal_label": "IUI offered",
             "signal_state": "yes", "evidence_text": "Lists IUI",
             "source_url": "https://orig.example.com/services",
             "confidence": "high", "positive_weight": 25},
            {"signal_id": "S-ICP-007", "signal_label": "Cash-pay visible",
             "signal_state": "not_found", "evidence_text": "",
             "source_url": "", "confidence": "low", "positive_weight": 16},
        ],
    }


def _write_run(run_directory, reviews_map=None):
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-06-21T12:00:00+00:00", "operator": "tester",
    }))
    (run_directory / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": [_record()]}, indent=2))
    if reviews_map is not None:
        (run_directory / "reviews.json").write_text(json.dumps(reviews_map, indent=2))


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path)
    return tmp_path / _RUN_ID


@pytest.fixture
def client(run_dir):
    _write_run(run_dir)
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        yield c


def _dashboard_html(c):
    r = c.get(f"/dashboard/{_RUN_ID}")
    assert r.status_code == 200
    return r.text


# ---------------------------------------------------------------------------
# 1 — every signal row renders an edit affordance
# ---------------------------------------------------------------------------

def test_edit_affordance_on_each_signal(client):
    html = _dashboard_html(client)
    # Two signals → two override-ui blocks and two edit affordances.
    assert html.count('class="signal-override-ui"') == 2
    assert "toggleSignalEdit('T-1','S-ICP-001')" in html
    assert "toggleSignalEdit('T-1','S-ICP-007')" in html


# ---------------------------------------------------------------------------
# 2 — inline form present with State defaulting to the signal's current state
# ---------------------------------------------------------------------------

def test_form_defaults_to_current_state(client):
    html = _dashboard_html(client)
    # not_found signal → not_found option carries `selected`.
    assert 'id="sigedit-state-T-1__S-ICP-007"' in html
    assert '<option value="not_found" selected>not_found</option>' in html
    # yes signal → yes option carries `selected`.
    assert '<option value="yes" selected>yes</option>' in html
    # Required source URL input and optional note input exist.
    assert 'id="sigedit-url-T-1__S-ICP-007"' in html
    assert 'id="sigedit-note-T-1__S-ICP-007"' in html


# ---------------------------------------------------------------------------
# 3 — valid submit POSTs to the right URL with the right body and NO override_by
# ---------------------------------------------------------------------------

def test_submit_builds_correct_post(client):
    html = _dashboard_html(client)
    # JS targets the existing signal-override route.
    assert "'/api/ui/reviews/' + runId + '/' + rid + '/signal-override'" in html
    # Body carries the four allowed fields.
    for key in ("signal_id:", "override_state:", "source_url:", "override_note:"):
        assert key in html
    # And NEVER an override_by key (server sets it from the session).
    assert "override_by:" not in html


def test_route_accepts_js_body_shape(client, run_dir):
    """Integration: the exact body the JS sends (no override_by) is accepted."""
    r = client.post(
        f"/api/ui/reviews/{_RUN_ID}/T-1/signal-override",
        json={"signal_id": "S-ICP-007", "override_state": "yes",
              "source_url": "https://acme.example.com/pay", "override_note": "n"},
    )
    assert r.status_code == 200
    assert r.json()["signal_override"]["override_by"] == "tester"


# ---------------------------------------------------------------------------
# 4 — empty source_url is guarded client-side (no POST)
# ---------------------------------------------------------------------------

def test_empty_source_url_guard_present(client):
    html = _dashboard_html(client)
    # The guard returns before the fetch when sourceUrl is empty.
    assert "if (!sourceUrl)" in html
    assert "Source URL is required." in html
    # The guard sits before the fetch call in the function body.
    guard_pos = html.index("if (!sourceUrl)")
    fetch_pos = html.index("'/api/ui/reviews/' + runId + '/' + rid + '/signal-override'")
    assert guard_pos < fetch_pos


# ---------------------------------------------------------------------------
# 5 — after a successful save the signal reflects the override (reload convention)
# ---------------------------------------------------------------------------

def test_success_reflects_override_after_reload(client, run_dir):
    # JS reloads on success; simulate by POSTing then re-rendering.
    r = client.post(
        f"/api/ui/reviews/{_RUN_ID}/T-1/signal-override",
        json={"signal_id": "S-ICP-007", "override_state": "yes",
              "source_url": "https://acme.example.com/pay", "override_note": "Self-pay"},
    )
    assert r.status_code == 200
    html = _dashboard_html(client)
    assert ">Override</span>" in html                       # badge now shows
    assert "https://acme.example.com/pay" in html           # operator source_url
    # JS uses a full reload on success.
    assert "window.location.reload()" in html


# ---------------------------------------------------------------------------
# 6 — a non-200 surfaces an inline error and leaves the form open
# ---------------------------------------------------------------------------

def test_error_path_keeps_form_open(client):
    html = _dashboard_html(client)
    # Error branch writes to the inline error element and shows it.
    assert "errEl.style.display = 'block'" in html
    # Reload happens only on success; the catch shows an error instead.
    success_then = html.index("window.location.reload()")
    catch_block = html.index(".catch(function(err)")
    assert success_then < catch_block  # reload is in the success .then, not the catch


# ---------------------------------------------------------------------------
# 7 — pre-existing 4a signal markup is unchanged; only affordance/form added
# ---------------------------------------------------------------------------

def test_preexisting_signal_markup_intact(run_dir):
    # No interaction / no overrides: the 4a render must be intact, with only the
    # new affordance/form nodes added on top.
    _write_run(run_dir, reviews_map=None)
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        html = _dashboard_html(c)
    # Pre-existing 4a markup unchanged: both group headers, labels, state badges.
    assert "FOUND" in html and "NOT FOUND" in html
    assert '<span class="signal-label">IUI offered</span>' in html
    assert '<span class="signal-label">Cash-pay visible</span>' in html
    assert 'class="signal-state-badge state-yes"' in html
    # No override present → no 4a Override badge anywhere.
    assert ">Override</span>" not in html
    # The only additions are the 4b affordance/form nodes (one per signal).
    assert html.count('class="signal-override-ui"') == 2


# ---------------------------------------------------------------------------
# 8 — the edit UI does not appear in any client-facing template
# ---------------------------------------------------------------------------

def test_edit_ui_absent_from_client_templates():
    client_templates = [
        _REPO / "handoff_renderer" / "templates" / "sales_handoff.html",
        _API_DIR / "reports" / "templates" / "bullseye_cards.html",
    ]
    checked = 0
    for path in client_templates:
        if not path.exists():
            continue
        checked += 1
        text = path.read_text(encoding="utf-8")
        assert "toggleSignalEdit" not in text
        assert "signal-override-ui" not in text
        assert "submitSignalOverride" not in text
    assert checked >= 1  # at least one client template was actually verified
