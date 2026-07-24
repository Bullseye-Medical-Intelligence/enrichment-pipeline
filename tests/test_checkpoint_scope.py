"""
tests/test_checkpoint_scope.py
Tests that the Step 4 crash-recovery checkpoint is scoped to the inputs that
produced it and cleaned up on success — so a later run in the same output
directory can never silently inherit the previous run's enrichment.
Deterministic: no LLM, no crawl, no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline import (
    _checkpoint_fingerprint,
    _checkpoint_path,
    _clear_step4_checkpoint,
    _init_step4_checkpoint,
    _load_step4_checkpoint,
    _write_step4_checkpoint,
)


def _inputs(tmp_path: Path, icp_body: dict | None = None, csv_body: str = "name\nAlpha\n"):
    """Write a config/ICP/input trio and return their paths."""
    config = tmp_path / "run_config.json"
    icp = tmp_path / "icp.json"
    csv = tmp_path / "input.csv"
    config.write_text(json.dumps({"bullseye_min_score": 90}), encoding="utf-8")
    icp.write_text(json.dumps(icp_body or {"signals": [{"signal_id": "S-1", "positive_weight": 10}]}),
                   encoding="utf-8")
    csv.write_text(csv_body, encoding="utf-8")
    return str(csv), str(config), str(icp)


class TestFingerprint:

    def test_same_inputs_same_fingerprint(self, tmp_path):
        csv, cfg, icp = _inputs(tmp_path)
        assert _checkpoint_fingerprint(csv, cfg, icp) == _checkpoint_fingerprint(csv, cfg, icp)

    def test_edited_icp_changes_fingerprint(self, tmp_path):
        csv, cfg, icp = _inputs(tmp_path)
        before = _checkpoint_fingerprint(csv, cfg, icp)
        # An operator raises a signal weight — the exact scenario that must
        # invalidate a checkpoint.
        Path(icp).write_text(
            json.dumps({"signals": [{"signal_id": "S-1", "positive_weight": 40}]}),
            encoding="utf-8",
        )
        assert _checkpoint_fingerprint(csv, cfg, icp) != before

    def test_edited_config_changes_fingerprint(self, tmp_path):
        csv, cfg, icp = _inputs(tmp_path)
        before = _checkpoint_fingerprint(csv, cfg, icp)
        Path(cfg).write_text(json.dumps({"bullseye_min_score": 75}), encoding="utf-8")
        assert _checkpoint_fingerprint(csv, cfg, icp) != before

    def test_different_input_file_changes_fingerprint(self, tmp_path):
        csv, cfg, icp = _inputs(tmp_path)
        other = tmp_path / "other.csv"
        other.write_text("name\nAlpha\nBeta\n", encoding="utf-8")
        assert _checkpoint_fingerprint(str(other), cfg, icp) != _checkpoint_fingerprint(csv, cfg, icp)

    def test_missing_files_do_not_raise(self, tmp_path):
        fp = _checkpoint_fingerprint(
            str(tmp_path / "nope.csv"), str(tmp_path / "nope.json"), str(tmp_path / "nope2.json")
        )
        assert isinstance(fp, str) and fp


class TestCheckpointReuse:

    def test_matching_fingerprint_resumes(self, tmp_path):
        csv, cfg, icp = _inputs(tmp_path)
        fp = _checkpoint_fingerprint(csv, cfg, icp)
        _init_step4_checkpoint(str(tmp_path), fp)
        _write_step4_checkpoint(str(tmp_path), {"id": "T-1", "enrichment_status": "complete"})

        loaded = _load_step4_checkpoint(str(tmp_path), fp)
        assert set(loaded) == {"T-1"}

    def test_changed_icp_discards_checkpoint(self, tmp_path):
        """The reported failure: edit the ICP, re-run into the same output dir.

        Record ids are deterministic, so a stale checkpoint would restore every
        record's OLD signals (scored against the OLD weights) and make zero
        Claude calls, presenting it as a fresh run.
        """
        csv, cfg, icp = _inputs(tmp_path)
        old_fp = _checkpoint_fingerprint(csv, cfg, icp)
        _init_step4_checkpoint(str(tmp_path), old_fp)
        _write_step4_checkpoint(str(tmp_path), {"id": "T-1", "enrichment_status": "complete"})

        Path(icp).write_text(
            json.dumps({"signals": [{"signal_id": "S-1", "positive_weight": 40}]}),
            encoding="utf-8",
        )
        new_fp = _checkpoint_fingerprint(csv, cfg, icp)

        assert _load_step4_checkpoint(str(tmp_path), new_fp) == {}
        # The stale file is removed, so new appends cannot mix two runs' records.
        assert not _checkpoint_path(str(tmp_path)).exists()

    def test_unstamped_legacy_checkpoint_discarded(self, tmp_path):
        """A checkpoint from a version that wrote no fingerprint is not trusted."""
        csv, cfg, icp = _inputs(tmp_path)
        _checkpoint_path(str(tmp_path)).write_text(
            json.dumps({"id": "T-1", "enrichment_status": "complete"}) + "\n", encoding="utf-8"
        )
        assert _load_step4_checkpoint(str(tmp_path), _checkpoint_fingerprint(csv, cfg, icp)) == {}

    def test_failed_records_still_skipped(self, tmp_path):
        csv, cfg, icp = _inputs(tmp_path)
        fp = _checkpoint_fingerprint(csv, cfg, icp)
        _init_step4_checkpoint(str(tmp_path), fp)
        _write_step4_checkpoint(str(tmp_path), {"id": "T-ok", "enrichment_status": "complete"})
        _write_step4_checkpoint(str(tmp_path), {"id": "T-bad", "enrichment_status": "failed"})
        assert set(_load_step4_checkpoint(str(tmp_path), fp)) == {"T-ok"}

    def test_corrupt_final_line_tolerated(self, tmp_path):
        csv, cfg, icp = _inputs(tmp_path)
        fp = _checkpoint_fingerprint(csv, cfg, icp)
        _init_step4_checkpoint(str(tmp_path), fp)
        _write_step4_checkpoint(str(tmp_path), {"id": "T-1", "enrichment_status": "complete"})
        with open(_checkpoint_path(str(tmp_path)), "a", encoding="utf-8") as f:
            f.write('{"id": "T-2", "enrich')  # killed mid-write
        assert set(_load_step4_checkpoint(str(tmp_path), fp)) == {"T-1"}

    def test_missing_checkpoint_is_empty(self, tmp_path):
        assert _load_step4_checkpoint(str(tmp_path), "anyfp") == {}


class TestCleanup:

    def test_clear_removes_file(self, tmp_path):
        _init_step4_checkpoint(str(tmp_path), "fp")
        assert _checkpoint_path(str(tmp_path)).exists()
        _clear_step4_checkpoint(str(tmp_path))
        assert not _checkpoint_path(str(tmp_path)).exists()

    def test_clear_is_idempotent(self, tmp_path):
        _clear_step4_checkpoint(str(tmp_path))  # no file — must not raise
        _clear_step4_checkpoint(str(tmp_path))

    def test_cleared_checkpoint_cannot_be_inherited(self, tmp_path):
        """After a successful run the checkpoint is gone, so an identical re-run
        re-enriches instead of replaying the previous run's records."""
        csv, cfg, icp = _inputs(tmp_path)
        fp = _checkpoint_fingerprint(csv, cfg, icp)
        _init_step4_checkpoint(str(tmp_path), fp)
        _write_step4_checkpoint(str(tmp_path), {"id": "T-1", "enrichment_status": "complete"})
        _clear_step4_checkpoint(str(tmp_path))
        assert _load_step4_checkpoint(str(tmp_path), fp) == {}
