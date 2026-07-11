"""
test_signal_columns.py

Tests for at-a-glance ICP-signal columns on the operator dashboard (results table
+ Contact Queue). Columns are config-driven by an ICP signal's `column_label`;
each cell's state comes from the record's frozen signals. Display only, no scoring
change. Uses the FastAPI TestClient to render the dashboard and assert on markup.

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

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO))

from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
import icp_profiles  # noqa: E402
import main  # noqa: E402
import runs  # noqa: E402

_RUN_ID = "RUN-20260622-090000-dddd"

# A minimal live ICP with two labeled signals (Cash Pay, Fertility).
_LABELED_ICP = {
    "icp_id": "obgyn_femasys", "name": "OBGYN Femasys", "version": "test-v1",
    "signals": [
        {"signal_id": "S-ICP-007", "signal_label": "Cash-pay visible",
         "prompt_instruction": "x", "positive_weight": 20, "column_label": "Cash Pay"},
        {"signal_id": "S-ICP-003", "signal_label": "Fertility services",
         "prompt_instruction": "y", "positive_weight": 18, "column_label": "Fertility"},
    ],
}


def _sig(signal_id, state, **extra):
    s = {"signal_id": signal_id, "signal_label": signal_id, "signal_state": state,
         "state_inferred": False, "positive_weight": 20}
    s.update(extra)
    return s


def _record(rid, signals):
    return {
        "id": rid, "record_id": rid, "practice_name": "Practice " + rid,
        "bullseye_score": 72, "target_tier": "Contender", "exclusion_status": "CLEAR",
        "enrichment_status": "complete", "confidence_band": "Moderate",
        "address_city": "Atlanta", "address_state": "GA", "source_confidence": "high",
        "signals": signals,
    }


def _write_run(run_directory, records):
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-06-22T09:00:00+00:00",
        "completed_at": "2026-06-22T09:30:00+00:00", "operator": "tester",
        "icp_profile_id": "obgyn_femasys",
    }))
    (run_directory / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": records}, indent=2))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated run + ICP store seeded with a labeled obgyn_femasys profile.

    The startup seed-sync is stubbed so it cannot overwrite the test ICP files.
    """
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path / "runs")
    icp_dir = tmp_path / "icp"
    icp_dir.mkdir()
    monkeypatch.setattr(config, "ICP_PROFILES_PATH", icp_dir)
    monkeypatch.setattr(icp_profiles, "sync_seed_profile", lambda *a, **k: False)
    _set_icp(_LABELED_ICP)
    return tmp_path / "runs" / _RUN_ID


def _set_icp(profile):
    (config.ICP_PROFILES_PATH / "obgyn_femasys.json").write_text(json.dumps(profile))


def _get(path):
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        return c.get(path).text


def test_results_renders_labeled_columns(env):
    _write_run(env, [_record("T-1", [_sig("S-ICP-007", "yes"), _sig("S-ICP-003", "no")])])
    html = _get(f"/dashboard/{_RUN_ID}")
    assert ">Cash Pay</th>" in html
    assert ">Fertility</th>" in html
    assert 'state-yes">YES' in html      # cash pay confirmed
    assert 'state-no">NO' in html        # fertility confirmed absent


def test_queue_renders_labeled_columns(env):
    _write_run(env, [_record("T-1", [_sig("S-ICP-007", "yes")])])
    html = _get(f"/dashboard/{_RUN_ID}/queue")
    assert ">Cash Pay</th>" in html
    assert ">Fertility</th>" in html


def test_inferred_renders_inf_badge(env):
    _write_run(env, [_record("T-1", [_sig("S-ICP-007", "not_found", state_inferred=True)])])
    html = _get(f"/dashboard/{_RUN_ID}")
    assert 'state-inferred">INF' in html


def test_absent_signal_renders_no_state_badge(env):
    # Record carries neither labeled signal: the columns exist but show no
    # YES/INF/NO badge for them (blank/muted cells). Use a not_found signal so
    # the always-rendered detail panel contributes no yes/no/inf badge either.
    _write_run(env, [_record("T-1", [_sig("S-ICP-001", "not_found")])])
    html = _get(f"/dashboard/{_RUN_ID}")
    assert ">Cash Pay</th>" in html
    assert ">Fertility</th>" in html
    assert 'state-yes">YES' not in html
    assert 'state-inferred">INF' not in html
    assert 'state-no">NO' not in html


def test_no_columns_when_no_label(env):
    _set_icp({"icp_id": "obgyn_femasys", "name": "x", "version": "v", "signals": [
        {"signal_id": "S-ICP-001", "signal_label": "x", "prompt_instruction": "y",
         "positive_weight": 10},
    ]})
    _write_run(env, [_record("T-1", [_sig("S-ICP-001", "yes")])])
    html = _get(f"/dashboard/{_RUN_ID}")
    assert "signal-col-th" not in html


def test_shared_label_rolls_up_to_strongest(env):
    _set_icp({"icp_id": "obgyn_femasys", "name": "x", "version": "v", "signals": [
        {"signal_id": "S-ICP-007", "signal_label": "a", "prompt_instruction": "p",
         "positive_weight": 20, "column_label": "Cash Pay"},
        {"signal_id": "S-ICP-008", "signal_label": "b", "prompt_instruction": "p",
         "positive_weight": 0, "column_label": "Cash Pay"},
    ]})
    _write_run(env, [_record("T-1", [_sig("S-ICP-007", "yes"), _sig("S-ICP-008", "no")])])
    html = _get(f"/dashboard/{_RUN_ID}")
    assert ">Fertility</th>" not in html   # only one labeled column now
    assert 'state-yes">YES' in html        # strongest of {yes, no} = yes


def test_columns_resolve_from_snapshot_when_no_icp_profile_id(env):
    # Old run with no icp_profile_id in status.json: the icp_id is recovered from
    # the frozen icp_snapshot.json, so columns still resolve via the live ICP.
    env.mkdir(parents=True, exist_ok=True)
    (env / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-06-22T09:00:00+00:00",
        "completed_at": "2026-06-22T09:30:00+00:00", "operator": "tester",
    }))  # note: no icp_profile_id
    (env / "icp_snapshot.json").write_text(json.dumps({"icp_id": "obgyn_femasys"}))
    (env / "enriched_targets.json").write_text(json.dumps(
        {"run_id": _RUN_ID, "records": [_record("T-1", [_sig("S-ICP-007", "yes")])]}))
    html = _get(f"/dashboard/{_RUN_ID}")
    assert ">Cash Pay</th>" in html
    assert 'state-yes">YES' in html
