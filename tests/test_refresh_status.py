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


def test_non_string_started_at_degrades_not_500(tmp_path):
    """A null or numeric started_at (hand-edit / partial write) must be treated
    like an unparseable timestamp — reported failed — not raise TypeError."""
    runner.mark_refresh_running(tmp_path, ["T-1", "T-2"], "re-enrich")
    path = tmp_path / runner.REFRESH_STATUS_FILENAME
    data = json.loads(path.read_text())
    data["T-1"]["started_at"] = None
    data["T-2"]["started_at"] = 1234567890
    path.write_text(json.dumps(data))

    loaded = runner.load_refresh_status(tmp_path)
    assert loaded["T-1"]["state"] == "failed"
    assert loaded["T-2"]["state"] == "failed"


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


# ---------------------------------------------------------------------------
# Heartbeat staleness + live progress (job_dir -> scratch progress.json)
# ---------------------------------------------------------------------------

def _age_started_at(run_directory, rid, minutes):
    path = run_directory / runner.REFRESH_STATUS_FILENAME
    data = json.loads(path.read_text())
    data[rid]["started_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat()
    path.write_text(json.dumps(data))


def _write_job_progress(run_directory, job_dir, minutes_ago=0, **over):
    """Write a scratch progress.json whose updated_at AND mtime are minutes_ago old."""
    scratch = run_directory / job_dir
    scratch.mkdir(parents=True, exist_ok=True)
    stamp_dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    progress = {"step_num": 3, "step_name": "Web extraction", "step_total": 8,
                "records_done": 14, "records_total": 38,
                "updated_at": stamp_dt.isoformat()}
    progress.update(over)
    path = scratch / "progress.json"
    path.write_text(json.dumps(progress))
    os.utime(path, (stamp_dt.timestamp(), stamp_dt.timestamp()))


def test_fresh_heartbeat_keeps_long_job_running(tmp_path):
    """A batch older than the stale window whose scratch progress.json is still
    being written must stay 'running' with a progress line — a healthy long
    browser batch is never falsely reported failed."""
    runner.mark_refresh_running(tmp_path, ["T-1"], "browser re-crawl", job_dir=".batch_ab12")
    _age_started_at(tmp_path, "T-1", config.REFRESH_STALE_MINUTES + 30)
    _write_job_progress(tmp_path, ".batch_ab12")
    state = runner.load_refresh_status(tmp_path)["T-1"]
    assert state["state"] == "running"
    assert state["progress_display"] == "Step 3/8 Web extraction · 14/38 records"


def test_dead_job_goes_stale_one_window_after_last_write(tmp_path):
    """A job whose heartbeat stopped a full stale window ago reports failed."""
    runner.mark_refresh_running(tmp_path, ["T-1"], "browser re-crawl", job_dir=".batch_ab12")
    _age_started_at(tmp_path, "T-1", config.REFRESH_STALE_MINUTES + 30)
    _write_job_progress(tmp_path, ".batch_ab12", minutes_ago=config.REFRESH_STALE_MINUTES + 10)
    state = runner.load_refresh_status(tmp_path)["T-1"]
    assert state["state"] == "failed"
    assert "did not report completion" in state["error"]


def test_job_dir_traversal_is_refused(tmp_path):
    """A hand-edited path-like job_dir must not read outside the run dir — the
    entry degrades to the started_at-only staleness check."""
    runner.mark_refresh_running(tmp_path, ["T-1"], "re-enrich", job_dir="../../etc")
    assert runner.load_refresh_status(tmp_path)["T-1"]["state"] == "running"
    _age_started_at(tmp_path, "T-1", config.REFRESH_STALE_MINUTES + 5)
    assert runner.load_refresh_status(tmp_path)["T-1"]["state"] == "failed"


def test_progress_display_absent_without_job_progress(tmp_path):
    runner.mark_refresh_running(tmp_path, ["T-1"], "re-enrich")
    assert "progress_display" not in runner.load_refresh_status(tmp_path)["T-1"]


def test_done_entry_drops_job_dir(tmp_path):
    runner.mark_refresh_running(tmp_path, ["T-1"], "re-enrich", job_dir=".batch_x")
    runner.mark_refresh_done(tmp_path, ["T-1"])
    raw = json.loads((tmp_path / runner.REFRESH_STATUS_FILENAME).read_text())
    assert "job_dir" not in raw["T-1"]


def test_format_job_progress_tolerates_garbage():
    assert runner._format_job_progress({}) == ""
    assert runner._format_job_progress({"step_num": "x", "records_total": "n"}) == ""
    assert runner._format_job_progress({"step_name": "Web extraction"}) == "Web extraction"


def test_refresh_status_route_serves_progress_display(env):
    _write_run(env, [_record("T-1")])
    runner.mark_refresh_running(env, ["T-1"], "browser re-crawl", job_dir=".batch_ab")
    _write_job_progress(env, ".batch_ab")
    r = _get(f"/runs/{_RUN_ID}/refresh-status")
    assert r.json()["T-1"]["progress_display"] == "Step 3/8 Web extraction · 14/38 records"


def test_dashboard_renders_progress_line(env):
    _write_run(env, [_record("T-1")])
    runner.mark_refresh_running(env, ["T-1"], "browser re-crawl", job_dir=".batch_ab")
    _write_job_progress(env, ".batch_ab")
    r = _get(f"/dashboard/{_RUN_ID}")
    assert r.status_code == 200
    assert "Step 3/8 Web extraction · 14/38 records" in r.text


# ---------------------------------------------------------------------------
# Double-submit guard
# ---------------------------------------------------------------------------

def test_batch_reenrich_refuses_overlapping_records(env):
    _write_run(env, [_record("T-1")])
    runner.mark_refresh_running(env, ["T-1"], "browser re-crawl")
    with pytest.raises(runner.RefreshInProgress):
        asyncio.run(runner.orchestrate_batch_reenrich(
            _RUN_ID, ["T-1"], "tester", None, use_playwright=True))


def test_batch_guard_ignores_stale_running_entry(env, monkeypatch):
    """A stale (dead) running entry must NOT block a new attempt."""
    _write_run(env, [_record("T-1")])
    (env / runner.PROJECT_CONFIG_SNAPSHOT_FILENAME).write_text(json.dumps({"client_name": "X"}))
    (env / runner.ICP_SNAPSHOT_FILENAME).write_text(json.dumps({"signals": []}))
    runner.mark_refresh_running(env, ["T-1"], "browser re-crawl")
    _age_started_at(env, "T-1", config.REFRESH_STALE_MINUTES + 5)

    spawned = {}
    monkeypatch.setattr(runner, "spawn_pipeline",
                        lambda *a, **k: spawned.setdefault("process", _FakeProcess()))

    class _CollectingBackground:
        def add_task(self, *a, **k):
            spawned["task"] = True

    count = asyncio.run(runner.orchestrate_batch_reenrich(
        _RUN_ID, ["T-1"], "tester", _CollectingBackground(), use_playwright=True))
    assert count == 1
    assert "process" in spawned and "task" in spawned


def test_single_record_prepare_refuses_mid_refresh(env):
    _write_run(env, [_record("T-1")])
    runner.mark_refresh_running(env, ["T-1"], "browser re-crawl")
    with pytest.raises(runner.RefreshInProgress):
        runner._prepare_single_record_job(_RUN_ID, "T-1")


def test_rerun_selected_double_submit_redirects_with_notice(env):
    _write_run(env, [_record("T-1")])
    runner.mark_refresh_running(env, ["T-1"], "browser re-crawl")
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        r = c.post(f"/dashboard/{_RUN_ID}/rerun-selected",
                   data={"record_ids": "T-1", "use_playwright": "1"},
                   follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert "notice=" in location
    assert "re-enrich" in location
