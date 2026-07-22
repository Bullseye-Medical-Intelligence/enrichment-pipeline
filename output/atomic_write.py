"""
atomic_write.py
Crash-safe file writes for pipeline output. Writes to a temp file in the
target directory then os.replace()s it into place, so a process crash
mid-write can never leave a truncated enriched_targets.json on disk.

Also provides the cross-process run-state guard for post-run CLI passes:
the API (pipeline-api/locking.py) serializes its read-modify-write sections
on <run_dir>/.run.lock, and the repo-root CLIs (rescore_run, reextract_run,
suppress_run, recrawl_run, verify_run) rewrite enriched_targets.json
wholesale. A pass that loaded the file minutes ago must not clobber a merge
that landed in between, so the CLI's final write happens under the same lock
file with a fingerprint compare — if the file changed since the pass loaded
it, the write is refused and nothing is lost. The lock filename must stay
identical to pipeline-api/locking.py's run_lock_path().
"""

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

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


RUN_LOCK_FILENAME = ".run.lock"  # must match pipeline-api/locking.py run_lock_path()
_LOCK_POLL_SECONDS = 0.02
_LOCK_TIMEOUT_SECONDS = 10.0


class ConcurrentRunChange(RuntimeError):
    """enriched_targets.json changed on disk while a post-run pass was running."""


def stat_fingerprint(path: Path) -> Optional[tuple]:
    """Identity of the file version currently on disk: (inode, mtime_ns, size).

    Returns None when the file does not exist. os.replace() always produces a
    new inode, so any atomic rewrite changes the fingerprint. Capture the
    fingerprint BEFORE opening the file for load — a replace that lands
    mid-load then fails the final compare instead of slipping through.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


@contextmanager
def run_state_lock(run_directory: Path, timeout: float = _LOCK_TIMEOUT_SECONDS):
    """Hold the run's state lock (<run_dir>/.run.lock) for the block.

    Fresh descriptor per acquisition (flock is per open file description).
    Raises ConcurrentRunChange on timeout — a held lock means the API is
    mid-transaction on this run and the pass should not write.
    """
    lock_path = Path(run_directory) / RUN_LOCK_FILENAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise ConcurrentRunChange(
                    f"Run state is locked by another operation "
                    f"({lock_path.name} busy for over {timeout:.0f}s). "
                    "Nothing was written — retry when the other operation finishes."
                )
            time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def guarded_replace(
    run_directory: Path,
    targets_path: Path,
    tmp_path: Path,
    loaded_fingerprint: Optional[tuple],
) -> None:
    """os.replace(tmp_path -> targets_path) only if the file is unchanged.

    Under the run-state lock, compares the current fingerprint against the one
    captured when the pass loaded the file. On mismatch the tmp file is
    removed and ConcurrentRunChange is raised — the concurrent writer's data
    wins and the pass reports it wrote nothing.
    """
    with run_state_lock(run_directory):
        if stat_fingerprint(targets_path) != loaded_fingerprint:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise ConcurrentRunChange(
                f"{Path(targets_path).name} changed while this pass was running "
                "(a concurrent merge or another pass wrote it). Nothing was "
                "written — re-run the pass against the current data."
            )
        os.replace(tmp_path, targets_path)


def atomic_write(path: Path, write_fn: Callable[[object], None], *, newline: str = "") -> None:
    """Write to `path` atomically via a temp file + os.replace().

    write_fn receives an open text file handle and is responsible for
    writing the full contents. The temp file shares `path`'s directory so
    os.replace() stays on one filesystem (a true atomic rename).
    """
    path = Path(path)
    directory = path.parent
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            write_fn(f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
