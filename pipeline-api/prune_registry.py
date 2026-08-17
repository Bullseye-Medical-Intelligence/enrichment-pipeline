"""
prune_registry.py
One-time migration for the registry safety fix (docs/data-boundary-model.md,
Section H decision 2026-08-17: fix-only).

Phase 0 — back up master_practice_registry.json next to itself
          (master_practice_registry.backup-<utc-timestamp>.json).
Phase 4 — prune the per-client COMMERCIAL fields (current_tier,
          bullseye_score, exclusion_status, enrichment_status) from every
          entry, leaving identity, provenance, and change_history untouched.

Any two clients whose runs resolved to the same practice silently overwrote
each other's values in these four fields on every "Update Registry" click,
with no attribution and no history (C-1). The registry is identity-only from
now on; registry_update.py no longer writes these fields and strips them from
legacy entries on touch — this script removes them wholesale so no landmine
waits for the first registry-browsing feature.

Usage:
    python prune_registry.py [--registry-path PATH] [--preview]

--preview reports what would change without writing anything (no backup).
Idempotent: a second run finds nothing to prune and writes nothing.
Prints a JSON summary to stdout. Exit code 0 on success (including no-op).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import locking  # noqa: E402
from registry_update import (  # noqa: E402
    COMMERCIAL_ENTRY_FIELDS,
    RegistryLoadError,
    load_registry,
    registry_path,
    save_registry,
)


def prune_registry_commercial_fields(path: Path, preview: bool = False) -> dict:
    """Back up, then strip COMMERCIAL_ENTRY_FIELDS from every registry entry.

    Returns a summary dict. The backup is written only when a real prune is
    about to happen (never for --preview or a no-op), so repeated runs do not
    litter the directory with identical backups.
    """
    if not path.exists():
        return {"registry_path": str(path), "entries": 0, "pruned_entries": 0,
                "backup_path": "", "preview": preview,
                "message": "No registry file — nothing to prune."}

    with locking.file_lock(Path(str(path) + ".lock")):
        registry = load_registry(path)
        entries = registry.get("entries") or {}

        pruned_entries = 0
        pruned_fields = 0
        for entry in entries.values():
            hit = False
            for field in COMMERCIAL_ENTRY_FIELDS:
                if field in entry:
                    hit = True
                    pruned_fields += 1
                    if not preview:
                        del entry[field]
            if hit:
                pruned_entries += 1

        backup_path = ""
        if pruned_entries and not preview:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = path.with_name(f"{path.stem}.backup-{stamp}{path.suffix}")
            shutil.copy2(path, backup)  # Phase 0: reversible via this snapshot
            backup_path = str(backup)
            save_registry(registry, path)

    return {
        "registry_path": str(path),
        "entries": len(entries),
        "pruned_entries": pruned_entries,
        "pruned_fields": pruned_fields,
        "backup_path": backup_path,
        "preview": preview,
        "message": ("Nothing to prune — registry is already identity-only."
                    if not pruned_entries else
                    ("Preview only — nothing written." if preview else
                     "Pruned. Reversible via the backup file.")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Back up the master practice registry and prune per-client "
                    "commercial fields from every entry (identity-only registry)."
    )
    parser.add_argument(
        "--registry-path", default=None,
        help="Registry file to prune (default: the configured platform registry)",
    )
    parser.add_argument(
        "--preview", action="store_true",
        help="Report what would change without writing anything",
    )
    args = parser.parse_args()

    path = Path(args.registry_path) if args.registry_path else registry_path()
    try:
        summary = prune_registry_commercial_fields(path, preview=args.preview)
    except RegistryLoadError as exc:
        sys.exit(str(exc))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
