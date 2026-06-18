"""
registry.py
Master practice registry — load, save, and empty-state construction.

The registry is a single JSON file (master_practice_registry.json) stored
alongside the runs/ directory.  All writes are atomic (tmp + os.replace).
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REGISTRY_VERSION = "1"


class RegistryLoadError(Exception):
    """Raised when an existing registry file is present but cannot be read.

    A missing file is a valid bootstrap state. A present-but-corrupt file is not:
    silently treating it as empty would reclassify every known practice as NEW,
    so discovery must abort loudly instead.
    """


def empty_registry() -> dict:
    """Return a blank, valid registry structure."""
    return {
        "version": REGISTRY_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "entry_count": 0,
        "entries": {},
    }


def load_registry(path: Path) -> dict:
    """Load the registry from *path*.

    A missing file returns an empty registry (valid bootstrap). A present-but-
    unreadable file raises RegistryLoadError — never silently emptied, or every
    known practice would be reclassified as NEW.
    """
    if not path.exists():
        return empty_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Registry at %s exists but is unreadable: %s", path, exc)
        raise RegistryLoadError(
            f"Registry file at {path} exists but could not be read ({exc}). "
            "Discovery aborted — fix or restore the registry before retrying."
        ) from exc
    if not isinstance(data, dict):
        raise RegistryLoadError(
            f"Registry file at {path} is not a valid registry object. "
            "Discovery aborted — fix or restore the registry before retrying."
        )
    if not isinstance(data.get("entries"), dict):
        logger.warning("Registry at %s has no 'entries' dict — treating as empty", path)
        data["entries"] = {}
    return data


def save_registry(registry: dict, path: Path) -> None:
    """Atomically write *registry* to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    registry["updated_at"] = datetime.now(timezone.utc).isoformat()
    registry["entry_count"] = len(registry.get("entries") or {})
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
