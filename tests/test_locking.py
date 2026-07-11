"""
tests/test_locking.py

Deterministic concurrency tests for the durable-state locks.

No timing-dependent sleeps: threads synchronize on barriers/events so the
contention is guaranteed, and every assertion is a property that mutual
exclusion makes certain (all updates survive; the cap is never exceeded)
regardless of scheduling.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import locking  # noqa: E402
import registry_update  # noqa: E402
import reviews  # noqa: E402
import runner  # noqa: E402
import runs  # noqa: E402
from schema import ReviewEdit  # noqa: E402

_N_THREADS = 8


def _run_threads(n: int, fn) -> list:
    """Run fn(i) on n threads, all released together by a barrier."""
    barrier = threading.Barrier(n)
    results = []

    def _work(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(_work, i) for i in range(n)]
        for f in futures:
            results.append(f.result())
    return results


# ---------------------------------------------------------------------------
# file_lock primitive
# ---------------------------------------------------------------------------

def test_file_lock_mutual_exclusion_across_threads(tmp_path):
    """With the lock, read-increment-write on a shared file loses no updates.
    (This is the exact RMW shape all four state stores use.)"""
    counter = tmp_path / "counter.json"
    counter.write_text("0")
    lock = tmp_path / "counter.lock"

    def _increment(_):
        with locking.file_lock(lock):
            value = int(counter.read_text())
            counter.write_text(str(value + 1))

    _run_threads(_N_THREADS, _increment)
    assert int(counter.read_text()) == _N_THREADS


def test_file_lock_timeout_raises_clear_error(tmp_path):
    lock = tmp_path / "x.lock"
    holder_has_lock = threading.Event()
    release = threading.Event()

    def _hold():
        with locking.file_lock(lock):
            holder_has_lock.set()
            release.wait(timeout=30)

    t = threading.Thread(target=_hold)
    t.start()
    try:
        assert holder_has_lock.wait(timeout=30)
        with pytest.raises(locking.LockTimeout) as exc:
            with locking.file_lock(lock, timeout=0.1):
                pass
        # Operator-facing: names the lock and says nothing was changed.
        assert "Nothing was changed" in str(exc.value)
    finally:
        release.set()
        t.join()


def test_file_lock_released_on_exception(tmp_path):
    lock = tmp_path / "x.lock"
    with pytest.raises(RuntimeError):
        with locking.file_lock(lock):
            raise RuntimeError("boom")
    # Immediately reacquirable — the lock did not leak.
    with locking.file_lock(lock, timeout=0.5):
        pass


# ---------------------------------------------------------------------------
# reviews.json: concurrent edits to different records must all survive
# ---------------------------------------------------------------------------

def test_concurrent_review_saves_lose_nothing(tmp_path):
    edit = ReviewEdit(analyst_note="note", override_tier=None,
                      override_reason=None, qc_status="approved")

    def _save(i):
        reviews.save_review("RUN-X", f"T-{i}", edit, f"user{i}", tmp_path)

    _run_threads(_N_THREADS, _save)

    saved = json.loads((tmp_path / "reviews.json").read_text())
    assert sorted(saved) == sorted(f"T-{i}" for i in range(_N_THREADS))


def test_concurrent_stamp_and_save_lose_nothing(tmp_path):
    """The two writers that collide in production: a background re-enrich stamp
    and an operator's review save."""
    edit = ReviewEdit(analyst_note="operator note", override_tier=None,
                      override_reason=None, qc_status="approved")

    def _write(i):
        if i % 2 == 0:
            reviews.stamp_reenriched("RUN-X", f"S-{i}", tmp_path, "browser re-crawl")
        else:
            reviews.save_review("RUN-X", f"R-{i}", edit, "ana", tmp_path)

    _run_threads(_N_THREADS, _write)

    saved = json.loads((tmp_path / "reviews.json").read_text())
    assert len(saved) == _N_THREADS


# ---------------------------------------------------------------------------
# refresh_status.json: concurrent jobs must not drop each other's entries
# ---------------------------------------------------------------------------

def test_concurrent_refresh_marks_lose_nothing(tmp_path):
    def _mark(i):
        runner.mark_refresh_running(tmp_path, [f"T-{i}"], "re-enrich")

    _run_threads(_N_THREADS, _mark)

    status = json.loads((tmp_path / "refresh_status.json").read_text())
    assert sorted(status) == sorted(f"T-{i}" for i in range(_N_THREADS))
    assert all(v["state"] == "running" for v in status.values())


# ---------------------------------------------------------------------------
# status.json: a threadpool writer and other writers must not drop fields
# ---------------------------------------------------------------------------

def test_concurrent_status_updates_lose_no_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path)
    run_id = "RUN-20260711-100000-aaaa"
    runs.create_run(run_id, "P-1", "manual", "in.csv", "tester", 5)

    # Each thread writes a DIFFERENT field — the registry stamp vs. monitor
    # counts shape. Without the lock the whole-file rewrite drops the loser's.
    fields = [
        {"bullseye_count": 3}, {"contender_count": 4},
        {"needs_verification_count": 2}, {"manual_review_count": 1},
        {"excluded_count": 6}, {"error_count": 0},
        {"registry_updated_at": "2026-07-11T10:00:00+00:00"},
        {"registry_update_count": 9},
    ]

    def _update(i):
        runs.update_run_status(run_id, **fields[i])

    _run_threads(len(fields), _update)

    final = runs.get_run(run_id)
    for f in fields:
        for key, value in f.items():
            assert getattr(final, key) == value, f"lost field {key}"


# ---------------------------------------------------------------------------
# master_practice_registry.json: two registry updates must both survive
# ---------------------------------------------------------------------------

def _registry_run(runs_dir: Path, run_id: str, rid: str, name: str, domain: str):
    rd = runs_dir / run_id
    rd.mkdir(parents=True)
    (rd / "status.json").write_text(json.dumps({
        "run_id": run_id, "project_id": "p", "source_type": "outscraper",
        "input_filename": "in.csv", "status": "complete", "operator": "t",
        "created_at": "2026-07-11T10:00:00+00:00", "run_type": "enrichment",
    }))
    (rd / "enriched_targets.json").write_text(json.dumps({"run_id": run_id, "records": [{
        "id": rid, "practice_name": name, "specialty": "OBGYN",
        "website_url": f"https://{domain}", "phone": "",
        "address_city": "Austin", "address_state": "TX", "address_zip": "78701",
        "bullseye_score": 90, "exclusion_status": "CLEAR",
        "target_tier": "Bullseye", "enrichment_status": "complete",
    }]}))


def test_concurrent_registry_updates_lose_nothing(tmp_path, monkeypatch):
    """The presently-reachable production race: update_registry_from_run runs in
    FastAPI's threadpool; two concurrent updates must both land in the registry."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", runs_dir)

    ids = ["RUN-20260711-100001-aaaa", "RUN-20260711-100002-bbbb"]
    _registry_run(runs_dir, ids[0], "T-A", "Alpha Clinic", "alpha.example.com")
    _registry_run(runs_dir, ids[1], "T-B", "Beta Clinic", "beta.example.com")

    def _apply(i):
        return registry_update.update_registry_from_run(
            ids[i], selection_mode="bullseye_only")

    results = _run_threads(2, _apply)

    assert all(r["registry_update_count"] == 1 for r in results)
    registry = json.loads(registry_update.registry_path().read_text())
    names = sorted(e["practice_name"] for e in registry["entries"].values())
    assert names == ["Alpha Clinic", "Beta Clinic"]  # neither insert was lost
    # Both runs' status stamps survived too (per-run files, per-run locks).
    for run_id in ids:
        assert runs.get_run(run_id).registry_updated_at


# ---------------------------------------------------------------------------
# MAX_CONCURRENT_RUNS admission: concurrent starts never exceed the cap
# ---------------------------------------------------------------------------

class _FakeUpload:
    filename = "upload.csv"


def test_concurrent_prepare_run_respects_cap(tmp_path, monkeypatch):
    """Two uploads suspended together inside CSV validation (the old race
    window: the cap was checked BEFORE the await) — exactly one may register."""
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path)
    monkeypatch.setattr(runner, "OUTPUT_RUNS_PATH", tmp_path)
    monkeypatch.setattr(runner, "MAX_CONCURRENT_RUNS", 1)
    monkeypatch.setattr(runner.projects, "get_project",
                        lambda pid: {"client_name": "C", "icp_profile_id": "icp"})
    monkeypatch.setattr(runner.projects, "validate_project_config", lambda cfg: None)
    monkeypatch.setattr(runner.projects, "suppression_list_path",
                        lambda pid: tmp_path / "no_suppression.csv")
    monkeypatch.setattr(runner.icp_profiles, "sync_seed_profile", lambda i: None)
    monkeypatch.setattr(runner.icp_profiles, "get_icp_profile",
                        lambda i: {"name": "ICP", "version": "v1", "signals": []})

    import validator
    arrived = 0
    both_inside = asyncio.Event()

    async def _gated_validate(file, source_type, project_id):
        # Deterministic interleaving: both coroutines must be suspended here —
        # past the point where the cap used to be checked — before either
        # proceeds to the admission section.
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_inside.set()
        await both_inside.wait()
        return b"practice_name\nA\n", 1

    monkeypatch.setattr(validator, "validate_csv_upload", _gated_validate)

    async def _race():
        return await asyncio.gather(
            runner._prepare_run(_FakeUpload(), "manual", "P-1", "op"),
            runner._prepare_run(_FakeUpload(), "manual", "P-1", "op"),
            return_exceptions=True,
        )

    results = asyncio.run(_race())

    winners = [r for r in results if isinstance(r, tuple)]
    losers = [r for r in results if isinstance(r, ValueError)]
    assert len(winners) == 1, f"cap exceeded: {results}"
    assert len(losers) == 1 and "Too many runs" in str(losers[0])
    assert runs.count_active_runs() == 1
    # The refused upload's partially-built run dir was cleaned up.
    run_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
