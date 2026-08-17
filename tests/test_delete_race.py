"""
test_delete_race.py

Run deletion vs post-run pass safety (review backlog P1-4). Deterministic —
locks are held on fresh descriptors in-process (flock is per open file
description, so one thread contends with itself exactly like two processes).

Guarantees under test:
  - delete_run refuses while a post-run CLI pass holds .postrun.lock
  - delete_run maps a busy .run.lock to a per-run ValueError (bulk delete
    skips the run instead of aborting with a 503)
  - a deleted run directory is never resurrected by per-run lock acquisition
  - the CLIs' run_state_lock refuses cleanly (ConcurrentRunChange) when the
    run directory was deleted mid-pass, instead of an uncaught traceback
"""

import json
import os
import sys
from pathlib import Path

import pytest

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import locking  # noqa: E402
import runs  # noqa: E402

from output.atomic_write import ConcurrentRunChange, run_state_lock  # noqa: E402

_RUN_ID = "RUN-20260621-120000-aaaa"


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    """Point OUTPUT_RUNS_PATH at tmp_path and create a completed run."""
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path)
    run_directory = tmp_path / _RUN_ID
    run_directory.mkdir()
    (run_directory / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-06-21T12:00:00+00:00", "operator": "tester",
    }))
    (run_directory / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": []})
    )
    return run_directory


class TestDeleteVsPostrunPass:

    def test_delete_refused_while_postrun_lock_held(self, run_env):
        """A running post-run pass blocks deletion with a clear refusal."""
        with locking.file_lock(locking.postrun_lock_path(run_env), timeout=1.0):
            with pytest.raises(ValueError, match="post-run pass in progress"):
                runs.delete_run(_RUN_ID)
        assert run_env.exists()
        assert (run_env / "enriched_targets.json").exists()

    def test_delete_refused_while_run_lock_held(self, run_env, monkeypatch):
        """A busy state lock maps to ValueError, not a LockTimeout 503."""
        monkeypatch.setattr(runs, "_DELETE_DRAIN_TIMEOUT_SECONDS", 0.2)
        with locking.run_lock(run_env):
            with pytest.raises(ValueError, match="busy with another operation"):
                runs.delete_run(_RUN_ID)
        assert run_env.exists()

    def test_delete_succeeds_when_no_pass_running(self, run_env):
        """Existing lock FILES (idle artifacts) never block deletion."""
        locking.run_lock_path(run_env).touch()
        locking.postrun_lock_path(run_env).touch()
        runs.delete_run(_RUN_ID)
        assert not run_env.exists()


class TestNoGhostRunDirectory:

    def test_run_lock_does_not_resurrect_deleted_run_dir(self, run_env):
        """Acquiring the per-run lock on a deleted run must not recreate it."""
        import shutil
        shutil.rmtree(run_env)
        with pytest.raises(FileNotFoundError):
            with locking.run_lock(run_env):
                pass
        assert not run_env.exists()

    def test_run_state_lock_refuses_cleanly_on_deleted_dir(self, tmp_path):
        """The CLI-side lock maps a deleted run dir to ConcurrentRunChange."""
        gone = tmp_path / "RUN-20260621-130000-bbbb"
        with pytest.raises(ConcurrentRunChange, match="no longer exists"):
            with run_state_lock(gone):
                pass
        assert not gone.exists()
