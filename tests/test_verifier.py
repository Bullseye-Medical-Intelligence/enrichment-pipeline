"""
tests/test_verifier.py
Tests for the post-run Needs Verification pass.
All tests are deterministic — no live API calls. GPT is mocked.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from enrichment.verifier import (
    _anchor_check,
    _find_gating_signal_ids,
    _normalize,
    run_verification_pass,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    tier="Needs Verification",
    status="complete",
    signals=None,
    context_text="The clinic offers IUI and has ultrasound on site.",
    already_verified=False,
) -> dict:
    rec = {
        "id": "test-001",
        "practice_name": "Test Clinic",
        "target_tier": tier,
        "enrichment_status": status,
        "_context_text": context_text,
        "signals": signals or [],
    }
    if already_verified:
        rec["verification"] = {"verified_at": "2026-06-01T00:00:00+00:00"}
    return rec


def _make_icp_signals(
    required_id="S-01",
    required_label="IUI listed",
    verification_required_id=None,
    exclude_if_yes_id=None,
) -> list[dict]:
    sigs = [
        {
            "signal_id": required_id,
            "signal_label": required_label,
            "prompt_instruction": "Is IUI listed?",
            "required_for_bullseye": True,
            "verification_required": False,
            "exclude_if_yes": False,
        }
    ]
    if verification_required_id:
        sigs.append({
            "signal_id": verification_required_id,
            "signal_label": "Verification required signal",
            "prompt_instruction": "Check for X.",
            "required_for_bullseye": False,
            "verification_required": True,
            "exclude_if_yes": False,
        })
    if exclude_if_yes_id:
        sigs.append({
            "signal_id": exclude_if_yes_id,
            "signal_label": "Exclusion signal",
            "prompt_instruction": "Check for exclusion.",
            "required_for_bullseye": False,
            "verification_required": False,
            "exclude_if_yes": True,
        })
    return sigs


# ---------------------------------------------------------------------------
# _normalize
# ---------------------------------------------------------------------------

def test_normalize_collapses_whitespace():
    assert _normalize("  Hello   World  ") == "hello world"


def test_normalize_lowercases():
    assert _normalize("IUI SERVICES") == "iui services"


# ---------------------------------------------------------------------------
# _anchor_check
# ---------------------------------------------------------------------------

def test_anchor_check_exact_match():
    assert _anchor_check("IUI services available", "The clinic offers IUI services available on-site.")


def test_anchor_check_case_insensitive():
    assert _anchor_check("iui services", "The clinic offers IUI Services on-site.")


def test_anchor_check_whitespace_normalized():
    assert _anchor_check("IUI  services", "The clinic offers IUI services on-site.")


def test_anchor_check_missing_returns_false():
    assert not _anchor_check("REI specialist on staff", "The clinic offers IUI services.")


def test_anchor_check_empty_evidence_returns_false():
    assert not _anchor_check("", "The clinic offers IUI services.")


def test_anchor_check_empty_context_returns_false():
    assert not _anchor_check("IUI services", "")


# ---------------------------------------------------------------------------
# _find_gating_signal_ids
# ---------------------------------------------------------------------------

def test_find_gating_required_not_found():
    record = _make_record(signals=[
        {"signal_id": "S-01", "signal_state": "not_found", "state_inferred": False},
    ])
    icp = _make_icp_signals(required_id="S-01")
    assert _find_gating_signal_ids(record, icp) == ["S-01"]


def test_find_gating_ignores_yes_signals():
    record = _make_record(signals=[
        {"signal_id": "S-01", "signal_state": "yes", "state_inferred": False},
    ])
    icp = _make_icp_signals(required_id="S-01")
    assert _find_gating_signal_ids(record, icp) == []


def test_find_gating_ignores_inferred():
    record = _make_record(signals=[
        {"signal_id": "S-01", "signal_state": "not_found", "state_inferred": True},
    ])
    icp = _make_icp_signals(required_id="S-01")
    assert _find_gating_signal_ids(record, icp) == []


def test_find_gating_verification_required():
    record = _make_record(signals=[
        {"signal_id": "S-02", "signal_state": "not_found", "state_inferred": False},
    ])
    icp = _make_icp_signals(required_id="S-99", verification_required_id="S-02")
    # S-99 is not_found but doesn't exist in record signals, so only S-02 is gating
    assert "S-02" in _find_gating_signal_ids(record, icp)


# ---------------------------------------------------------------------------
# run_verification_pass — integration with mocked GPT
# ---------------------------------------------------------------------------

def _make_run_dir(tmp_path: Path, records: list[dict], icp_signals: list[dict]) -> Path:
    """Create a minimal run directory with enriched_targets.json."""
    run_dir = tmp_path / "RUN-20260616-140000"
    run_dir.mkdir()
    payload = {"run_id": "RUN-20260616-140000", "records": records}
    (run_dir / "enriched_targets.json").write_text(json.dumps(payload), encoding="utf-8")
    return run_dir


def _gpt_response_promoting(signal_id: str) -> str:
    return json.dumps({
        "signal_verdicts": [
            {"signal_id": signal_id, "signal_state": "yes", "confidence": "high",
             "evidence_text": "IUI services are available on-site."}
        ],
        "notes": "Signal confirmed by blind re-extraction.",
    })


def _gpt_response_not_found(signal_id: str) -> str:
    return json.dumps({
        "signal_verdicts": [
            {"signal_id": signal_id, "signal_state": "not_found", "confidence": None, "evidence_text": ""}
        ],
        "notes": "Signal not found in website text.",
    })


def _gpt_response_disqualify(exclude_id: str, gating_id: str) -> str:
    return json.dumps({
        "signal_verdicts": [
            {"signal_id": gating_id, "signal_state": "yes", "confidence": "high",
             "evidence_text": "IUI listed."},
            {"signal_id": exclude_id, "signal_state": "yes", "confidence": "high",
             "evidence_text": "Hospital-based clinic confirmed."},
        ],
        "notes": "Exclusion signal detected.",
    })


@patch("enrichment.verifier._call_gpt")
def test_promote_outcome(mock_gpt, tmp_path):
    """A record whose gating signal is confirmed by GPT gets recommended_action=promote."""
    icp = _make_icp_signals(required_id="S-01")
    record = _make_record(signals=[
        {"signal_id": "S-01", "signal_state": "not_found", "state_inferred": False},
    ])
    mock_gpt.return_value = _gpt_response_promoting("S-01")

    run_dir = _make_run_dir(tmp_path, [record], icp)
    stats = run_verification_pass(run_dir, icp)

    assert stats["promoted"] == 1
    assert stats["held"] == 0
    updated = json.loads((run_dir / "enriched_targets.json").read_text())
    assert updated["records"][0]["verification"]["recommended_action"] == "promote"
    # Original fields untouched
    assert updated["records"][0]["target_tier"] == "Needs Verification"


@patch("enrichment.verifier._call_gpt")
def test_hold_outcome(mock_gpt, tmp_path):
    """A record whose gating signal is still not_found after GPT stays held."""
    icp = _make_icp_signals(required_id="S-01")
    record = _make_record(signals=[
        {"signal_id": "S-01", "signal_state": "not_found", "state_inferred": False},
    ])
    mock_gpt.return_value = _gpt_response_not_found("S-01")

    run_dir = _make_run_dir(tmp_path, [record], icp)
    stats = run_verification_pass(run_dir, icp)

    assert stats["held"] == 1
    assert stats["promoted"] == 0
    updated = json.loads((run_dir / "enriched_targets.json").read_text())
    assert updated["records"][0]["verification"]["recommended_action"] == "hold"


@patch("enrichment.verifier._call_gpt")
def test_disqualify_outcome(mock_gpt, tmp_path):
    """A record where GPT finds an exclusion signal gets recommended_action=disqualify."""
    icp = _make_icp_signals(required_id="S-01", exclude_if_yes_id="S-EX")
    record = _make_record(signals=[
        {"signal_id": "S-01", "signal_state": "not_found", "state_inferred": False},
    ])
    mock_gpt.return_value = _gpt_response_disqualify("S-EX", "S-01")

    run_dir = _make_run_dir(tmp_path, [record], icp)
    stats = run_verification_pass(run_dir, icp)

    assert stats["disqualified"] == 1
    updated = json.loads((run_dir / "enriched_targets.json").read_text())
    assert updated["records"][0]["verification"]["recommended_action"] == "disqualify"


def test_anchor_failure_skips_gpt(tmp_path):
    """A record with a yes-signal that fails anchor-check gets hold without a GPT call."""
    icp = _make_icp_signals(required_id="S-01")
    record = _make_record(
        context_text="Nothing relevant here.",
        signals=[
            {"signal_id": "S-YES", "signal_state": "yes",
             "evidence_text": "IUI services available on-site.", "state_inferred": False},
            {"signal_id": "S-01", "signal_state": "not_found", "state_inferred": False},
        ],
    )
    run_dir = _make_run_dir(tmp_path, [record], icp)

    with patch("enrichment.verifier._call_gpt") as mock_gpt:
        stats = run_verification_pass(run_dir, icp)
        mock_gpt.assert_not_called()

    assert stats["held"] == 1
    updated = json.loads((run_dir / "enriched_targets.json").read_text())
    v = updated["records"][0]["verification"]
    assert v["recommended_action"] == "hold"
    assert v["method"] == "anchor"
    assert "S-YES" in v["anchor_failures"]


def test_idempotent_skips_already_verified(tmp_path):
    """Re-running on a record that already has verified_at does not overwrite it."""
    icp = _make_icp_signals(required_id="S-01")
    record = _make_record(already_verified=True, signals=[
        {"signal_id": "S-01", "signal_state": "not_found", "state_inferred": False},
    ])
    run_dir = _make_run_dir(tmp_path, [record], icp)

    with patch("enrichment.verifier._call_gpt") as mock_gpt:
        stats = run_verification_pass(run_dir, icp)
        mock_gpt.assert_not_called()

    assert stats["skipped"] == 1
    updated = json.loads((run_dir / "enriched_targets.json").read_text())
    # verified_at should be the original value, not overwritten
    assert updated["records"][0]["verification"]["verified_at"] == "2026-06-01T00:00:00+00:00"


def test_non_needs_verification_records_skipped(tmp_path):
    """Bullseye and Excluded records are not touched by the verification pass."""
    icp = _make_icp_signals(required_id="S-01")
    records = [
        _make_record(tier="Bullseye", signals=[]),
        _make_record(tier="Excluded", signals=[]),
        _make_record(tier="Contender", signals=[]),
    ]
    run_dir = _make_run_dir(tmp_path, records, icp)

    with patch("enrichment.verifier._call_gpt") as mock_gpt:
        stats = run_verification_pass(run_dir, icp)
        mock_gpt.assert_not_called()

    assert stats["skipped"] == 3
    assert stats["promoted"] == 0


def test_verification_object_is_additive(tmp_path):
    """The verification pass must not modify signals, target_tier, or bullseye_score."""
    icp = _make_icp_signals(required_id="S-01")
    record = _make_record(signals=[
        {"signal_id": "S-01", "signal_state": "not_found", "state_inferred": False},
    ])
    record["bullseye_score"] = 72
    record["signals"][0]["evidence_text"] = ""

    run_dir = _make_run_dir(tmp_path, [record], icp)

    with patch("enrichment.verifier._call_gpt") as mock_gpt:
        mock_gpt.return_value = _gpt_response_promoting("S-01")
        run_verification_pass(run_dir, icp)

    updated = json.loads((run_dir / "enriched_targets.json").read_text())
    rec = updated["records"][0]
    assert rec["target_tier"] == "Needs Verification"   # never auto-promoted
    assert rec["bullseye_score"] == 72                   # score unchanged
    assert rec["signals"][0]["signal_state"] == "not_found"  # original signal unchanged
    assert "verification" in rec                         # additive object present


# ---------------------------------------------------------------------------
# Production-shape regression: enriched_targets.json never carries _context_text
# (it is stripped at output). The pass must rehydrate it from the Evidence Vault.
# ---------------------------------------------------------------------------

@patch("enrichment.verifier._call_gpt")
def test_rehydrates_context_from_evidence_vault(mock_gpt, tmp_path):
    """With no _context_text in output, anchor-check uses vault-reconstructed text."""
    from output.evidence_writer import write_record_evidence

    icp = _make_icp_signals(required_id="S-01")
    # Production shape: the record has NO _context_text key at all.
    record = {
        "id": "test-001",
        "practice_name": "Test Clinic",
        "target_tier": "Needs Verification",
        "enrichment_status": "complete",
        "signals": [
            {"signal_id": "S-YES", "signal_state": "yes",
             "evidence_text": "IUI services available on-site.", "state_inferred": False},
            {"signal_id": "S-01", "signal_state": "not_found", "state_inferred": False},
        ],
    }
    mock_gpt.return_value = _gpt_response_promoting("S-01")

    run_dir = _make_run_dir(tmp_path, [record], icp)
    # The crawled page text lives only in the vault, keyed by record id.
    write_record_evidence(
        run_dir, "test-001",
        [{"url": "https://example.com/", "text": "Our clinic. IUI services available on-site. Call today."}],
    )

    stats = run_verification_pass(run_dir, icp)

    # Anchor-check passed against rehydrated text → GPT ran → promote.
    mock_gpt.assert_called_once()
    assert stats["promoted"] == 1
    updated = json.loads((run_dir / "enriched_targets.json").read_text())
    rec = updated["records"][0]
    assert rec["verification"]["recommended_action"] == "promote"
    assert "compromised evidence" not in rec["verification"]["notes"]
    # Rehydrated context must NOT leak back into the output schema.
    assert "_context_text" not in rec


def test_held_when_no_vault_snapshot(tmp_path):
    """No _context_text and no vault snapshot → yes-signal anchor-fails, GPT skipped."""
    icp = _make_icp_signals(required_id="S-01")
    record = {
        "id": "test-001",
        "practice_name": "Test Clinic",
        "target_tier": "Needs Verification",
        "enrichment_status": "complete",
        "signals": [
            {"signal_id": "S-YES", "signal_state": "yes",
             "evidence_text": "IUI services available on-site.", "state_inferred": False},
            {"signal_id": "S-01", "signal_state": "not_found", "state_inferred": False},
        ],
    }
    run_dir = _make_run_dir(tmp_path, [record], icp)

    with patch("enrichment.verifier._call_gpt") as mock_gpt:
        stats = run_verification_pass(run_dir, icp)
        mock_gpt.assert_not_called()

    assert stats["held"] == 1
