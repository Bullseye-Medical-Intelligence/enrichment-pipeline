"""
test_refresh_status.py

Tests for the per-record in-place refresh status (Task 2 fix) and the
Expand/Collapse All toggle (Task 1):
- runner mark/load round-trip: running -> done (stamps last_refreshed_at) and
  running -> failed (carries the error); stale "running" reported as failed.
- _monitor_batch_reenrich surfaces a nonzero pipeline exit as a per-record
  failed state instead of a silent server-side log line.
- GET /runs/{run_id}/refresh-status returns the map (session-auth).
- The dashboard renders spinner / refreshed / failed indicators from the map.
- The Expand All toggle button and its filter-respecting JS exist.

Deterministic — no network, no real subprocess.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_API_DIR = _REPO / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO))

from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
import icp_profiles  # noqa: E402
import main  # noqa: E402
import runner  # noqa: E402
import runs  # noqa: E402

_RUN_ID = "RUN-20260702-100000-ffff"


def _record(rid, **over):
    rec = {
        "id": rid, "record_id": rid, "practice_name": "Practice " + rid,
        "bullseye_score": 72, "target_tier": "Contender", "exclusion_status": "CLEAR",
        "enrichment_status": "complete", "confidence_band": "Moderate",
        "address_city": "Atlanta", "address_state": "GA", "source_confidence": "complete",
        "signals": [], "sales_angle": [], "call_brief": {},
    }
    rec.update(over)
    return rec


def _write_run(run_directory, records):
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-07-02T09:00:00+00:00",
        "completed_at": "2026-07-02T09:30:00+00:00", "operator": "tester",
    }))
    (run_directory / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": records}, indent=2))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path / "runs")
    icp_dir = tmp_path / "icp"
    icp_dir.mkdir()
    monkeypatch.setattr(config, "ICP_PROFILES_PATH", icp_dir)
    monkeypatch.setattr(icp_profiles, "sync_seed_profile", lambda *a, **k: False)
    run_dir = tmp_path / "runs" / _RUN_ID
    return run_dir


def _get(path):
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        return c.get(path)


# ---------------------------------------------------------------------------
# Runner: mark / load round-trip
# ---------------------------------------------------------------------------

def test_running_then_done_stamps_last_refreshed_at(tmp_path):
    runner.mark_refresh_running(tmp_path, ["T-1"], "browser re-crawl")
    state = runner.load_refresh_status(tmp_path)["T-1"]
    assert state["state"] == "running"
    assert state["kind"] == "browser re-crawl"

    runner.mark_refresh_done(tmp_path, ["T-1"])
    state = runner.load_refresh_status(tmp_path)["T-1"]
    assert state["state"] == "done"
    assert state["last_refreshed_at"]
    assert state["error"] == ""


def test_running_then_failed_carries_error(tmp_path):
    runner.mark_refresh_running(tmp_path, ["T-1"], "re-enrich")
    runner.mark_refresh_failed(tmp_path, ["T-1"], "Pipeline exited with an error: boom")
    state = runner.load_refresh_status(tmp_path)["T-1"]
    assert state["state"] == "failed"
    assert "boom" in state["error"]
    # A later successful refresh clears the failure.
    runner.mark_refresh_running(tmp_path, ["T-1"], "re-enrich")
    runner.mark_refresh_done(tmp_path, ["T-1"])
    assert runner.load_refresh_status(tmp_path)["T-1"]["state"] == "done"


def test_stale_running_reported_failed(tmp_path):
    runner.mark_refresh_running(tmp_path, ["T-1"], "re-enrich")
    path = tmp_path / runner.REFRESH_STATUS_FILENAME
    data = json.loads(path.read_text())
    data["T-1"]["started_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=config.REFRESH_STALE_MINUTES + 5)
    ).isoformat()
    path.write_text(json.dumps(data))
    state = runner.load_refresh_status(tmp_path)["T-1"]
    assert state["state"] == "failed"
    assert "did not report completion" in state["error"]
    # Read-only reporting: the file itself is not rewritten by a GET-path load.
    assert json.loads(path.read_text())["T-1"]["state"] == "running"


# ---------------------------------------------------------------------------
# Batch monitor: nonzero exit surfaces per-record failure
# ---------------------------------------------------------------------------

class _FakeProcess:
    returncode = 1

    def communicate(self):
        return b"", b"playwright: browser executable not found"


def test_monitor_batch_reenrich_marks_failed_on_pipeline_error(env, monkeypatch):
    _write_run(env, [_record("T-1")])
    scratch = env / ".batch_test"
    scratch.mkdir(parents=True)
    asyncio.run(runner._monitor_batch_reenrich(_RUN_ID, scratch, ["T-1"], _FakeProcess()))
    state = runner.load_refresh_status(env)["T-1"]
    assert state["state"] == "failed"
    assert "browser executable not found" in state["error"]
    assert not scratch.exists()  # scratch always cleaned up


# ---------------------------------------------------------------------------
# Route + dashboard rendering
# ---------------------------------------------------------------------------

def test_refresh_status_route_returns_map(env):
    _write_run(env, [_record("T-1")])
    runner.mark_refresh_running(env, ["T-1"], "browser re-crawl")
    r = _get(f"/runs/{_RUN_ID}/refresh-status")
    assert r.status_code == 200
    assert r.json()["T-1"]["state"] == "running"


def test_dashboard_renders_running_spinner(env):
    _write_run(env, [_record("T-1")])
    runner.mark_refresh_running(env, ["T-1"], "browser re-crawl")
    html = _get(f"/dashboard/{_RUN_ID}").text
    assert 'data-refreshing="1"' in html
    assert 'class="spinner"' in html


def test_dashboard_renders_failed_badge_with_error(env):
    _write_run(env, [_record("T-1")])
    runner.mark_refresh_running(env, ["T-1"], "browser re-crawl")
    runner.mark_refresh_failed(env, ["T-1"], "Pipeline exited with an error: no chromium")
    html = _get(f"/dashboard/{_RUN_ID}").text
    assert "refresh-failed" in html
    assert "no chromium" in html
    assert 'data-refreshing="1"' not in html


def test_dashboard_renders_refreshed_badge_with_timestamp(env):
    _write_run(env, [_record("T-1")])
    runner.mark_refresh_running(env, ["T-1"], "manual content")
    runner.mark_refresh_done(env, ["T-1"])
    html = _get(f"/dashboard/{_RUN_ID}").text
    assert "refresh-ok" in html
    assert "Refreshed 20" in html  # ISO timestamp in the hover title


# ---------------------------------------------------------------------------
# Task 1: Expand / Collapse All
# ---------------------------------------------------------------------------

def test_expand_all_button_on_results_page(env):
    _write_run(env, [_record("T-1")])
    html = _get(f"/dashboard/{_RUN_ID}").text
    assert 'id="expand-all-btn"' in html
    assert "toggleExpandAll(this)" in html
    assert ">Expand All</button>" in html


def test_toggle_expand_all_js_respects_filter():
    js = (_API_DIR / "static" / "app.js").read_text(encoding="utf-8")
    assert "function toggleExpandAll" in js
    body = js.split("function toggleExpandAll", 1)[1].split("\n}", 1)[0]
    # Only visible (unfiltered) rows are toggled, and the label flips with state.
    assert "row.style.display === 'none'" in body
    assert "'Collapse All'" in body
