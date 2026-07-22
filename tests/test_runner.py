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
    def __init__(self, returncode: int = 0, stderr: bytes = b"", stdout: bytes = b""):
        self.returncode = returncode
        self._stderr = stderr
        self._stdout = stdout

    def communicate(self):
        return (self._stdout, self._stderr)


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


# ---------------------------------------------------------------------------
# In-place single-record merge (_merge_recrawled_record)
# ---------------------------------------------------------------------------

def _complete_run(run_store: Path, run_id: str, records: list[dict]) -> Path:
    """Create a complete run dir with status.json + enriched_targets.json."""
    run_dir = run_store / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({
        "run_id": run_id, "project_id": "p", "source_type": "manual",
        "input_filename": "in.csv", "status": "complete",
        "created_at": "2026-05-28T10:00:00+00:00", "operator": "tester",
        "records_input": len(records), "records_output": len(records),
    }))
    (run_dir / "enriched_targets.json").write_text(json.dumps(
        {"run_id": run_id, "record_count": len(records), "records": records}))
    return run_dir


def _scratch_with(run_dir: Path, record: dict) -> Path:
    """Build a valid scratch dir holding one re-enriched record."""
    scratch = run_dir / ".recrawl_test"
    scratch.mkdir()
    (scratch / "enriched_targets.json").write_text(json.dumps({"records": [record]}))
    (scratch / "run_log.json").write_text(json.dumps(
        {"run_id": "x", "records_output": 1, "records_excluded": 0, "records_failed": 0}))
    return scratch


def test_merge_updates_record_and_counts(run_store):
    run_id = "RUN-20260528-110000-aaaa"
    run_dir = _complete_run(run_store, run_id, [
        {"id": "T-A", "practice_name": "A", "target_tier": "Contender",
         "bullseye_score": 40, "exclusion_status": "CLEAR"},
        {"id": "T-B", "practice_name": "B", "target_tier": "Bullseye",
         "bullseye_score": 90, "exclusion_status": "CLEAR"},
    ])
    scratch = _scratch_with(run_dir, {
        "id": "T-A", "practice_name": "A", "target_tier": "Bullseye",
        "bullseye_score": 92, "exclusion_status": "CLEAR"})

    runner._merge_recrawled_record(run_id, scratch, "T-A", "browser re-crawl")

    data = json.loads((run_dir / "enriched_targets.json").read_text())
    by_id = {r["id"]: r for r in data["records"]}
    assert by_id["T-A"]["target_tier"] == "Bullseye"
    assert by_id["T-A"]["bullseye_score"] == 92
    assert by_id["T-B"]["target_tier"] == "Bullseye"   # untouched
    assert data["record_count"] == 2
    status = runs.get_run(run_id)
    assert status.status == "complete"
    assert status.bullseye_count == 2
    assert status.contender_count == 0


def test_merge_stamps_review_note(run_store):
    run_id = "RUN-20260528-110001-bbbb"
    run_dir = _complete_run(run_store, run_id, [
        {"id": "T-A", "practice_name": "A", "target_tier": "Contender",
         "bullseye_score": 40, "exclusion_status": "CLEAR"}])
    (run_dir / "reviews.json").write_text(json.dumps({
        "T-A": {"analyst_note": "prior note", "override_tier": "Strong",
                "override_reason": "good fit", "qc_status": "approved",
                "reviewed_by": "tester", "reviewed_at": "2026-05-28T10:00:00+00:00"}}))
    scratch = _scratch_with(run_dir, {
        "id": "T-A", "practice_name": "A", "target_tier": "Bullseye",
        "bullseye_score": 92, "exclusion_status": "CLEAR"})

    runner._merge_recrawled_record(run_id, scratch, "T-A", "manual content")

    review = json.loads((run_dir / "reviews.json").read_text())["T-A"]
    assert review["qc_status"] == "approved"          # decision preserved
    assert review["override_tier"] == "Strong"
    assert "prior note" in review["analyst_note"]
    assert "Re-enriched on" in review["analyst_note"]
    assert "manual content" in review["analyst_note"]


def test_merge_id_not_found_leaves_source_untouched(run_store):
    run_id = "RUN-20260528-110002-cccc"
    run_dir = _complete_run(run_store, run_id, [
        {"id": "T-A", "practice_name": "A", "target_tier": "Contender",
         "bullseye_score": 40, "exclusion_status": "CLEAR"}])
    before = (run_dir / "enriched_targets.json").read_text()
    scratch = _scratch_with(run_dir, {
        "id": "T-ZZZ", "practice_name": "Z", "target_tier": "Bullseye",
        "bullseye_score": 92, "exclusion_status": "CLEAR"})

    runner._merge_recrawled_record(run_id, scratch, "T-ZZZ", "browser re-crawl")

    assert (run_dir / "enriched_targets.json").read_text() == before


def test_reenrichment_id_mismatch_leaves_source_untouched(run_store):
    # record_id IS present in the source, but the scratch output carries a
    # DIFFERENT id. Previously a fallback merged that wrong record into the
    # source slot, orphaning the analyst's review. The merge must now abort.
    run_id = "RUN-20260528-110004-eeee"
    run_dir = _complete_run(run_store, run_id, [
        {"id": "T-A", "practice_name": "A", "target_tier": "Contender",
         "bullseye_score": 40, "exclusion_status": "CLEAR"}])
    before = (run_dir / "enriched_targets.json").read_text()
    scratch = _scratch_with(run_dir, {
        "id": "T-WRONG", "practice_name": "Mismatch", "target_tier": "Bullseye",
        "bullseye_score": 95, "exclusion_status": "CLEAR"})

    result = runner._merge_recrawled_record(run_id, scratch, "T-A", "browser re-crawl")

    assert result.ok is False
    assert (run_dir / "enriched_targets.json").read_text() == before


def test_merge_blocked_recrawl_keeps_good_record(run_store):
    # Data-loss guard: a re-crawl that comes back blocked/thin (source_confidence
    # "failed"/"limited") must NOT overwrite a record that already holds a good
    # crawl. A transient bot gate would otherwise destroy confirmed signals.
    run_id = "RUN-20260528-110010-f001"
    run_dir = _complete_run(run_store, run_id, [
        {"id": "T-A", "practice_name": "A", "target_tier": "Bullseye",
         "bullseye_score": 90, "exclusion_status": "CLEAR",
         "source_confidence": "partial",
         "signals": [{"signal_id": "S-1", "signal_state": "yes"}]},
    ])
    before = (run_dir / "enriched_targets.json").read_text()
    scratch = _scratch_with(run_dir, {
        "id": "T-A", "practice_name": "A", "target_tier": "Manual Review",
        "bullseye_score": 0, "exclusion_status": "CLEAR",
        "source_confidence": "failed", "signals": []})

    result = runner._merge_recrawled_record(run_id, scratch, "T-A", "browser re-crawl")

    assert result.ok is False
    assert "blocked" in result.message.lower()
    assert (run_dir / "enriched_targets.json").read_text() == before


def test_merge_blocked_recrawl_over_blocked_record_overwrites(run_store):
    # The guard only protects readable records. When the prior record was ALREADY
    # blocked, the normal "Re-crawl Blocked Sites" flow merges the new result
    # (blocked→improved here) without being blocked by the guard.
    run_id = "RUN-20260528-110011-f002"
    run_dir = _complete_run(run_store, run_id, [
        {"id": "T-A", "practice_name": "A", "target_tier": "Manual Review",
         "bullseye_score": 0, "exclusion_status": "CLEAR",
         "source_confidence": "limited", "signals": []},
    ])
    scratch = _scratch_with(run_dir, {
        "id": "T-A", "practice_name": "A", "target_tier": "Bullseye",
        "bullseye_score": 90, "exclusion_status": "CLEAR",
        "source_confidence": "partial",
        "signals": [{"signal_id": "S-1", "signal_state": "yes"}]})

    result = runner._merge_recrawled_record(run_id, scratch, "T-A", "browser re-crawl")

    assert result.ok is True
    by_id = {r["id"]: r for r in
             json.loads((run_dir / "enriched_targets.json").read_text())["records"]}
    assert by_id["T-A"]["target_tier"] == "Bullseye"


def test_batch_merge_blocked_recrawl_keeps_good_record(run_store):
    # Same data-loss guard on the batch in-place path: a good record whose
    # re-crawl came back blocked is kept, and the record is reported failed in
    # refresh_status (never a silent success badge over destroyed data).
    run_id = "RUN-20260528-110012-f003"
    run_dir = _complete_run(run_store, run_id, [
        {"id": "T-A", "practice_name": "A", "target_tier": "Bullseye",
         "bullseye_score": 90, "exclusion_status": "CLEAR",
         "source_confidence": "partial",
         "signals": [{"signal_id": "S-1", "signal_state": "yes"}]},
    ])
    scratch = _scratch_with(run_dir, {
        "id": "T-A", "practice_name": "A", "target_tier": "Manual Review",
        "bullseye_score": 0, "exclusion_status": "CLEAR",
        "source_confidence": "failed", "signals": []})

    asyncio.run(runner._monitor_batch_reenrich(
        run_id, scratch, ["T-A"], _FakeProcess(returncode=0)))

    by_id = {r["id"]: r for r in
             json.loads((run_dir / "enriched_targets.json").read_text())["records"]}
    assert by_id["T-A"]["target_tier"] == "Bullseye"   # kept
    assert by_id["T-A"]["bullseye_score"] == 90
    refresh = runner.load_refresh_status(run_dir)
    assert refresh["T-A"]["state"] == "failed"


def test_merge_invalid_scratch_leaves_source_untouched(run_store):
    run_id = "RUN-20260528-110003-dddd"
    run_dir = _complete_run(run_store, run_id, [
        {"id": "T-A", "practice_name": "A", "target_tier": "Contender",
         "bullseye_score": 40, "exclusion_status": "CLEAR"}])
    before = (run_dir / "enriched_targets.json").read_text()
    scratch = run_dir / ".recrawl_bad"
    scratch.mkdir()
    (scratch / "enriched_targets.json").write_text("{ not valid json")

    runner._merge_recrawled_record(run_id, scratch, "T-A", "browser re-crawl")

    assert (run_dir / "enriched_targets.json").read_text() == before


def _complete_run_with_snapshots(run_store: Path, run_id: str, record: dict) -> Path:
    """Complete run dir that also carries the config + ICP snapshots a re-enrich needs."""
    run_dir = _complete_run(run_store, run_id, [record])
    (run_dir / config.PROJECT_CONFIG_SNAPSHOT_FILENAME).write_text(json.dumps(
        {"project_id": "p", "active_exclusion_rules": [], "bullseye_min_score": 75}))
    (run_dir / config.ICP_SNAPSHOT_FILENAME).write_text(json.dumps(
        {"icp_id": "t", "name": "T", "version": "v1",
         "signals": [{"signal_id": "S-1", "signal_label": "x",
                      "prompt_instruction": "y", "positive_weight": 10}]}))
    return run_dir


def test_manual_content_writes_one_scratch_file_and_flag_per_page(run_store, monkeypatch):
    run_id = "RUN-20260528-120000-aaaa"
    _complete_run_with_snapshots(run_store, run_id, {
        "id": "T-A", "practice_name": "A", "website_url": "https://a.com",
        "target_tier": "Contender", "bullseye_score": 0, "exclusion_status": "CLEAR"})

    captured = {}

    def _fake_spawn(*args, extra_flags=None, **kwargs):
        captured["extra_flags"] = extra_flags
        return _FakeProcess(returncode=0)

    async def _fake_update(*args, **kwargs):
        return None

    monkeypatch.setattr(runner, "spawn_pipeline", _fake_spawn)
    monkeypatch.setattr(runner, "_run_inplace_update", _fake_update)

    asyncio.run(runner.orchestrate_manual_content_recrawl(
        source_run_id=run_id,
        record_id="T-A",
        contents=[(b"<html><body>Home page content here.</body></html>", "home.html"),
                  (b"Plain about page text.", "about.txt"),
                  (b"   ", "blank.txt")],  # blank is dropped
        operator="tester",
    ))

    flags = captured["extra_flags"]
    # One --manual-content-path per non-empty page (blank dropped → 2 pages).
    assert flags.count("--manual-content-path") == 2
    paths = [flags[i + 1] for i, f in enumerate(flags) if f == "--manual-content-path"]
    assert all(Path(p).exists() for p in paths)
    assert paths[0].endswith(".html")   # HTML sniffed
    assert paths[1].endswith(".txt")    # plain text


def test_manual_content_rejects_all_empty(run_store):
    run_id = "RUN-20260528-120001-bbbb"
    _complete_run_with_snapshots(run_store, run_id, {
        "id": "T-A", "practice_name": "A", "website_url": "https://a.com",
        "target_tier": "Contender", "bullseye_score": 0, "exclusion_status": "CLEAR"})

    with pytest.raises(ValueError):
        asyncio.run(runner.orchestrate_manual_content_recrawl(
            source_run_id=run_id, record_id="T-A",
            contents=[(b"", "a.txt"), (b"   ", "b.txt")], operator="tester"))


def test_recompute_counts_from_records():
    recs = [
        {"target_tier": "Bullseye", "exclusion_status": "CLEAR", "enrichment_status": "complete"},
        {"target_tier": "Contender", "exclusion_status": "CLEAR", "enrichment_status": "complete"},
        {"target_tier": "Excluded", "exclusion_status": "EXCLUDED", "enrichment_status": "complete"},
        {"target_tier": "Needs Verification", "exclusion_status": "CLEAR", "enrichment_status": "failed"},
    ]
    counts = runner._recompute_counts_from_records(recs)
    assert counts["bullseye_count"] == 1
    assert counts["contender_count"] == 1
    assert counts["needs_verification_count"] == 1
    assert counts["excluded_count"] == 1
    assert counts["error_count"] == 1


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


def test_monitor_persists_stdout_tail_on_nonzero_exit(run_store):
    """A failed run's stdout is written to pipeline_stdout.log for diagnostics."""
    run_id = "RUN-20260528-100006-aaaa"
    run_dir = _make_run(run_store, run_id)

    asyncio.run(runner.monitor_pipeline(
        run_id,
        _FakeProcess(returncode=1, stderr=b"boom", stdout=b"STEP 4: signal extract\nprogress 5/10"),
    ))

    log = run_dir / runner.PIPELINE_STDOUT_FILENAME
    assert log.exists()
    assert "STEP 4: signal extract" in log.read_text(encoding="utf-8")


def test_monitor_no_stdout_log_on_success(run_store):
    """A clean run does not write a stdout log file."""
    run_id = "RUN-20260528-100007-bbbb"
    run_dir = _make_run(run_store, run_id)
    (run_dir / "enriched_targets.json").write_text(json.dumps(_VALID_ENRICHED))
    (run_dir / "run_log.json").write_text(json.dumps(_VALID_LOG))

    asyncio.run(runner.monitor_pipeline(
        run_id, _FakeProcess(returncode=0, stdout=b"all good")
    ))

    assert not (run_dir / runner.PIPELINE_STDOUT_FILENAME).exists()


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


# ---------------------------------------------------------------------------
# Evidence Vault: merge carries the scratch run's snapshot into the source run
# ---------------------------------------------------------------------------

def _scratch_with_evidence(run_dir: Path, record: dict, record_id: str) -> Path:
    scratch = _scratch_with(run_dir, record)
    evidence_dir = scratch / "evidence" / record_id
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "page-01.txt").write_text("Fresh page text.", encoding="utf-8")
    (evidence_dir / "index.json").write_text(json.dumps([
        {"url": "https://a.example", "file": "page-01.txt",
         "fetched_at": "2026-06-10T12:00:00+00:00", "sha256": "x", "chars": 16,
         "provenance": "crawl"},
    ]))
    return scratch


def test_merge_copies_scratch_evidence_into_source_run(run_store):
    run_id = "RUN-20260528-130000-aaaa"
    run_dir = _complete_run(run_store, run_id, [
        {"id": "T-A", "practice_name": "A", "target_tier": "Contender",
         "bullseye_score": 40, "exclusion_status": "CLEAR"},
    ])
    # Stale snapshot from the original crawl, to be replaced by the merge.
    stale = run_dir / "evidence" / "T-A"
    stale.mkdir(parents=True)
    (stale / "page-01.txt").write_text("Old page text.", encoding="utf-8")
    scratch = _scratch_with_evidence(run_dir, {
        "id": "T-A", "practice_name": "A", "target_tier": "Bullseye",
        "bullseye_score": 92, "exclusion_status": "CLEAR"}, "T-A")

    result = runner._merge_recrawled_record(run_id, scratch, "T-A", "browser re-crawl")

    assert result.ok
    copied = (run_dir / "evidence" / "T-A" / "page-01.txt").read_text(encoding="utf-8")
    assert copied == "Fresh page text."


def test_merge_without_scratch_evidence_keeps_source_snapshot(run_store):
    run_id = "RUN-20260528-140000-aaaa"
    run_dir = _complete_run(run_store, run_id, [
        {"id": "T-A", "practice_name": "A", "target_tier": "Contender",
         "bullseye_score": 40, "exclusion_status": "CLEAR"},
    ])
    stale = run_dir / "evidence" / "T-A"
    stale.mkdir(parents=True)
    (stale / "page-01.txt").write_text("Old page text.", encoding="utf-8")
    scratch = _scratch_with(run_dir, {
        "id": "T-A", "practice_name": "A", "target_tier": "Bullseye",
        "bullseye_score": 92, "exclusion_status": "CLEAR"})

    result = runner._merge_recrawled_record(run_id, scratch, "T-A", "browser re-crawl")

    assert result.ok
    # No new capture — the original snapshot survives untouched.
    assert (run_dir / "evidence" / "T-A" / "page-01.txt").read_text(encoding="utf-8") == "Old page text."


class TestRefreshStatusCorruption:
    """Damaged refresh_status.json is preserved as a sidecar, never merged with."""

    def test_corrupt_file_preserved_as_sidecar(self, tmp_path):
        run_dir = tmp_path / "RUN-20260722-130000"
        run_dir.mkdir()
        path = run_dir / "refresh_status.json"
        path.write_text("{ this is not json", encoding="utf-8")

        runner.mark_refresh_running(run_dir, ["T-1"], kind="recrawl")

        sidecar = run_dir / "refresh_status.json.corrupt"
        assert sidecar.exists()
        assert sidecar.read_text(encoding="utf-8") == "{ this is not json"
        fresh = json.loads(path.read_text(encoding="utf-8"))
        assert fresh["T-1"]["state"] == "running"

    def test_non_dict_root_read_returns_empty(self, tmp_path):
        run_dir = tmp_path / "RUN-20260722-130001"
        run_dir.mkdir()
        (run_dir / "refresh_status.json").write_text('["a", "list"]', encoding="utf-8")
        assert runner.load_refresh_status(run_dir) == {}

    def test_non_dict_entries_skipped_on_read(self, tmp_path):
        run_dir = tmp_path / "RUN-20260722-130002"
        run_dir.mkdir()
        (run_dir / "refresh_status.json").write_text(
            json.dumps({"T-1": {"state": "done"}, "T-2": "garbage"}), encoding="utf-8"
        )
        loaded = runner.load_refresh_status(run_dir)
        assert "T-1" in loaded
        assert "T-2" not in loaded
