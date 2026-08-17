"""
test_reextract_route_spend.py

Route tests for POST /dashboard/{run_id}/reextract cost booking. Deterministic —
the post-run CLI subprocess is monkeypatched; no LLM, no network.

Guarantee under test: the route folds the pass's Claude usage into the run's
totals from the CLI's stats line even when the pass exits non-zero (a refused
write after a concurrent merge), not only on success.
"""

import json
import os
import sys
import types
from pathlib import Path

import pytest

# pipeline-api modules import each other by bare name; put the dir on the path.
_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import runner  # noqa: E402
import runs  # noqa: E402
import ui  # noqa: E402

_RUN_ID = "RUN-20260621-120000-aaaa"


def _write_run(run_directory: Path) -> None:
    """Write a complete run dir: status.json + targets + ICP snapshot."""
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-06-21T12:00:00+00:00", "operator": "tester",
        "llm_input_tokens": 100, "llm_output_tokens": 10, "llm_call_count": 1,
    }))
    (run_directory / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": []})
    )
    (run_directory / "icp_snapshot.json").write_text(json.dumps({
        "icp_id": "icp", "name": "ICP", "version": "1.0",
        "signals": [{"signal_id": "S-1", "signal_label": "x",
                     "prompt_instruction": "y", "positive_weight": 10}],
    }))


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    """Point OUTPUT_RUNS_PATH at tmp_path and create a complete run."""
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path)
    run_directory = tmp_path / _RUN_ID
    _write_run(run_directory)
    return run_directory


@pytest.fixture
def client(run_env):
    """Logged-in TestClient sharing the monkeypatched run environment."""
    with TestClient(main.app, raise_server_exceptions=False) as c:
        r = c.post("/login", data={"username": "tester", "password": "secret-pw"})
        assert r.status_code in (200, 302, 303)
        yield c


def _fake_cli(returncode: int, stdout: str, stderr: str = ""):
    """Replace ui._run_postrun_cli with a stub returning a canned result."""
    result = types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    def _factory(run_directory, cmd, timeout):
        return lambda: result
    return _factory


_REFUSAL_STDOUT = (
    "Re-extracting signals for RUN…\n"
    "20\n"  # a bare-number line must not break the stats parse
    + json.dumps({
        "refused": True, "error": "run changed during the pass",
        "processed": 3, "skipped": 0, "skipped_excluded": 0, "tier_changes": [],
        "llm_input_tokens": 5000, "llm_output_tokens": 400, "llm_call_count": 3,
    }) + "\n"
)


def test_refused_pass_books_spend(client, run_env, monkeypatch):
    """A non-zero exit with a refusal stats line still books the Claude usage."""
    booked = []
    monkeypatch.setattr(ui, "_run_postrun_cli",
                        _fake_cli(1, _REFUSAL_STDOUT, stderr="run changed"))
    monkeypatch.setattr(
        runner, "add_llm_usage",
        lambda run_id, inp, out, calls: booked.append((run_id, inp, out, calls)),
    )

    r = client.post(f"/dashboard/{_RUN_ID}/reextract")

    assert r.status_code == 500
    assert booked == [(_RUN_ID, 5000, 400, 3)]


def test_successful_pass_still_books_spend(client, run_env, monkeypatch):
    """The success path keeps booking usage (no regression from the reorder)."""
    booked = []
    stdout = json.dumps({
        "processed": 2, "skipped": 0, "skipped_excluded": 0, "tier_changes": [],
        "llm_input_tokens": 3000, "llm_output_tokens": 200, "llm_call_count": 2,
    }) + "\n"
    monkeypatch.setattr(ui, "_run_postrun_cli", _fake_cli(0, stdout))
    monkeypatch.setattr(
        runner, "add_llm_usage",
        lambda run_id, inp, out, calls: booked.append((run_id, inp, out, calls)),
    )
    monkeypatch.setattr(runner, "refresh_run_counts", lambda run_id: None)

    r = client.post(f"/dashboard/{_RUN_ID}/reextract", follow_redirects=False)

    assert r.status_code == 303
    assert booked == [(_RUN_ID, 3000, 200, 2)]
