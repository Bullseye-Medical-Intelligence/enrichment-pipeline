"""
tests/test_run_write_guard.py
Tests for the cross-process run-state write guard in output/atomic_write.py:
fingerprint compare-and-replace under .run.lock, used by every post-run CLI's
final enriched_targets.json write. Deterministic — no subprocesses, no network.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from output.atomic_write import (
    ConcurrentRunChange,
    RUN_LOCK_FILENAME,
    guarded_replace,
    run_state_lock,
    stat_fingerprint,
)


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj), encoding="utf-8")


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "RUN-20260722-120000"
    d.mkdir()
    _write_json(d / "enriched_targets.json", {"records": [{"id": "T-1"}]})
    return d


class TestStatFingerprint:

    def test_missing_file_is_none(self, tmp_path):
        assert stat_fingerprint(tmp_path / "nope.json") is None

    def test_rewrite_changes_fingerprint(self, run_dir):
        target = run_dir / "enriched_targets.json"
        fp1 = stat_fingerprint(target)
        # os.replace-style rewrite (new inode) must change the fingerprint even
        # when size and content stay identical.
        tmp = run_dir / "enriched_targets.tmp"
        tmp.write_bytes(target.read_bytes())
        os.replace(tmp, target)
        assert stat_fingerprint(target) != fp1


class TestGuardedReplace:

    def test_happy_path_replaces(self, run_dir):
        target = run_dir / "enriched_targets.json"
        fp = stat_fingerprint(target)
        tmp = run_dir / "enriched_targets.tmp"
        _write_json(tmp, {"records": [{"id": "T-1", "updated": True}]})
        guarded_replace(run_dir, target, tmp, fp)
        assert json.loads(target.read_text())["records"][0]["updated"] is True
        assert not tmp.exists()

    def test_concurrent_change_refused_and_preserved(self, run_dir):
        """A rewrite that landed after load wins; the pass write is refused."""
        target = run_dir / "enriched_targets.json"
        fp = stat_fingerprint(target)

        # Concurrent writer (e.g. a batch merge) replaces the file mid-pass.
        merge_tmp = run_dir / "merge.tmp"
        _write_json(merge_tmp, {"records": [{"id": "T-1", "merged": True}]})
        os.replace(merge_tmp, target)
        merged_bytes = target.read_bytes()

        pass_tmp = run_dir / "enriched_targets.tmp"
        _write_json(pass_tmp, {"records": [{"id": "T-1", "stale_pass": True}]})
        with pytest.raises(ConcurrentRunChange) as exc:
            guarded_replace(run_dir, target, pass_tmp, fp)
        assert "Nothing was written" in str(exc.value)
        assert target.read_bytes() == merged_bytes  # merge data intact
        assert not pass_tmp.exists()  # stale tmp cleaned up

    def test_deleted_file_refused(self, run_dir):
        """If the run was deleted mid-pass, the write is refused, not recreated."""
        target = run_dir / "enriched_targets.json"
        fp = stat_fingerprint(target)
        target.unlink()
        tmp = run_dir / "enriched_targets.tmp"
        _write_json(tmp, {"records": []})
        with pytest.raises(ConcurrentRunChange):
            guarded_replace(run_dir, target, tmp, fp)
        assert not target.exists()


class TestRunStateLock:

    def test_lock_file_matches_api_side(self, run_dir):
        with run_state_lock(run_dir):
            assert (run_dir / RUN_LOCK_FILENAME).exists()
        assert RUN_LOCK_FILENAME == ".run.lock"

    def test_held_lock_blocks_second_acquirer(self, run_dir):
        """A held lock makes a second acquisition raise after its timeout."""
        acquired = threading.Event()
        release = threading.Event()

        def _holder():
            with run_state_lock(run_dir):
                acquired.set()
                release.wait(timeout=10)

        t = threading.Thread(target=_holder, daemon=True)
        t.start()
        assert acquired.wait(timeout=5)
        try:
            with pytest.raises(ConcurrentRunChange):
                with run_state_lock(run_dir, timeout=0.2):
                    pass
        finally:
            release.set()
            t.join(timeout=5)

    def test_guarded_replace_waits_for_lock(self, run_dir):
        """guarded_replace serializes behind a briefly-held lock, then succeeds."""
        target = run_dir / "enriched_targets.json"
        fp = stat_fingerprint(target)
        tmp = run_dir / "enriched_targets.tmp"
        _write_json(tmp, {"records": [{"id": "T-1", "v": 2}]})

        acquired = threading.Event()

        def _brief_holder():
            with run_state_lock(run_dir):
                acquired.set()
                # Hold briefly, then release — guarded_replace should proceed.
                import time
                time.sleep(0.2)

        t = threading.Thread(target=_brief_holder, daemon=True)
        t.start()
        assert acquired.wait(timeout=5)
        guarded_replace(run_dir, target, tmp, fp)
        t.join(timeout=5)
        assert json.loads(target.read_text())["records"][0]["v"] == 2
