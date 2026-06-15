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


def empty_registry() -> dict:
    """Return a blank, valid registry structure."""
    return {
        "version": REGISTRY_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "entry_count": 0,
        "entries": {},
    }


def load_registry(path: Path) -> dict:
    """Load the registry from *path*; return an empty registry if absent or corrupt."""
    if not path.exists():
        return empty_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get("entries"), dict):
            logger.warning("Registry at %s has no 'entries' dict — treating as empty", path)
            data["entries"] = {}
        return data
    except Exception:
        logger.exception("Failed to load registry from %s — returning empty", path)
        return empty_registry()


def save_registry(registry: dict, path: Path) -> None:
    """Atomically write *registry* to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    registry["updated_at"] = datetime.now(timezone.utc).isoformat()
    registry["entry_count"] = len(registry.get("entries") or {})
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
