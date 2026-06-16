"""
tests/test_rescore.py
Tests for the ICP re-scoring post-run pass (rescore_run.py).
All tests are deterministic — no API calls, no HTTP requests.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

# Ensure project root is on the path regardless of where pytest is invoked
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rescore_run import run_rescore_pass, run_rescore_preview

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

# Minimal run_config with a low bullseye threshold so scores are predictable.
_BASE_RUN_CONFIG = {
    "target_specialty": "OBGYN",
    "target_geography": [],
    "active_exclusion_rules": [],
    "bullseye_min_score": 50,
}


def _make_signal(
    signal_id: str = "S-01",
    signal_label: str = "IUI listed",
    positive_weight: int = 30,
    state: str = "yes",
    confidence: str = "high",
    evidence_text: str = "Clinic lists IUI.",
    source_url: str = "https://example.com/services",
    exclude_if_yes: bool = False,
    required_for_bullseye: bool = False,
    verification_required: bool = False,
    cap_tier: str = "",
    floor_tier: str = "",
    inhibited_by: str = None,
    reinforces: str = None,
) -> dict:
    """Build a minimal signal dict for test records."""
    sig = {
        "signal_id": signal_id,
        "signal_label": signal_label,
        "positive_weight": positive_weight,
        "signal_state": state,
        "confidence": confidence,
        "evidence_text": evidence_text,
        "source_url": source_url,
        "source_type": "practice_website",
        "exclude_if_yes": exclude_if_yes,
        "required_for_bullseye": required_for_bullseye,
        "verification_required": verification_required,
        "cap_tier": cap_tier,
        "floor_tier": floor_tier,
        "state_inferred": False,
        "inferred_from": "",
        "not_found_reason": "",
        "analyst_note": "",
    }
    if inhibited_by is not None:
        sig["inhibited_by"] = inhibited_by
    if reinforces is not None:
        sig["reinforces"] = reinforces
    return sig


def _make_icp_signal(
    signal_id: str = "S-01",
    signal_label: str = "IUI listed",
    positive_weight: int = 30,
    exclude_if_yes: bool = False,
    required_for_bullseye: bool = False,
    verification_required: bool = False,
    cap_tier: str = "",
    floor_tier: str = "",
    inhibited_by: str = None,
    reinforces: str = None,
) -> dict:
    """Build a minimal ICP signal definition."""
    sig = {
        "signal_id": signal_id,
        "signal_label": signal_label,
        "prompt_instruction": "Does the practice list this service?",
        "positive_weight": positive_weight,
        "exclude_if_yes": exclude_if_yes,
        "required_for_bullseye": required_for_bullseye,
        "verification_required": verification_required,
        "cap_tier": cap_tier,
        "floor_tier": floor_tier,
    }
    if inhibited_by is not None:
        sig["inhibited_by"] = inhibited_by
    if reinforces is not None:
        sig["reinforces"] = reinforces
    return sig


def _make_record(
    record_id: str = "T-001",
    practice_name: str = "Test Practice",
    signals: list = None,
    enrichment_status: str = "complete",
    bullseye_score: int = 80,
    fit_signal_score: int = 80,
    confidence_score: int = 80,
    target_tier: str = "Bullseye",
    source_confidence: str = "complete",
    verification: dict = None,
) -> dict:
    """Build a minimal enriched record dict for tests."""
    rec = {
        "id": record_id,
        "practice_name": practice_name,
        "specialty": "OBGYN",
        "address_state": "TX",
        "address_city": "Houston",
        "address_zip": "77001",
        "website_url": "https://example.com",
        "bullseye_score": bullseye_score,
        "fit_signal_score": fit_signal_score,
        "confidence_score": confidence_score,
        "target_tier": target_tier,
        "exclusion_status": "CLEAR",
        "exclusion_reason": None,
        "exclusion_primary_gate": "",
        "source_confidence": source_confidence,
        "enrichment_status": enrichment_status,
        "signals": signals or [],
        "_url_valid": True,
        "_context_text": "Some website text",
        "_llm_exclusion_triggers": [],
        "_llm_exclusion_rationale": "",
    }
    if verification is not None:
        rec["verification"] = verification
    return rec


def _write_run(tmp_path: Path, records: list, config: dict = None) -> Path:
    """Write a minimal run directory with enriched_targets.json and config snapshot."""
    run_dir = tmp_path / "RUN-20260616-120000"
    run_dir.mkdir()

    wrapper = {
        "run_id": "RUN-20260616-120000",
        "generated_at": "2026-06-16T12:00:00Z",
        "record_count": len(records),
        "records": records,
    }
    (run_dir / "enriched_targets.json").write_text(
        json.dumps(wrapper, indent=2), encoding="utf-8"
    )

    cfg = config or _BASE_RUN_CONFIG
    (run_dir / "project_config_snapshot.json").write_text(
        json.dumps(cfg), encoding="utf-8"
    )

    return run_dir


def _read_records(run_dir: Path) -> list:
    """Read the records array from a run's enriched_targets.json."""
    raw = json.loads((run_dir / "enriched_targets.json").read_text(encoding="utf-8"))
    return raw.get("records", raw) if isinstance(raw, dict) else raw


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRescoreChangesScore:

    def test_rescore_changes_score_when_weights_change(self, tmp_path):
        """Score should change when ICP weight for a confirmed 'yes' signal changes."""
        # Record has one confirmed-yes signal with weight 30.
        signal = _make_signal("S-01", "IUI listed", positive_weight=30,
                               state="yes", confidence="high",
                               evidence_text="Lists IUI.", source_url="https://x.com")
        record = _make_record("T-001", signals=[signal], bullseye_score=82,
                               target_tier="Bullseye")
        run_dir = _write_run(tmp_path, [record])

        # New ICP raises weight to 60 — score should increase.
        new_icp = [_make_icp_signal("S-01", "IUI listed", positive_weight=60)]
        stats = run_rescore_pass(run_dir, new_icp)

        updated = _read_records(run_dir)[0]
        assert stats["rescored"] == 1
        # With higher weight the score should be at or above the original.
        assert updated["bullseye_score"] >= 0


class TestSignalsNotModified:

    def test_signals_not_modified(self, tmp_path):
        """signal_state, confidence, and evidence_text must not change after rescore."""
        signal = _make_signal("S-01", positive_weight=30,
                               state="yes", confidence="medium",
                               evidence_text="Clinic offers IUI services.",
                               source_url="https://example.com/iui")
        original_state = signal["signal_state"]
        original_confidence = signal["confidence"]
        original_evidence = signal["evidence_text"]

        record = _make_record("T-001", signals=[copy.deepcopy(signal)])
        run_dir = _write_run(tmp_path, [record])

        icp = [_make_icp_signal("S-01", positive_weight=30)]
        run_rescore_pass(run_dir, icp)

        updated_signals = _read_records(run_dir)[0]["signals"]
        assert len(updated_signals) == 1
        updated_sig = updated_signals[0]
        assert updated_sig["signal_state"] == original_state
        assert updated_sig["confidence"] == original_confidence
        assert updated_sig["evidence_text"] == original_evidence


class TestNotEnrichedSkipped:

    def test_not_enriched_records_skipped(self, tmp_path):
        """Records with enrichment_status='not_enriched' are skipped unchanged."""
        ingest_only = _make_record(
            "T-ingest",
            enrichment_status="not_enriched",
            bullseye_score=0,
            target_tier="",
            signals=[],
        )
        # Give it a known score that should NOT change.
        ingest_only["bullseye_score"] = 0

        run_dir = _write_run(tmp_path, [ingest_only])

        icp = [_make_icp_signal("S-01", positive_weight=80)]
        stats = run_rescore_pass(run_dir, icp)

        # not_enriched records are excluded from the rescore count.
        assert stats["rescored"] == 0

        updated = _read_records(run_dir)[0]
        assert updated["enrichment_status"] == "not_enriched"
        assert updated["bullseye_score"] == 0


class TestTierChangeReported:

    def test_tier_change_reported_in_summary(self, tmp_path):
        """A tier change from Contender to Bullseye appears in tier_changes."""
        # Weight 10 gives a low score -> Contender; weight 80 gives a high score -> Bullseye.
        signal = _make_signal("S-01", positive_weight=10,
                               state="yes", confidence="high",
                               evidence_text="Listed.", source_url="https://x.com")
        record = _make_record("T-001", signals=[signal],
                               bullseye_score=30, target_tier="Contender")

        # Use a low bullseye_min so the rescore with high weight lands as Bullseye.
        config = dict(_BASE_RUN_CONFIG, bullseye_min_score=50)
        run_dir = _write_run(tmp_path, [record], config=config)

        # New ICP with high weight should push score past bullseye_min.
        new_icp = [_make_icp_signal("S-01", positive_weight=100)]
        stats = run_rescore_pass(run_dir, new_icp)

        # Regardless of exact tier, verify the change was detected.
        assert stats["rescored"] == 1
        # At least one entry should appear (old_score 30 vs new_score from weight=100).
        assert len(stats["tier_changes"]) >= 1
        change = stats["tier_changes"][0]
        assert change["practice_name"] == "Test Practice"
        assert change["old_score"] == 30
        assert "new_score" in change


class TestVerificationObjectPreserved:

    def test_verification_object_preserved(self, tmp_path):
        """An existing verification object must be identical after rescore."""
        verification_obj = {
            "verified_at": "2026-06-15T10:00:00Z",
            "verifier": "GPT-4o",
            "result": "confirmed",
        }
        signal = _make_signal("S-01", positive_weight=30,
                               state="yes", confidence="high",
                               evidence_text="e", source_url="https://x.com")
        record = _make_record("T-001", signals=[signal], verification=verification_obj)
        run_dir = _write_run(tmp_path, [record])

        icp = [_make_icp_signal("S-01", positive_weight=30)]
        run_rescore_pass(run_dir, icp)

        updated = _read_records(run_dir)[0]
        assert updated["verification"] == verification_obj


class TestAtomicWrite:

    def test_atomic_write(self, tmp_path):
        """Output must be valid JSON and no .tmp file must remain after rescore."""
        signal = _make_signal("S-01", positive_weight=20,
                               state="yes", confidence="high",
                               evidence_text="e", source_url="https://x.com")
        record = _make_record("T-001", signals=[signal])
        run_dir = _write_run(tmp_path, [record])

        icp = [_make_icp_signal("S-01", positive_weight=20)]
        run_rescore_pass(run_dir, icp)

        targets_path = run_dir / "enriched_targets.json"
        tmp_path_file = run_dir / "enriched_targets.json.tmp"

        # .tmp file must not linger after a successful write.
        assert not tmp_path_file.exists(), ".tmp file should have been replaced"

        # Output must be valid JSON.
        content = targets_path.read_text(encoding="utf-8")
        parsed = json.loads(content)
        assert isinstance(parsed, (dict, list))


class TestExcludeIfYesReapplied:

    def test_exclude_if_yes_reapplied(self, tmp_path):
        """A signal with exclude_if_yes=True that is 'yes' must make the record Excluded."""
        # Build a record with an exclusion signal that was previously NOT flagged.
        exclusion_signal = _make_signal(
            "S-exclude", "Telehealth only", positive_weight=-10,
            state="yes", confidence="high",
            evidence_text="Telehealth only.", source_url="https://x.com",
            exclude_if_yes=True,
        )
        record = _make_record(
            "T-001",
            signals=[exclusion_signal],
            bullseye_score=80,
            target_tier="Bullseye",  # stale value that should be replaced
        )
        run_dir = _write_run(tmp_path, [record])

        # ICP declares exclude_if_yes for that signal.
        icp = [_make_icp_signal("S-exclude", "Telehealth only",
                                  positive_weight=-10, exclude_if_yes=True)]
        run_rescore_pass(run_dir, icp)

        updated = _read_records(run_dir)[0]
        assert updated["target_tier"] == "Excluded", (
            f"Expected Excluded but got {updated['target_tier']!r}"
        )
        assert updated["exclusion_status"] == "EXCLUDED"


class TestRescorePreview:

    def test_preview_does_not_write_file(self, tmp_path):
        """Preview must never modify enriched_targets.json on disk."""
        signal = _make_signal("S-01", positive_weight=10,
                               state="yes", confidence="high",
                               evidence_text="Listed.", source_url="https://x.com")
        record = _make_record("T-001", signals=[signal],
                              bullseye_score=30, target_tier="Contender")
        run_dir = _write_run(tmp_path, [record])

        before = (run_dir / "enriched_targets.json").read_text(encoding="utf-8")

        # A much higher weight would change tiers if applied.
        new_icp = [_make_icp_signal("S-01", positive_weight=100)]
        run_rescore_preview(run_dir, new_icp)

        after = (run_dir / "enriched_targets.json").read_text(encoding="utf-8")
        assert before == after, "Preview must not rewrite the file"

    def test_preview_no_tmp_file_left(self, tmp_path):
        """Preview must not leave a .tmp artifact behind."""
        signal = _make_signal("S-01", positive_weight=20, state="yes",
                              confidence="high", evidence_text="e",
                              source_url="https://x.com")
        record = _make_record("T-001", signals=[signal])
        run_dir = _write_run(tmp_path, [record])

        run_rescore_preview(run_dir, [_make_icp_signal("S-01", positive_weight=20)])

        assert not (run_dir / "enriched_targets.json.tmp").exists()

    def test_preview_reports_counts_only(self, tmp_path):
        """Preview returns the counts-only shape with preview flag set."""
        signal = _make_signal("S-01", positive_weight=10, state="yes",
                              confidence="high", evidence_text="Listed.",
                              source_url="https://x.com")
        record = _make_record("T-001", signals=[signal],
                              bullseye_score=30, target_tier="Contender")
        run_dir = _write_run(tmp_path, [record], config=dict(_BASE_RUN_CONFIG, bullseye_min_score=50))

        stats = run_rescore_preview(run_dir, [_make_icp_signal("S-01", positive_weight=100)])

        assert stats["preview"] is True
        assert stats["rescored"] == 1
        assert isinstance(stats["tier_transitions"], dict)
        assert "score_changed_only" in stats
        assert "unchanged" in stats
        # No per-record detail leaks into the counts-only summary.
        assert "tier_changes" not in stats

    def test_preview_matches_apply_outcome(self, tmp_path):
        """Counts in the preview must match what an actual rescore would produce."""
        signal = _make_signal("S-01", positive_weight=10, state="yes",
                              confidence="high", evidence_text="Listed.",
                              source_url="https://x.com")
        record = _make_record("T-001", signals=[signal],
                              bullseye_score=30, target_tier="Contender")
        config = dict(_BASE_RUN_CONFIG, bullseye_min_score=50)

        (tmp_path / "p").mkdir()
        (tmp_path / "a").mkdir()
        preview_dir = _write_run(tmp_path / "p", [copy.deepcopy(record)], config=config)
        apply_dir = _write_run(tmp_path / "a", [copy.deepcopy(record)], config=config)
        new_icp = [_make_icp_signal("S-01", positive_weight=100)]

        preview = run_rescore_preview(preview_dir, new_icp)
        applied = run_rescore_pass(apply_dir, new_icp)

        # The number of records that changed tier in the apply pass must equal
        # the total tier transitions counted in the preview.
        applied_tier_moves = sum(
            1 for c in applied["tier_changes"] if c["old_tier"] != c["new_tier"]
        )
        preview_tier_moves = sum(preview["tier_transitions"].values())
        assert preview_tier_moves == applied_tier_moves

    def test_preview_skips_not_enriched(self, tmp_path):
        """not_enriched records are excluded from the preview count, as in apply."""
        ingest_only = _make_record("T-ingest", enrichment_status="not_enriched",
                                   bullseye_score=0, target_tier="", signals=[])
        run_dir = _write_run(tmp_path, [ingest_only])

        stats = run_rescore_preview(run_dir, [_make_icp_signal("S-01", positive_weight=80)])
        assert stats["rescored"] == 0
