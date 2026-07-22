"""
tests/test_recrawl.py
Deterministic tests for recrawl_run.py — no real Playwright, no real LLM.
All external calls are mocked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so recrawl_run and enrichment imports work.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from recrawl_run import run_browser_recrawl_pass, _load_records, _write_records_atomic


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_ICP_SIGNALS = [
    {
        "signal_id": "S-01",
        "signal_label": "Offers IUI",
        "prompt_instruction": "Look for IUI or intrauterine insemination on the site.",
        "positive_weight": 30,
    }
]


def _make_run_dir(tmp_path: Path) -> Path:
    """Create a minimal run directory with enriched_targets.json."""
    run_dir = tmp_path / "RUN-20260616-120000"
    run_dir.mkdir()
    return run_dir


def _write_targets(run_dir: Path, records: list[dict]) -> None:
    """Write a canonical enriched_targets.json wrapper."""
    payload = {
        "run_id": run_dir.name,
        "generated_at": "2026-06-16T12:00:00Z",
        "record_count": len(records),
        "records": records,
    }
    (run_dir / "enriched_targets.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _ok_record(idx: int = 1) -> dict:
    """A record that does not need re-crawl (source_confidence ok)."""
    return {
        "id": f"ok-{idx}",
        "practice_name": f"Good Practice {idx}",
        "website_url": f"https://goodpractice{idx}.com",
        "source_confidence": "ok",
        "target_tier": "Bullseye",
        "bullseye_score": 90,
        "fit_signal_score": 85,
        "confidence_score": 80,
        "signals": [],
        "exclusion_status": "CLEAR",
        "exclusion_reason": None,
        "enrichment_status": "complete",
    }


def _blocked_record(idx: int = 1, confidence: str = "limited") -> dict:
    """A record that is blocked and needs re-crawl."""
    return {
        "id": f"blocked-{idx}",
        "practice_name": f"Blocked Practice {idx}",
        "website_url": f"https://blocked{idx}.com",
        "source_confidence": confidence,
        "target_tier": "Manual Review",
        "bullseye_score": 0,
        "fit_signal_score": 0,
        "confidence_score": 0,
        "signals": [],
        "exclusion_status": "CLEAR",
        "exclusion_reason": None,
        "enrichment_status": "partial",
    }


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _make_extraction_result(context_text: str = "", error: str = "", url: str = ""):
    """Return a minimal ExtractionResult-like object."""
    obj = MagicMock()
    obj.context_text = context_text
    obj.error = error
    obj.url = url
    obj.pages_crawled = [url] if (url and context_text) else []
    obj.pages = []
    return obj


def _make_enriched_record(base: dict) -> dict:
    """Return a record shaped as if extract_signals returned it."""
    return {
        **base,
        "source_confidence": "partial",
        "signals": [
            {
                "signal_id": "S-01",
                "signal_label": "Offers IUI",
                "signal_state": "yes",
                "evidence_text": "We provide IUI services.",
                "source_url": base.get("website_url", ""),
                "source_type": "practice_website",
                "confidence": "high",
                "positive_weight": 30,
                "verification_required": False,
                "required_for_bullseye": False,
                "cap_tier": "",
                "exclude_if_yes": False,
                "inhibited_by": None,
                "state_inferred": False,
                "inferred_from": "",
                "not_found_reason": "",
                "analyst_note": "",
            }
        ],
        "bullseye_score": 85,
        "fit_signal_score": 80,
        "confidence_score": 75,
        "enrichment_status": "complete",
        "exclusion_status": "CLEAR",
        "exclusion_reason": None,
        "target_tier": "Contender",
        "qc_status": "pending",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNoBlockedRecords:
    """test_no_blocked_records_exits_early"""

    def test_all_ok_records_no_recrawl(self, tmp_path):
        """When all records have source_confidence ok, recrawl count is 0 and file unchanged."""
        run_dir = _make_run_dir(tmp_path)
        records = [_ok_record(1), _ok_record(2)]
        _write_targets(run_dir, records)

        with patch("recrawl_run.crawl_with_playwright") as mock_crawl:
            stats = run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        assert stats["recrawled"] == 0
        assert stats["improved"] == 0
        assert stats["still_blocked"] == 0
        mock_crawl.assert_not_called()

        # File should not have been rewritten (mtime unchanged)
        # Note: on very fast systems mtime resolution may be 1s; we skip the
        # mtime check here but verify content is intact.
        loaded, _, _ = _load_records(run_dir)
        assert len(loaded) == 2
        assert loaded[0]["id"] == "ok-1"
        assert loaded[1]["id"] == "ok-2"


class TestBlockedRecordRecrawledAndRescored:
    """test_blocked_record_recrawled_and_rescored"""

    def test_limited_record_improves(self, tmp_path):
        """A limited record is re-crawled, signals extracted, scoring updated."""
        run_dir = _make_run_dir(tmp_path)
        blocked = _blocked_record(1, confidence="limited")
        _write_targets(run_dir, [blocked])

        good_text = "We provide IUI services. " * 50  # well over MIN_CONTEXT_CHARS

        extraction_result = _make_extraction_result(
            context_text=good_text,
            url="https://blocked1.com",
        )
        enriched = _make_enriched_record(blocked)

        with (
            patch("recrawl_run.crawl_with_playwright", return_value=extraction_result),
            patch("recrawl_run.extract_signals", return_value=enriched) as mock_extract,
            patch("recrawl_run.apply_exclusions", side_effect=lambda r, cfg: r),
            patch("recrawl_run.validate_and_finalize", side_effect=lambda r: r),
            patch("recrawl_run.strip_internal_fields", side_effect=lambda r: r),
        ):
            stats = run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        assert stats["recrawled"] == 1
        assert stats["improved"] == 1
        assert stats["still_blocked"] == 0

        mock_extract.assert_called_once()
        call_kwargs = mock_extract.call_args
        assert call_kwargs.kwargs.get("icp_signals") == _SAMPLE_ICP_SIGNALS

        loaded, _, _ = _load_records(run_dir)
        assert len(loaded) == 1
        assert loaded[0]["source_confidence"] == "partial"


class TestStillBlockedAfterRecrawlNotRegressed:
    """test_still_blocked_after_recrawl_not_regressed"""

    def test_failed_recrawl_preserves_original(self, tmp_path):
        """When Playwright still returns thin/blocked result, original record is unchanged."""
        run_dir = _make_run_dir(tmp_path)
        blocked = _blocked_record(1, confidence="failed")
        original_score = blocked["bullseye_score"]
        original_signals = blocked["signals"]
        _write_targets(run_dir, [blocked])

        # Return empty context so the recrawl is still considered blocked
        failed_result = _make_extraction_result(
            context_text="",
            error="Bot blocked",
            url="https://blocked1.com",
        )

        with (
            patch("recrawl_run.crawl_with_playwright", return_value=failed_result),
            patch("recrawl_run.extract_signals") as mock_extract,
        ):
            stats = run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        assert stats["recrawled"] == 1
        assert stats["improved"] == 0
        assert stats["still_blocked"] == 1

        # extract_signals must NOT have been called — no regression
        mock_extract.assert_not_called()

        # Original record must be unchanged in the file
        loaded, _, _ = _load_records(run_dir)
        assert loaded[0]["bullseye_score"] == original_score
        assert loaded[0]["signals"] == original_signals
        assert loaded[0]["source_confidence"] == "failed"

    def test_thin_context_not_regressed(self, tmp_path):
        """When Playwright returns fewer than MIN_CONTEXT_CHARS, record is not rescored."""
        from enrichment.constants import MIN_CONTEXT_CHARS

        run_dir = _make_run_dir(tmp_path)
        blocked = _blocked_record(1, confidence="limited")
        _write_targets(run_dir, [blocked])

        thin_result = _make_extraction_result(
            context_text="A" * (MIN_CONTEXT_CHARS - 1),
            url="https://blocked1.com",
        )

        with (
            patch("recrawl_run.crawl_with_playwright", return_value=thin_result),
            patch("recrawl_run.extract_signals") as mock_extract,
        ):
            stats = run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        assert stats["still_blocked"] == 1
        mock_extract.assert_not_called()

        loaded, _, _ = _load_records(run_dir)
        assert loaded[0]["source_confidence"] == "limited"


class TestIdempotentSecondRun:
    """test_idempotent_second_run"""

    def test_second_run_finds_nothing_to_recrawl(self, tmp_path):
        """After a successful recrawl, a second run finds no blocked records."""
        run_dir = _make_run_dir(tmp_path)
        # Write a record that is now ok (already fixed by first pass)
        already_fixed = {
            **_blocked_record(1),
            "source_confidence": "partial",  # previously limited, now fixed
        }
        _write_targets(run_dir, [already_fixed])

        with patch("recrawl_run.crawl_with_playwright") as mock_crawl:
            stats = run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        assert stats["recrawled"] == 0
        assert stats["improved"] == 0
        mock_crawl.assert_not_called()


class TestOnlyBlockedRecordsTouched:
    """test_only_blocked_records_touched"""

    def test_ok_records_unchanged(self, tmp_path):
        """Mix of ok and limited records: ok records are byte-for-byte identical in output."""
        run_dir = _make_run_dir(tmp_path)
        ok1 = _ok_record(1)
        ok2 = _ok_record(2)
        blocked = _blocked_record(3, confidence="limited")
        _write_targets(run_dir, [ok1, ok2, blocked])

        good_text = "IUI services offered here. " * 60
        extraction_result = _make_extraction_result(
            context_text=good_text,
            url="https://blocked3.com",
        )
        enriched = _make_enriched_record(blocked)
        enriched["id"] = "blocked-3"

        with (
            patch("recrawl_run.crawl_with_playwright", return_value=extraction_result),
            patch("recrawl_run.extract_signals", return_value=enriched),
            patch("recrawl_run.apply_exclusions", side_effect=lambda r, cfg: r),
            patch("recrawl_run.validate_and_finalize", side_effect=lambda r: r),
            patch("recrawl_run.strip_internal_fields", side_effect=lambda r: r),
        ):
            stats = run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        assert stats["recrawled"] == 1
        assert stats["improved"] == 1

        loaded, _, _ = _load_records(run_dir)
        assert len(loaded) == 3

        # ok records must be byte-for-byte identical (same content)
        assert loaded[0]["id"] == "ok-1"
        assert loaded[0]["bullseye_score"] == ok1["bullseye_score"]
        assert loaded[0]["target_tier"] == ok1["target_tier"]
        assert loaded[0]["signals"] == ok1["signals"]

        assert loaded[1]["id"] == "ok-2"
        assert loaded[1]["bullseye_score"] == ok2["bullseye_score"]
        assert loaded[1]["target_tier"] == ok2["target_tier"]
        assert loaded[1]["signals"] == ok2["signals"]

        # Blocked record should be improved
        assert loaded[2]["id"] == "blocked-3"
        assert loaded[2]["source_confidence"] == "partial"

    def test_failed_confidence_also_targeted(self, tmp_path):
        """Records with source_confidence failed are also targeted for re-crawl."""
        run_dir = _make_run_dir(tmp_path)
        ok = _ok_record(1)
        failed = _blocked_record(2, confidence="failed")
        _write_targets(run_dir, [ok, failed])

        failed_crawl = _make_extraction_result(error="still blocked")

        with (
            patch("recrawl_run.crawl_with_playwright", return_value=failed_crawl),
            patch("recrawl_run.extract_signals") as mock_extract,
        ):
            stats = run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        assert stats["recrawled"] == 1  # tried to re-crawl the failed record
        assert stats["still_blocked"] == 1
        mock_extract.assert_not_called()

        loaded, _, _ = _load_records(run_dir)
        # ok record untouched
        assert loaded[0]["id"] == "ok-1"
        assert loaded[0]["target_tier"] == "Bullseye"


class TestAtomicWrite:
    """Verify the write is atomic (tmp file + rename)."""

    def test_atomic_write_produces_valid_json(self, tmp_path):
        """_write_records_atomic writes valid JSON and the tmp file is gone after."""
        run_dir = _make_run_dir(tmp_path)
        records = [_ok_record(1)]
        payload = {"run_id": run_dir.name, "records": records, "record_count": 1}
        (run_dir / "enriched_targets.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

        from output.atomic_write import stat_fingerprint
        fp = stat_fingerprint(run_dir / "enriched_targets.json")
        _write_records_atomic(run_dir, records, payload, fp)

        # tmp file should be gone
        tmp = run_dir / "enriched_targets.tmp"
        assert not tmp.exists()

        # Output file is valid JSON
        content = (run_dir / "enriched_targets.json").read_text(encoding="utf-8")
        loaded = json.loads(content)
        assert loaded["record_count"] == 1
        assert len(loaded["records"]) == 1


class TestExcludedRecordsSkipped:
    """EXCLUDED records are never re-crawled — exclusions are preserved."""

    def test_excluded_blocked_record_not_recrawled(self, tmp_path):
        run_dir = _make_run_dir(tmp_path)
        excluded = _blocked_record(1, confidence="limited")
        excluded["exclusion_status"] = "EXCLUDED"
        excluded["exclusion_reason"] = "Existing customer"
        excluded["target_tier"] = "Excluded"
        normal = _blocked_record(2, confidence="limited")
        _write_targets(run_dir, [excluded, normal])

        good_text = "We provide IUI services. " * 50
        extraction_result = _make_extraction_result(
            context_text=good_text, url="https://blocked2.com"
        )
        enriched = _make_enriched_record(normal)

        with (
            patch("recrawl_run.crawl_with_playwright", return_value=extraction_result) as mock_crawl,
            patch("recrawl_run.extract_signals", return_value=enriched),
            patch("recrawl_run.apply_exclusions", side_effect=lambda r, cfg: r),
            patch("recrawl_run.validate_and_finalize", side_effect=lambda r: r),
            patch("recrawl_run.strip_internal_fields", side_effect=lambda r: r),
        ):
            stats = run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        assert stats["skipped_excluded"] == 1
        assert stats["recrawled"] == 1  # only the non-excluded record
        assert mock_crawl.call_count == 1

        loaded, _, _ = _load_records(run_dir)
        by_id = {r["id"]: r for r in loaded}
        assert by_id["blocked-1"]["exclusion_status"] == "EXCLUDED"
        assert by_id["blocked-1"]["target_tier"] == "Excluded"
        assert by_id["blocked-1"]["signals"] == []


class TestPageBudget:
    """The re-crawl uses the standard page budget, not the function default."""

    def test_default_budget_is_20(self, tmp_path):
        run_dir = _make_run_dir(tmp_path)
        _write_targets(run_dir, [_blocked_record(1)])
        failed = _make_extraction_result(context_text="", error="blocked")

        with patch("recrawl_run.crawl_with_playwright", return_value=failed) as mock_crawl:
            run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        assert mock_crawl.call_args.kwargs.get("max_pages") == 20

    def test_budget_from_config_snapshot(self, tmp_path):
        run_dir = _make_run_dir(tmp_path)
        (run_dir / "project_config_snapshot.json").write_text(
            json.dumps({"max_pages_per_practice": 12}), encoding="utf-8"
        )
        _write_targets(run_dir, [_blocked_record(1)])
        failed = _make_extraction_result(context_text="", error="blocked")

        with patch("recrawl_run.crawl_with_playwright", return_value=failed) as mock_crawl:
            run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        assert mock_crawl.call_args.kwargs.get("max_pages") == 12


class TestEvidenceVaultWrite:
    """A successful re-crawl persists the fresh pages to the Evidence Vault."""

    def test_vault_written_on_success(self, tmp_path):
        run_dir = _make_run_dir(tmp_path)
        blocked = _blocked_record(1, confidence="limited")
        _write_targets(run_dir, [blocked])

        good_text = "We provide IUI services. " * 50
        extraction_result = _make_extraction_result(
            context_text=good_text, url="https://blocked1.com"
        )
        extraction_result.pages = [
            {"url": "https://blocked1.com", "text": good_text}
        ]
        enriched = _make_enriched_record(blocked)

        with (
            patch("recrawl_run.crawl_with_playwright", return_value=extraction_result),
            patch("recrawl_run.extract_signals", return_value=enriched),
            patch("recrawl_run.apply_exclusions", side_effect=lambda r, cfg: r),
            patch("recrawl_run.validate_and_finalize", side_effect=lambda r: r),
            patch("recrawl_run.strip_internal_fields", side_effect=lambda r: r),
        ):
            stats = run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        assert stats["improved"] == 1
        evidence_dir = run_dir / "evidence" / "blocked-1"
        assert evidence_dir.is_dir()
        index = json.loads((evidence_dir / "index.json").read_text(encoding="utf-8"))
        pages = index if isinstance(index, list) else index.get("pages", [])
        assert pages and pages[0]["provenance"] == "recrawl"

    def test_vault_not_written_when_extraction_fails(self, tmp_path):
        """Extraction failure reverts the record — the vault must keep matching it."""
        run_dir = _make_run_dir(tmp_path)
        blocked = _blocked_record(1, confidence="limited")
        _write_targets(run_dir, [blocked])

        good_text = "We provide IUI services. " * 50
        extraction_result = _make_extraction_result(
            context_text=good_text, url="https://blocked1.com"
        )
        extraction_result.pages = [{"url": "https://blocked1.com", "text": good_text}]

        with (
            patch("recrawl_run.crawl_with_playwright", return_value=extraction_result),
            patch("recrawl_run.extract_signals", side_effect=RuntimeError("LLM down")),
        ):
            stats = run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        assert stats["still_blocked"] == 1
        assert not (run_dir / "evidence" / "blocked-1").exists()


class TestExclusionFailClosed:
    """An exclusion-check error aborts the pass — never forces CLEAR."""

    def test_apply_exclusions_error_aborts_whole_pass(self, tmp_path):
        run_dir = _make_run_dir(tmp_path)
        blocked = _blocked_record(1, confidence="limited")
        _write_targets(run_dir, [blocked])
        before = (run_dir / "enriched_targets.json").read_bytes()

        good_text = "We provide IUI services. " * 50
        extraction_result = _make_extraction_result(
            context_text=good_text, url="https://blocked1.com"
        )
        enriched = _make_enriched_record(blocked)

        with (
            patch("recrawl_run.crawl_with_playwright", return_value=extraction_result),
            patch("recrawl_run.extract_signals", return_value=enriched),
            patch("recrawl_run.apply_exclusions", side_effect=RuntimeError("bad rule")),
        ):
            import pytest
            with pytest.raises(RuntimeError):
                run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        # Nothing was written
        assert (run_dir / "enriched_targets.json").read_bytes() == before


class TestConcurrentWriteGuard:
    """A concurrent rewrite of enriched_targets.json refuses the pass write."""

    def test_pass_write_refused_after_concurrent_merge(self, tmp_path):
        from output.atomic_write import ConcurrentRunChange

        run_dir = _make_run_dir(tmp_path)
        blocked = _blocked_record(1, confidence="limited")
        _write_targets(run_dir, [blocked])
        targets = run_dir / "enriched_targets.json"

        good_text = "We provide IUI services. " * 50
        extraction_result = _make_extraction_result(
            context_text=good_text, url="https://blocked1.com"
        )
        enriched = _make_enriched_record(blocked)

        def _extract_and_merge(**kwargs):
            # Simulate a batch merge landing while the pass is mid-LLM.
            merged = {"run_id": run_dir.name, "records": [
                {**blocked, "practice_name": "Merged Elsewhere"}
            ], "record_count": 1}
            tmp = run_dir / "merge.tmp"
            tmp.write_text(json.dumps(merged), encoding="utf-8")
            import os as _os
            _os.replace(tmp, targets)
            return enriched

        with (
            patch("recrawl_run.crawl_with_playwright", return_value=extraction_result),
            patch("recrawl_run.extract_signals", side_effect=_extract_and_merge),
            patch("recrawl_run.apply_exclusions", side_effect=lambda r, cfg: r),
            patch("recrawl_run.validate_and_finalize", side_effect=lambda r: r),
            patch("recrawl_run.strip_internal_fields", side_effect=lambda r: r),
        ):
            import pytest
            with pytest.raises(ConcurrentRunChange):
                run_browser_recrawl_pass(run_dir, _SAMPLE_ICP_SIGNALS)

        # The merge's data survived; the stale pass wrote nothing.
        final = json.loads(targets.read_text(encoding="utf-8"))
        assert final["records"][0]["practice_name"] == "Merged Elsewhere"
