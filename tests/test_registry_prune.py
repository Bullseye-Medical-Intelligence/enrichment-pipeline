"""
tests/test_registry_prune.py
Tests for prune_registry.py — the one-time Phase 0 (backup) + Phase 4
(identity-only prune) migration from docs/data-boundary-model.md, decided
fix-only in Section H (2026-08-17).

Deterministic: filesystem only, no network, no subprocess.
"""

import json
import os
import sys
from pathlib import Path

_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

from prune_registry import prune_registry_commercial_fields  # noqa: E402


def _legacy_registry(tmp_path: Path) -> Path:
    path = tmp_path / "master_practice_registry.json"
    path.write_text(json.dumps({
        "version": "1", "updated_at": "2026-06-01T00:00:00+00:00", "entry_count": 2,
        "entries": {
            "e1": {
                "practice_registry_id": "e1", "practice_name": "Alpha",
                "website_domain": "alpha.com", "phone_digits": "4045551000",
                "current_tier": "Bullseye", "bullseye_score": 92,
                "exclusion_status": "CLEAR", "enrichment_status": "complete",
                "change_history": [{"field": "phone", "old": "", "new": "x",
                                    "changed_at": "t", "enrichment_run_id": "R"}],
            },
            "e2": {
                "practice_registry_id": "e2", "practice_name": "Beta",
                "website_domain": "beta.com", "phone_digits": "4045552000",
                # Already identity-only.
                "change_history": [],
            },
        },
    }), encoding="utf-8")
    return path


def _backups(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob("master_practice_registry.backup-*.json"))


def test_prune_strips_commercial_fields_and_backs_up(tmp_path):
    path = _legacy_registry(tmp_path)
    original_bytes = path.read_bytes()

    summary = prune_registry_commercial_fields(path)

    assert summary["pruned_entries"] == 1
    assert summary["pruned_fields"] == 4
    backups = _backups(tmp_path)
    assert len(backups) == 1                              # Phase 0 snapshot
    assert backups[0].read_bytes() == original_bytes      # byte-identical
    assert summary["backup_path"] == str(backups[0])

    pruned = json.loads(path.read_text(encoding="utf-8"))
    e1 = pruned["entries"]["e1"]
    for field in ("current_tier", "bullseye_score",
                  "exclusion_status", "enrichment_status"):
        assert field not in e1, field
    # Identity + history untouched.
    assert e1["practice_name"] == "Alpha"
    assert e1["change_history"][0]["enrichment_run_id"] == "R"
    assert pruned["entries"]["e2"]["practice_name"] == "Beta"


def test_prune_is_idempotent_no_second_backup(tmp_path):
    path = _legacy_registry(tmp_path)
    prune_registry_commercial_fields(path)
    summary = prune_registry_commercial_fields(path)
    assert summary["pruned_entries"] == 0
    assert "already identity-only" in summary["message"]
    assert len(_backups(tmp_path)) == 1  # no backup litter on re-run


def test_preview_writes_nothing(tmp_path):
    path = _legacy_registry(tmp_path)
    before = path.read_bytes()
    summary = prune_registry_commercial_fields(path, preview=True)
    assert summary["preview"] is True
    assert summary["pruned_entries"] == 1     # reported
    assert path.read_bytes() == before        # not written
    assert not _backups(tmp_path)             # no backup for a preview


def test_missing_registry_is_clean_noop(tmp_path):
    summary = prune_registry_commercial_fields(tmp_path / "nope.json")
    assert summary["pruned_entries"] == 0
    assert "nothing to prune" in summary["message"].lower()
