"""
tests/test_cost_estimate.py
Tests for llm_pricing.estimate_run_cost. No I/O beyond the tmp_path fixture.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline-api"))

from llm_pricing import (
    _DEFAULT_INPUT_TOKENS_PER_RECORD,
    _DEFAULT_OUTPUT_TOKENS_PER_RECORD,
    _MAX_HISTORY_RUNS,
    estimate_cost_usd,
    estimate_run_cost,
)


def _write_status(runs_dir, run_id, **fields):
    """Write a minimal status.json into a run subdirectory."""
    d = runs_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    base = {
        "run_id": run_id,
        "project_id": "P-001",
        "source_type": "outscraper",
        "input_filename": "test.csv",
        "operator": "tester",
        "status": "complete",
        "created_at": "2026-01-01T00:00:00Z",
        "run_type": "enrichment",
    }
    base.update(fields)
    (d / "status.json").write_text(json.dumps(base), encoding="utf-8")


class TestNoHistory:

    def test_empty_runs_dir_uses_defaults(self, tmp_path):
        est = estimate_run_cost(10, tmp_path / "runs")
        assert est["using_defaults"] is True
        assert est["history_run_count"] == 0
        assert est["record_count"] == 10
        assert est["avg_input_tokens_per_record"] == _DEFAULT_INPUT_TOKENS_PER_RECORD
        assert est["avg_output_tokens_per_record"] == _DEFAULT_OUTPUT_TOKENS_PER_RECORD

    def test_default_cost_is_positive(self, tmp_path):
        est = estimate_run_cost(5, tmp_path / "runs")
        assert est["estimated_cost_usd"] > 0

    def test_zero_records_yields_zero_cost(self, tmp_path):
        est = estimate_run_cost(0, tmp_path / "runs")
        assert est["estimated_input_tokens"] == 0
        assert est["estimated_output_tokens"] == 0
        assert est["estimated_cost_usd"] == 0.0

    def test_runs_with_no_token_data_treated_as_no_history(self, tmp_path):
        runs_dir = tmp_path / "runs"
        _write_status(runs_dir, "RUN-20260101-120000", records_output=20)
        est = estimate_run_cost(10, runs_dir)
        assert est["using_defaults"] is True


class TestWithHistory:

    def test_single_run_used_for_average(self, tmp_path):
        runs_dir = tmp_path / "runs"
        _write_status(
            runs_dir, "RUN-20260101-120000",
            records_output=10,
            llm_input_tokens=50_000,
            llm_output_tokens=5_000,
        )
        est = estimate_run_cost(10, runs_dir)
        assert est["using_defaults"] is False
        assert est["history_run_count"] == 1
        assert est["avg_input_tokens_per_record"] == 5_000
        assert est["avg_output_tokens_per_record"] == 500
        assert est["estimated_input_tokens"] == 50_000
        assert est["estimated_output_tokens"] == 5_000

    def test_multiple_runs_averaged(self, tmp_path):
        runs_dir = tmp_path / "runs"
        _write_status(
            runs_dir, "RUN-20260101-120000",
            records_output=10,
            llm_input_tokens=40_000,
            llm_output_tokens=4_000,
        )
        _write_status(
            runs_dir, "RUN-20260102-120000",
            records_output=10,
            llm_input_tokens=60_000,
            llm_output_tokens=6_000,
        )
        est = estimate_run_cost(10, runs_dir)
        assert est["history_run_count"] == 2
        assert est["avg_input_tokens_per_record"] == 5_000   # (4000+6000)/2
        assert est["avg_output_tokens_per_record"] == 500    # (400+600)/2

    def test_cost_matches_estimate_cost_usd(self, tmp_path):
        runs_dir = tmp_path / "runs"
        _write_status(
            runs_dir, "RUN-20260101-120000",
            records_output=10,
            llm_input_tokens=60_000,
            llm_output_tokens=7_500,
        )
        est = estimate_run_cost(10, runs_dir)
        expected = round(estimate_cost_usd(60_000, 7_500), 4)
        assert est["estimated_cost_usd"] == expected

    def test_failed_runs_excluded(self, tmp_path):
        runs_dir = tmp_path / "runs"
        _write_status(
            runs_dir, "RUN-20260101-120000",
            status="failed",
            records_output=10,
            llm_input_tokens=50_000,
            llm_output_tokens=5_000,
        )
        est = estimate_run_cost(10, runs_dir)
        assert est["using_defaults"] is True

    def test_discovery_runs_excluded(self, tmp_path):
        runs_dir = tmp_path / "runs"
        _write_status(
            runs_dir, "RUN-20260101-120000",
            run_type="discovery",
            records_output=10,
            llm_input_tokens=50_000,
            llm_output_tokens=5_000,
        )
        est = estimate_run_cost(10, runs_dir)
        assert est["using_defaults"] is True

    def test_run_with_zero_records_output_excluded(self, tmp_path):
        runs_dir = tmp_path / "runs"
        _write_status(
            runs_dir, "RUN-20260101-120000",
            records_output=0,
            llm_input_tokens=50_000,
            llm_output_tokens=5_000,
        )
        est = estimate_run_cost(10, runs_dir)
        assert est["using_defaults"] is True

    def test_caps_at_max_history_runs(self, tmp_path):
        runs_dir = tmp_path / "runs"
        for i in range(_MAX_HISTORY_RUNS + 5):
            _write_status(
                runs_dir, f"RUN-202601{i:02d}-120000",
                records_output=10,
                llm_input_tokens=50_000,
                llm_output_tokens=5_000,
            )
        est = estimate_run_cost(5, runs_dir)
        assert est["history_run_count"] == _MAX_HISTORY_RUNS

    def test_malformed_status_json_skipped(self, tmp_path):
        runs_dir = tmp_path / "runs"
        bad_dir = runs_dir / "RUN-20260101-110000"
        bad_dir.mkdir(parents=True)
        (bad_dir / "status.json").write_text("{not valid json", encoding="utf-8")
        _write_status(
            runs_dir, "RUN-20260101-120000",
            records_output=10,
            llm_input_tokens=50_000,
            llm_output_tokens=5_000,
        )
        est = estimate_run_cost(10, runs_dir)
        assert est["using_defaults"] is False
        assert est["history_run_count"] == 1

    def test_returned_fields_complete(self, tmp_path):
        est = estimate_run_cost(10, tmp_path / "no-runs")
        required = {
            "record_count", "estimated_input_tokens", "estimated_output_tokens",
            "estimated_cost_usd", "avg_input_tokens_per_record",
            "avg_output_tokens_per_record", "history_run_count", "using_defaults",
            "priced_model", "rates_as_of",
        }
        assert required <= est.keys()
