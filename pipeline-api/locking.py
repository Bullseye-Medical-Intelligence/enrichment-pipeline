"""
locking.py
Advisory file locks for the API's durable JSON state.

Every state file (status.json, reviews.json, refresh_status.json,
master_practice_registry.json) is written atomically (temp file + os.replace),
which protects against torn files but NOT against lost updates: two
read-modify-write sequences that interleave silently drop the loser's changes.
This module serializes those sequences with the smallest mechanism compatible
with the filesystem-JSON architecture: an OS advisory lock on a sibling
.lock file.

Scope of the guarantee:
  - WITHIN one host it serializes across threads (FastAPI's threadpool for
    sync-def routes and run_in_threadpool work), across asyncio tasks, and
    across multiple server processes (multi-worker uvicorn), because each
    acquisition opens a FRESH file descriptor — flock is per open file
    description, so two threads in one process contend just like two processes.
  - ACROSS hosts there is no guarantee: advisory locks do not travel over
    rclone/Drive sync, and NFS advisory-lock semantics vary. The deployment
    remains single-host by design (see pipeline-api/CLAUDE.md).

Rules for callers:
  - Lock only the state transaction (read -> mutate -> atomic write). Never
    hold a lock across an LLM call, a crawl, process.communicate, or any
    subprocess work.
  - Locks do not nest reentrantly (a second acquisition of the same lock file
    in the same thread deadlocks until timeout). Structure code as sequential
    independent critical sections instead of nesting.
  - When two different locks must be held together, acquire in this order and
    release in reverse: admission -> registry -> per-run. Never acquire a
    lower-ordered lock while holding a higher-ordered one.

Lock files:
  - per-run:   <run_dir>/.run.lock          (status.json, reviews.json,
                                             refresh_status.json,
                                             enriched_targets.json merges)
  - registry:  <master_practice_registry.json>.lock
  - admission: <OUTPUT_RUNS_PATH>/.admission.lock  (MAX_CONCURRENT_RUNS check)
  - post-run job: <run_dir>/.postrun.lock   (ui.py post-run triggers)

Job locks are the one exception to the "never hold across subprocess work"
rule: ui.py's post-run triggers hold .postrun.lock for the full CLI pass so a
double submit gets a fast 409 instead of racing. Only post-run triggers
contend for that file, so nothing else can starve on it. The repo-root CLIs
additionally acquire .run.lock themselves for their final compare-and-replace
write (see output/atomic_write.py — its lock filename must stay in sync with
run_lock_path here).

Windows is supported (start-bemi.bat): msvcrt.locking is used where fcntl is
unavailable. Timeouts raise LockTimeout with an operator-facing message; the
default of a few seconds is generous — contended sections are pure local JSON
I/O measured in milliseconds.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

try:  # POSIX
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

except ImportError:  # Windows
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


DEFAULT_TIMEOUT_SECONDS = 10.0
_POLL_SECONDS = 0.02


class LockTimeout(RuntimeError):
    """Could not acquire a state lock within the timeout.

    str() is operator-facing: it names the lock and the safe outcome (nothing
    was written) so a 503/flash message can surface it directly.
    """

    def __init__(self, lock_path: Path, timeout: float):
        self.lock_path = lock_path
        super().__init__(
            f"Another operation is still updating this state "
            f"(lock {lock_path.name} busy for over {timeout:.0f}s). "
            "Nothing was changed — retry in a moment; if this persists, check "
            "for a stuck request or restart the API."
        )


@contextmanager
def file_lock(lock_path: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS):
    """Hold an exclusive advisory lock on lock_path for the duration of the block.

    Opens a fresh descriptor per acquisition (required: flock is per open file
    description — a shared fd would let two threads in one process both
    "hold" the lock). Polls non-blocking until timeout, then raises
    LockTimeout. The lock file itself is an empty sibling artifact and is left
    in place (deleting it would race other waiters).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise LockTimeout(lock_path, timeout)
            time.sleep(_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def run_lock_path(run_directory: Path) -> Path:
    """The per-run lock file guarding a run's mutable JSON state."""
    return run_directory / ".run.lock"


@contextmanager
def run_lock(run_directory: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS):
    """Per-run state lock: status.json, reviews.json, refresh_status.json, and
    in-place enriched_targets.json merges for one run share this lock."""
    with file_lock(run_lock_path(run_directory), timeout=timeout):
        yield
