"""
tests/test_run_counts.py
Tests that run-level tier counts in status.json stay consistent with what the
results page shows: refreshed after a post-run pass rewrites records, and
counting the tier an operator actually sees (analyst overrides + retroactive
normalization), not the raw pipeline tier.
Deterministic: filesystem only, no subprocesses, no network.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import runner  # noqa: E402
import runs  # noqa: E402
from schema import RunStatus  # noqa: E402

_RUN_ID = "RUN-20260722-150000"


def _record(rid, tier, score=80, exclusion="CLEAR", enrichment="complete"):
    return {
        "id": rid, "record_id": rid, "practice_name": f"Practice {rid}",
        "target_tier": tier, "bullseye_score": score,
        "exclusion_status": exclusion, "enrichment_status": enrichment,
    }


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    """Point runs/ at a tmp dir holding one complete run."""
    runs_root = tmp_path / "runs"
    run_dir = runs_root / _RUN_ID
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_root)

    status = RunStatus(
        run_id=_RUN_ID, project_id="p1", source_type="outscraper",
        input_filename="in.csv", status="complete",
        created_at="2026-07-22T15:00:00Z", operator="tester",
        records_input=3, records_output=3,
        bullseye_count=99, contender_count=99,  # deliberately wrong / stale
        needs_verification_count=99, manual_review_count=99, excluded_count=99,
    )
    (run_dir / "status.json").write_text(status.model_dump_json(), encoding="utf-8")
    return run_dir


def _write_records(run_dir, records):
    (run_dir / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": records}), encoding="utf-8"
    )


def _write_reviews(run_dir, reviews_map):
    (run_dir / "reviews.json").write_text(json.dumps(reviews_map), encoding="utf-8")


class TestRefreshRunCounts:

    def test_refresh_replaces_stale_counts(self, run_env):
        """A rescore moved records between tiers; the run list must follow."""
        _write_records(run_env, [
            _record("T-1", "Bullseye"), _record("T-2", "Bullseye"),
            _record("T-3", "Contender"),
        ])
        counts = runner.refresh_run_counts(_RUN_ID)
        assert counts["bullseye_count"] == 2
        assert counts["contender_count"] == 1
        # Persisted, not just returned.
        assert runs.get_run(_RUN_ID).bullseye_count == 2
        assert runs.get_run(_RUN_ID).contender_count == 1

    def test_counts_follow_analyst_override(self, run_env):
        """An override promotes a Contender; the run list count must move with it."""
        _write_records(run_env, [_record("T-1", "Bullseye"), _record("T-2", "Contender")])
        _write_reviews(run_env, {
            "T-2": {"override_tier": "Bullseye", "override_reason": "confirmed by call",
                    "qc_status": "approved"},
        })
        counts = runner.refresh_run_counts(_RUN_ID)
        assert counts["bullseye_count"] == 2
        assert counts["contender_count"] == 0

    def test_override_off_excluded_leaves_excluded_bucket(self, run_env):
        """An analyst override on a hard-excluded record moves it out of Excluded,
        matching what the results page and client exports already show."""
        _write_records(run_env, [_record("T-1", "Excluded", score=20, exclusion="EXCLUDED")])
        _write_reviews(run_env, {
            "T-1": {"override_tier": "Bullseye", "override_reason": "independent after all",
                    "qc_status": "approved"},
        })
        counts = runner.refresh_run_counts(_RUN_ID)
        assert counts["excluded_count"] == 0
        assert counts["bullseye_count"] == 1

    def test_low_score_contender_counts_as_manual_review(self, run_env):
        """Retroactive normalization (Contender + score < 50 -> Manual Review) is
        applied on the results page; counts must agree rather than diverge."""
        _write_records(run_env, [_record("T-1", "Contender", score=30)])
        counts = runner.refresh_run_counts(_RUN_ID)
        assert counts["contender_count"] == 0
        assert counts["manual_review_count"] == 1

    def test_error_count_tracks_enrichment_status(self, run_env):
        _write_records(run_env, [
            _record("T-1", "Bullseye"),
            _record("T-2", "Manual Review", enrichment="failed"),
        ])
        assert runner.refresh_run_counts(_RUN_ID)["error_count"] == 1

    def test_damaged_reviews_still_refreshes_counts(self, run_env):
        """A corrupt overlay must not freeze counts at a stale value; it already
        fails loudly on the read paths."""
        _write_records(run_env, [_record("T-1", "Bullseye")])
        (run_env / "reviews.json").write_text("{ not json", encoding="utf-8")
        counts = runner.refresh_run_counts(_RUN_ID)
        assert counts["bullseye_count"] == 1

    def test_missing_output_is_noop(self, run_env):
        assert runner.refresh_run_counts(_RUN_ID) == {}

    def test_unreadable_output_is_noop(self, run_env):
        (run_env / "enriched_targets.json").write_text("{ broken", encoding="utf-8")
        assert runner.refresh_run_counts(_RUN_ID) == {}
        # Stale counts are left alone rather than zeroed out.
        assert runs.get_run(_RUN_ID).bullseye_count == 99


class TestAddLlmUsage:

    def test_usage_accumulates(self, run_env):
        """A re-extraction's Claude spend is added to the run's reported total."""
        runs.update_run_status(
            _RUN_ID, llm_input_tokens=1000, llm_output_tokens=200, llm_call_count=10)
        runner.add_llm_usage(_RUN_ID, 500, 100, 5)
        s = runs.get_run(_RUN_ID)
        assert s.llm_input_tokens == 1500
        assert s.llm_output_tokens == 300
        assert s.llm_call_count == 15

    def test_zero_calls_is_noop(self, run_env):
        runs.update_run_status(
            _RUN_ID, llm_input_tokens=1000, llm_output_tokens=200, llm_call_count=10)
        runner.add_llm_usage(_RUN_ID, 0, 0, 0)
        assert runs.get_run(_RUN_ID).llm_call_count == 10

    def test_pre_capture_run_left_uncaptured(self, run_env):
        """A run predating token capture stays 'not captured' — showing one pass's
        tokens as the run total would read as the whole cost."""
        assert runs.get_run(_RUN_ID).llm_call_count is None
        runner.add_llm_usage(_RUN_ID, 500, 100, 5)
        assert runs.get_run(_RUN_ID).llm_call_count is None
        assert runs.get_run(_RUN_ID).llm_input_tokens is None
