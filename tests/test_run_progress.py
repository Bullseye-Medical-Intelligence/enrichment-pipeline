"""
tests/test_run_progress.py
Tests for live run-progress reporting: NPI enrichment's per-record callback
(the one long step that previously reported nothing for its whole duration),
step-start stamping in progress.json, and the UI's rate/ETA derivation.
Deterministic — NPPES is monkeypatched, no network.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_API_DIR = REPO_ROOT / "pipeline-api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(REPO_ROOT))

import pipeline  # noqa: E402
from ingestion import npi_lookup  # noqa: E402
from ui import _enrich_progress, _format_duration  # noqa: E402


class TestNpiProgressCallback:

    def _records(self, n):
        return [{"id": f"T-{i}", "practice_name": f"P{i}", "address_zip": "95823",
                 "npi_optional": None, "phone": ""} for i in range(n)]

    def test_callback_fires_per_record(self, monkeypatch):
        monkeypatch.setattr(npi_lookup, "_match_record",
                            lambda record, rules: dict(npi_lookup._EMPTY_NPI_FIELDS))
        monkeypatch.setattr(npi_lookup, "_REQUEST_DELAY_SECONDS", 0)
        calls = []
        npi_lookup.enrich_records(self._records(4), {},
                                  progress_callback=lambda d, t: calls.append((d, t)))
        assert calls == [(1, 4), (2, 4), (3, 4), (4, 4)]

    def test_callback_error_never_breaks_enrichment(self, monkeypatch):
        monkeypatch.setattr(npi_lookup, "_match_record",
                            lambda record, rules: dict(npi_lookup._EMPTY_NPI_FIELDS))
        monkeypatch.setattr(npi_lookup, "_REQUEST_DELAY_SECONDS", 0)

        def _boom(done, total):
            raise RuntimeError("display broke")

        records = npi_lookup.enrich_records(self._records(3), {}, progress_callback=_boom)
        assert len(records) == 3

    def test_no_callback_still_works(self, monkeypatch):
        monkeypatch.setattr(npi_lookup, "_match_record",
                            lambda record, rules: dict(npi_lookup._EMPTY_NPI_FIELDS))
        monkeypatch.setattr(npi_lookup, "_REQUEST_DELAY_SECONDS", 0)
        assert len(npi_lookup.enrich_records(self._records(2), {})) == 2


class TestStepStartStamping:

    def _read(self, d):
        return json.loads((Path(d) / "progress.json").read_text(encoding="utf-8"))

    def test_started_at_stable_within_a_step(self, tmp_path):
        pipeline._progress_step_state["key"] = None
        pipeline._write_progress(str(tmp_path), 1, "NPI enrichment", 0, 100)
        first = self._read(tmp_path)["step_started_at"]
        pipeline._write_progress(str(tmp_path), 1, "NPI enrichment", 50, 100)
        assert self._read(tmp_path)["step_started_at"] == first

    def test_started_at_advances_on_new_step(self, tmp_path):
        pipeline._progress_step_state["key"] = None
        pipeline._write_progress(str(tmp_path), 1, "NPI enrichment", 100, 100)
        first = self._read(tmp_path)["step_started_at"]
        pipeline._progress_step_state["started_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=60)
        ).isoformat()
        pipeline._progress_step_state["key"] = None  # force restamp like a real transition
        pipeline._write_progress(str(tmp_path), 2, "URL validation", 0, 100)
        assert self._read(tmp_path)["step_started_at"] != first


class TestProgressEnrichment:

    def _progress(self, done=50, total=100, seconds_ago=60):
        return {
            "step_num": 1, "step_name": "NPI enrichment", "step_total": 8,
            "records_done": done, "records_total": total,
            "step_started_at": (
                datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
            ).isoformat(),
        }

    def test_rate_and_eta_derived(self):
        p = _enrich_progress(self._progress(done=60, total=120, seconds_ago=60))
        # 60 records in 60s = 60/min; 60 remaining at 1/s = ~1m.
        assert p["rate_display"] == "60.0/min"
        assert p["eta_display"] == "1m 0s"

    def test_too_early_shows_nothing(self):
        p = _enrich_progress(self._progress(done=1, total=100, seconds_ago=2))
        assert "rate_display" not in p and "eta_display" not in p

    def test_indeterminate_step_untouched(self):
        p = _enrich_progress({"step_num": 6, "step_name": "Exclusion check",
                              "records_done": 0, "records_total": 0})
        assert "rate_display" not in p

    def test_none_passthrough(self):
        assert _enrich_progress(None) is None

    def test_garbage_started_at_tolerated(self):
        p = self._progress()
        p["step_started_at"] = "not-a-date"
        out = _enrich_progress(p)
        assert "rate_display" not in out

    def test_format_duration(self):
        assert _format_duration(45) == "45s"
        assert _format_duration(260) == "4m 20s"
        assert _format_duration(4320) == "1h 12m"
