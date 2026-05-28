"""
atomic_write.py
Crash-safe file writes for pipeline output. Writes to a temp file in the
target directory then os.replace()s it into place, so a process crash
mid-write can never leave a truncated enriched_targets.json on disk.
"""

import os
import tempfile
from pathlib import Path
from typing import Callable


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
