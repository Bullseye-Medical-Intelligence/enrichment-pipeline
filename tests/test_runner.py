"""
Tests for runner.py: monitor_pipeline completion guard and runs.read_progress.

Uses a fake subprocess process and real tmp-path run directories.
No network, no real pipeline.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("PIPELINE_API_KEY", "test-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import runs    # noqa: E402
import runner  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_ENRICHED = {
    "records": [
        {"record_id": "T-1", "practice_name": "Alpha Clinic",
         "target_tier": "Bullseye", "bullseye_score": 85,
         "exclusion_status": "CLEAR"},
    ]
}
_VALID_LOG = {
    "run_id": "RUN-TEST",
    "records_output": 1,
    "records_excluded": 0,
    "records_failed": 0,
}


class _FakeProcess:
    def __init__(self, returncode: int = 0, stderr: bytes = b""):
        self.returncode = returncode
        self._stderr = stderr

    def communicate(self):
        return (b"", self._stderr)


@pytest.fixture
def run_store(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_dir)
    monkeypatch.setattr(runner, "OUTPUT_RUNS_PATH", runs_dir, raising=False)
    return runs_dir


def _make_run(run_store: Path, run_id: str) -> Path:
    """Create a minimal run directory with status.json."""
    run_dir = run_store / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({
        "run_id": run_id,
        "project_id": "test-project",
        "source_type": "outscraper",
        "input_filename": "test.csv",
        "status": "running",
        "created_at": "2026-05-28T10:00:00+00:00",
        "operator": "tester",
        "records_input": 1,
    }))
    return run_dir


# ---------------------------------------------------------------------------
# monitor_pipeline — success path
# ---------------------------------------------------------------------------

def test_monitor_marks_complete_when_both_files_valid(run_store):
    run_id = "RUN-20260528-100000-aaaa"
    run_dir = _make_run(run_store, run_id)
    (run_dir / "enriched_targets.json").write_text(json.dumps(_VALID_ENRICHED))
    (run_dir / "run_log.json").write_text(json.dumps(_VALID_LOG))

    asyncio.run(runner.monitor_pipeline(run_id, _FakeProcess(returncode=0)))

    status = runs.get_run(run_id)
    assert status.status == "complete"
    assert status.bullseye_count == 1


# ---------------------------------------------------------------------------
# monitor_pipeline — failure paths (exit 0 but bad output)
# ---------------------------------------------------------------------------

def test_monitor_marks_failed_when_enriched_missing(run_store):
    run_id = "RUN-20260528-100001-bbbb"
    _make_run(run_store, run_id)
    # No output files written.
    asyncio.run(runner.monitor_pipeline(run_id, _FakeProcess(returncode=0)))

    status = runs.get_run(run_id)
    assert status.status == "failed"
    assert "enriched_targets.json" in status.error_summary


def test_monitor_marks_failed_when_enriched_malformed(run_store):
    run_id = "RUN-20260528-100002-cccc"
    run_dir = _make_run(run_store, run_id)
    (run_dir / "enriched_targets.json").write_text("{ not valid json")

    asyncio.run(runner.monitor_pipeline(run_id, _FakeProcess(returncode=0)))

    status = runs.get_run(run_id)
    assert status.status == "failed"
    assert "malformed" in status.error_summary.lower()


def test_monitor_marks_failed_when_log_missing(run_store):
    run_id = "RUN-20260528-100003-dddd"
    run_dir = _make_run(run_store, run_id)
    (run_dir / "enriched_targets.json").write_text(json.dumps(_VALID_ENRICHED))
    # No run_log.json

    asyncio.run(runner.monitor_pipeline(run_id, _FakeProcess(returncode=0)))

    status = runs.get_run(run_id)
    assert status.status == "failed"
    assert "run_log.json" in status.error_summary


def test_monitor_marks_failed_when_log_malformed(run_store):
    run_id = "RUN-20260528-100004-eeee"
    run_dir = _make_run(run_store, run_id)
    (run_dir / "enriched_targets.json").write_text(json.dumps(_VALID_ENRICHED))
    (run_dir / "run_log.json").write_text("[ not an object ]")

    asyncio.run(runner.monitor_pipeline(run_id, _FakeProcess(returncode=0)))

    status = runs.get_run(run_id)
    assert status.status == "failed"
    assert "run_log.json" in status.error_summary


def test_monitor_marks_failed_on_nonzero_exit(run_store):
    run_id = "RUN-20260528-100005-ffff"
    _make_run(run_store, run_id)

    asyncio.run(runner.monitor_pipeline(
        run_id, _FakeProcess(returncode=1, stderr=b"SyntaxError: bad code")
    ))

    status = runs.get_run(run_id)
    assert status.status == "failed"
    assert "SyntaxError" in status.error_summary


# ---------------------------------------------------------------------------
# runs.read_progress
# ---------------------------------------------------------------------------

def test_read_progress_returns_none_for_missing_file(run_store):
    run_id = "RUN-20260528-200000-aa00"
    _make_run(run_store, run_id)
    assert runs.read_progress(run_id) is None


def test_read_progress_returns_none_for_malformed_file(run_store):
    run_id = "RUN-20260528-200001-bb11"
    run_dir = _make_run(run_store, run_id)
    (run_dir / "progress.json").write_text("{ bad json")
    assert runs.read_progress(run_id) is None


def test_read_progress_returns_data_for_valid_file(run_store):
    run_id = "RUN-20260528-200002-cc22"
    run_dir = _make_run(run_store, run_id)
    progress = {
        "step_num": 4, "step_name": "Signal extraction (Claude)",
        "step_total": 8, "records_done": 12, "records_total": 30,
        "updated_at": "2026-05-28T10:05:00+00:00",
    }
    (run_dir / "progress.json").write_text(json.dumps(progress))

    result = runs.read_progress(run_id)
    assert result["step_num"] == 4
    assert result["records_done"] == 12


def test_read_progress_returns_none_for_invalid_run_id(run_store):
    assert runs.read_progress("../../etc/passwd") is None
