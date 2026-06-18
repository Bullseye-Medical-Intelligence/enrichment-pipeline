"""
tests/test_reextract.py
Tests for reextract_run.py. No real LLM calls — extract_signals is monkeypatched.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from reextract_run import (
    _is_eligible,
    _load_icp_data,
    _load_run_config,
    run_reextract_pass,
    run_reextract_preview,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_record(
    enrichment_status="enriched",
    context="This practice offers IUI and ultrasound.",
    target_tier="Contender",
    bullseye_score=55,
    practice_name="Test Clinic",
) -> dict:
    return {
        "record_id": "rec-001",
        "practice_name": practice_name,
        "enrichment_status": enrichment_status,
        "_context_text": context,
        "target_tier": target_tier,
        "bullseye_score": bullseye_score,
        "exclusion_status": "CLEAR",
        "exclusion_reason": "",
        "exclusion_primary_gate": "",
        "enrichment_run_id": "RUN-20260101-120000",
        "signals": [],
    }


def _write_targets(run_dir, records):
    data = {"run_id": "RUN-20260101-120000", "records": records}
    (run_dir / "enriched_targets.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def _fake_extract(record, icp_signals, context_text, run_id, **kwargs):
    """Monkeypatch stub for extract_signals — sets a known signal state without Claude."""
    record["signals"] = [
        {
            "signal_id": "S-01",
            "signal_label": "Performs IUI",
            "signal_state": "yes",
            "confidence": "high",
            "evidence_text": "offers IUI",
            "source_url": "",
            "state_inferred": False,
            "inferred_from": "",
            "not_found_reason": "",
        }
    ]
    record["bullseye_score"] = 80
    record["fit_signal_score"] = 80
    record["confidence_score"] = 80
    record["fit_confidence_status"] = "strong"
    record["enrichment_status"] = "enriched"
    record["source_confidence"] = "good"
    record["sales_angle"] = []
    record["call_brief"] = {}
    record["date_enriched"] = "2026-01-01"
    record["llm_model_used"] = "stub"
    record["llm_prompt_version"] = "v0"
    return record


def _fake_apply_exclusions(record, run_config):
    """Monkeypatch stub — marks record CLEAR with Bullseye tier."""
    record["exclusion_status"] = "CLEAR"
    record["exclusion_reason"] = ""
    record["exclusion_primary_gate"] = ""
    record["target_tier"] = "Bullseye"


def _fake_validate_and_finalize(record):
    """Monkeypatch stub — no-op."""
    pass


# ---------------------------------------------------------------------------
# _is_eligible
# ---------------------------------------------------------------------------

class TestIsEligible:

    def test_enriched_with_context_is_eligible(self):
        assert _is_eligible(_make_record()) is True

    def test_not_enriched_is_ineligible(self):
        assert _is_eligible(_make_record(enrichment_status="not_enriched")) is False

    def test_empty_context_is_ineligible(self):
        assert _is_eligible(_make_record(context="")) is False

    def test_whitespace_context_is_ineligible(self):
        assert _is_eligible(_make_record(context="   \n  ")) is False

    def test_partial_enriched_with_context_is_eligible(self):
        assert _is_eligible(_make_record(enrichment_status="partial")) is True

    def test_missing_context_key_is_ineligible(self):
        record = _make_record()
        del record["_context_text"]
        assert _is_eligible(record) is False


# ---------------------------------------------------------------------------
# run_reextract_preview
# ---------------------------------------------------------------------------

class TestReextractPreview:

    def test_preview_counts_eligible(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [
            _make_record(practice_name="A"),
            _make_record(practice_name="B"),
            _make_record(enrichment_status="not_enriched", practice_name="C"),
            _make_record(context="", practice_name="D"),
        ])
        stats = run_reextract_preview(run_dir)
        assert stats["preview"] is True
        assert stats["eligible"] == 2
        assert stats["skipped_not_enriched"] == 1
        assert stats["skipped_no_context"] == 1
        assert stats["total"] == 4

    def test_preview_all_ineligible(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [
            _make_record(enrichment_status="not_enriched", practice_name="A"),
        ])
        stats = run_reextract_preview(run_dir)
        assert stats["eligible"] == 0
        assert stats["total"] == 1

    def test_preview_missing_targets_raises(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            run_reextract_preview(run_dir)

    def test_preview_does_not_write(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [_make_record()])
        mtime_before = (run_dir / "enriched_targets.json").stat().st_mtime
        run_reextract_preview(run_dir)
        mtime_after = (run_dir / "enriched_targets.json").stat().st_mtime
        assert mtime_before == mtime_after


# ---------------------------------------------------------------------------
# run_reextract_pass (extract_signals monkeypatched)
# ---------------------------------------------------------------------------

class TestReextractPass:

    def test_processes_eligible_records(self, tmp_path, monkeypatch):
        import enrichment.signal_extractor as se
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc
        monkeypatch.setattr(se, "extract_signals", _fake_extract)
        monkeypatch.setattr(ec, "apply_exclusions", _fake_apply_exclusions)
        monkeypatch.setattr(sc, "validate_and_finalize", _fake_validate_and_finalize)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [
            _make_record(practice_name="Clinic A"),
            _make_record(practice_name="Clinic B"),
        ])
        stats = run_reextract_pass(run_dir, [], {}, {}, llm_concurrency=2)

        assert stats["processed"] == 2
        assert stats["skipped"] == 0

    def test_skips_not_enriched(self, tmp_path, monkeypatch):
        import enrichment.signal_extractor as se
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc
        monkeypatch.setattr(se, "extract_signals", _fake_extract)
        monkeypatch.setattr(ec, "apply_exclusions", _fake_apply_exclusions)
        monkeypatch.setattr(sc, "validate_and_finalize", _fake_validate_and_finalize)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [
            _make_record(enrichment_status="not_enriched", practice_name="Roster"),
            _make_record(practice_name="Enriched"),
        ])
        stats = run_reextract_pass(run_dir, [], {}, {})
        assert stats["processed"] == 1
        assert stats["skipped"] == 1

    def test_skips_empty_context(self, tmp_path, monkeypatch):
        import enrichment.signal_extractor as se
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc
        monkeypatch.setattr(se, "extract_signals", _fake_extract)
        monkeypatch.setattr(ec, "apply_exclusions", _fake_apply_exclusions)
        monkeypatch.setattr(sc, "validate_and_finalize", _fake_validate_and_finalize)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [_make_record(context="")])
        stats = run_reextract_pass(run_dir, [], {}, {})
        assert stats["processed"] == 0
        assert stats["skipped"] == 1

    def test_all_ineligible_is_noop(self, tmp_path, monkeypatch):
        import enrichment.signal_extractor as se
        monkeypatch.setattr(se, "extract_signals", _fake_extract)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [_make_record(enrichment_status="not_enriched")])
        mtime_before = (run_dir / "enriched_targets.json").stat().st_mtime
        stats = run_reextract_pass(run_dir, [], {}, {})
        mtime_after = (run_dir / "enriched_targets.json").stat().st_mtime
        assert stats["processed"] == 0
        assert mtime_before == mtime_after

    def test_writes_atomically(self, tmp_path, monkeypatch):
        import enrichment.signal_extractor as se
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc
        monkeypatch.setattr(se, "extract_signals", _fake_extract)
        monkeypatch.setattr(ec, "apply_exclusions", _fake_apply_exclusions)
        monkeypatch.setattr(sc, "validate_and_finalize", _fake_validate_and_finalize)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [_make_record()])
        run_reextract_pass(run_dir, [], {}, {})

        # tmp file is gone; targets file is valid JSON with updated record
        assert not (run_dir / "enriched_targets.json.tmp").exists()
        result = json.loads((run_dir / "enriched_targets.json").read_text())
        records = result["records"]
        assert records[0]["target_tier"] == "Bullseye"

    def test_tracks_tier_changes(self, tmp_path, monkeypatch):
        import enrichment.signal_extractor as se
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc
        monkeypatch.setattr(se, "extract_signals", _fake_extract)
        monkeypatch.setattr(ec, "apply_exclusions", _fake_apply_exclusions)
        monkeypatch.setattr(sc, "validate_and_finalize", _fake_validate_and_finalize)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        # Record starts at Contender; stub promotes to Bullseye
        _write_targets(run_dir, [_make_record(target_tier="Contender", bullseye_score=55)])
        stats = run_reextract_pass(run_dir, [], {}, {})
        assert len(stats["tier_changes"]) == 1
        change = stats["tier_changes"][0]
        assert change["old_tier"] == "Contender"
        assert change["new_tier"] == "Bullseye"

    def test_no_change_when_tier_unchanged(self, tmp_path, monkeypatch):
        import enrichment.signal_extractor as se
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc

        def _same_score_extract(record, *a, **kw):
            # Produce same tier AND same score as the starting record
            _fake_extract(record, *a, **kw)
            record["bullseye_score"] = 55  # keep score at original value
            return record

        def _same_tier_apply(record, run_config):
            record["target_tier"] = "Contender"

        monkeypatch.setattr(se, "extract_signals", _same_score_extract)
        monkeypatch.setattr(ec, "apply_exclusions", _same_tier_apply)
        monkeypatch.setattr(sc, "validate_and_finalize", _fake_validate_and_finalize)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [_make_record(target_tier="Contender", bullseye_score=55)])
        stats = run_reextract_pass(run_dir, [], {}, {})
        assert stats["tier_changes"] == []

    def test_resets_exclusion_fields_before_extract(self, tmp_path, monkeypatch):
        """Stale exclusion fields on the record are cleared before extract_signals runs."""
        import enrichment.signal_extractor as se
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc

        observed_exclusion_status = []

        def _capture_extract(record, *a, **kw):
            observed_exclusion_status.append(record.get("exclusion_status"))
            return _fake_extract(record, *a, **kw)

        monkeypatch.setattr(se, "extract_signals", _capture_extract)
        monkeypatch.setattr(ec, "apply_exclusions", _fake_apply_exclusions)
        monkeypatch.setattr(sc, "validate_and_finalize", _fake_validate_and_finalize)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        record = _make_record()
        record["exclusion_status"] = "EXCLUDED"
        record["exclusion_reason"] = "stale reason"
        _write_targets(run_dir, [record])
        run_reextract_pass(run_dir, [], {}, {})
        assert observed_exclusion_status == ["CLEAR"]

    def test_missing_targets_raises(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            run_reextract_pass(run_dir, [], {}, {})

    def test_preserves_wrapper_envelope(self, tmp_path, monkeypatch):
        """run_id and generated_at in the JSON envelope are preserved after rewrite."""
        import enrichment.signal_extractor as se
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc
        monkeypatch.setattr(se, "extract_signals", _fake_extract)
        monkeypatch.setattr(ec, "apply_exclusions", _fake_apply_exclusions)
        monkeypatch.setattr(sc, "validate_and_finalize", _fake_validate_and_finalize)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [_make_record()])
        # Add extra envelope fields
        path = run_dir / "enriched_targets.json"
        data = json.loads(path.read_text())
        data["generated_at"] = "2026-01-01T12:00:00Z"
        path.write_text(json.dumps(data))

        run_reextract_pass(run_dir, [], {}, {})

        result = json.loads(path.read_text())
        assert result.get("run_id") == "RUN-20260101-120000"
        assert result.get("generated_at") == "2026-01-01T12:00:00Z"


# ---------------------------------------------------------------------------
# _load_run_config and _load_icp_data helpers
# ---------------------------------------------------------------------------

class TestLoaders:

    def test_load_run_config_returns_empty_when_missing(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        assert _load_run_config(run_dir) == {}

    def test_load_run_config_reads_snapshot(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        (run_dir / "project_config_snapshot.json").write_text(
            json.dumps({"bullseye_min_score": 85}), encoding="utf-8"
        )
        cfg = _load_run_config(run_dir)
        assert cfg["bullseye_min_score"] == 85

    def test_load_icp_data_reads_signals_key(self, tmp_path):
        icp_path = tmp_path / "icp.json"
        icp_path.write_text(
            json.dumps({"signals": [{"signal_id": "S-01"}]}), encoding="utf-8"
        )
        data = _load_icp_data(icp_path)
        assert len(data["signals"]) == 1


# ---------------------------------------------------------------------------
# Evidence Vault rehydration (production shape: no _context_text in output)
# ---------------------------------------------------------------------------

from output.evidence_writer import write_record_evidence


def _production_record(record_id="rec-1", status="enriched"):
    """A record as written to enriched_targets.json: no _context_text key."""
    return {
        "id": record_id,
        "practice_name": "Test Clinic",
        "enrichment_status": status,
        "target_tier": "Contender",
        "bullseye_score": 40,
        "exclusion_status": "CLEAR",
        "signals": [],
    }


class TestVaultRehydration:

    def test_preview_counts_vault_record_as_eligible(self, tmp_path):
        """A crawled record with a vault snapshot is eligible despite no _context_text."""
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [_production_record()])
        write_record_evidence(run_dir, "rec-1",
                              [{"url": "https://example.com/", "text": "Clinic services text."}])

        stats = run_reextract_preview(run_dir)
        assert stats["eligible"] == 1
        assert stats["skipped_no_context"] == 0

    def test_preview_no_vault_counts_as_no_context(self, tmp_path):
        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [_production_record()])

        stats = run_reextract_preview(run_dir)
        assert stats["eligible"] == 0
        assert stats["skipped_no_context"] == 1

    def test_pass_rehydrates_and_strips_context(self, tmp_path, monkeypatch):
        """The pass feeds vault text to extract_signals and never persists _context_text."""
        import enrichment.signal_extractor as se
        import enrichment.exclusion_checker as ec
        import enrichment.scorer as sc

        seen = {}

        def _capture_extract(record, icp_signals, context_text, run_id, **kwargs):
            seen["text"] = context_text
            record["signals"] = []
            record["target_tier"] = "Bullseye"
            record["bullseye_score"] = 80

        monkeypatch.setattr(se, "extract_signals", _capture_extract)
        monkeypatch.setattr(ec, "apply_exclusions", _fake_apply_exclusions)
        monkeypatch.setattr(sc, "validate_and_finalize", _fake_validate_and_finalize)

        run_dir = tmp_path / "RUN-20260101-120000"
        run_dir.mkdir()
        _write_targets(run_dir, [_production_record()])
        write_record_evidence(run_dir, "rec-1",
                              [{"url": "https://example.com/", "text": "Clinic offers IUI."}])

        stats = run_reextract_pass(run_dir, [], {}, {}, llm_concurrency=1)

        assert stats["processed"] == 1
        assert "Clinic offers IUI." in seen["text"]
        assert "[Source: https://example.com/]" in seen["text"]

        result = json.loads((run_dir / "enriched_targets.json").read_text())
        assert "_context_text" not in result["records"][0]
        assert result["records"][0]["target_tier"] == "Bullseye"
