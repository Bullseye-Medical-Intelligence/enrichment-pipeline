"""
Regression tests for source recovery after browser recrawls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from enrichment.exclusion_checker import _assign_tier
from extraction.web_extractor import (
    THIN_CRAWL_CHARS,
    ExtractionResult,
    _apply_successful_extraction,
    _source_confidence_for_extraction,
)


def test_browser_recovery_lifts_limited_source_confidence_gate():
    record = {
        "practice_name": "Blocked Clinic",
        "website_url": "https://blocked.example",
        "_url_valid": False,
        "_url_final": "",
        "_url_error": "HTTP 403",
        "source_confidence": "limited",
        # A confirmed signal so the record has evidence; the test isolates the
        # source-confidence gate, not the no-evidence Manual Review rule.
        "signals": [{"signal_id": "S-1", "signal_state": "yes"}],
    }
    result = ExtractionResult(
        url="https://blocked.example",
        context_text="x" * 4001,
        pages_crawled=[
            "https://blocked.example",
            "https://blocked.example/services",
        ],
    )

    _apply_successful_extraction(record, result, recover_blocked_source=True)

    assert record["source_confidence"] == "complete"
    assert record["_url_valid"] is True
    assert record["_url_error"] == ""
    assert record["_url_final"] == "https://blocked.example"
    assert _assign_tier(record, score=95, bullseye_min=75) == "Bullseye"


def test_thin_successful_crawl_is_flagged_limited():
    """A crawl that returned only boilerplate-thin text is flagged for re-crawl."""
    result = ExtractionResult(
        url="https://thin.example",
        context_text="x" * (THIN_CRAWL_CHARS - 1),
        pages_crawled=["https://thin.example"],
    )
    assert _source_confidence_for_extraction(result) == "limited"


def test_decent_partial_crawl_stays_partial():
    """A single-page crawl with real content is trusted (not flagged for browser)."""
    result = ExtractionResult(
        url="https://ok.example",
        context_text="x" * (THIN_CRAWL_CHARS + 500),
        pages_crawled=["https://ok.example"],
    )
    assert _source_confidence_for_extraction(result) == "partial"


def test_rich_multipage_crawl_is_complete():
    result = ExtractionResult(
        url="https://rich.example",
        context_text="x" * 4000,
        pages_crawled=["https://rich.example", "https://rich.example/services"],
    )
    assert _source_confidence_for_extraction(result) == "complete"


def test_fresh_thin_crawl_surfaces_for_browser_recrawl():
    """A first-pass crawl that came back thin lands at 'limited' so the operator
    (and auto-browser-retry) can recover it with a headless browser."""
    record = {"practice_name": "Thin Clinic", "website_url": "https://thin.example"}
    result = ExtractionResult(
        url="https://thin.example",
        context_text="Welcome. Loading...",  # JS-gated page, boilerplate only
        pages_crawled=["https://thin.example"],
    )
    _apply_successful_extraction(record, result, recover_blocked_source=False)
    assert record["source_confidence"] == "limited"


def test_normal_extraction_preserves_upstream_limited_confidence():
    record = {
        "practice_name": "Still Limited Clinic",
        "website_url": "https://limited.example",
        "_url_valid": False,
        "_url_final": "",
        "_url_error": "HTTP 403",
        "source_confidence": "limited",
    }
    result = ExtractionResult(
        url="https://limited.example",
        context_text="usable text",
        pages_crawled=["https://limited.example"],
    )

    _apply_successful_extraction(record, result, recover_blocked_source=False)

    assert record["source_confidence"] == "limited"
    assert record["_url_valid"] is False
    assert record["_url_error"] == "HTTP 403"


def test_extraction_result_carries_evidence_pages_onto_record():
    """Evidence Vault: per-page captures flow from ExtractionResult to the record."""
    record = {"practice_name": "Vault Clinic", "website_url": "https://v.example"}
    pages = [
        {"url": "https://v.example", "text": "Homepage text."},
        {"url": "https://v.example/services", "text": "Services text."},
    ]
    result = ExtractionResult(
        url="https://v.example",
        context_text="combined",
        pages_crawled=["https://v.example", "https://v.example/services"],
        pages=pages,
    )

    _apply_successful_extraction(record, result)

    assert record["_evidence_pages"] == pages


def test_extraction_result_defaults_to_no_evidence_pages():
    """Results built without pages (older call sites) default to an empty list."""
    result = ExtractionResult(url="https://x.example", context_text="t", pages_crawled=[])
    assert result.pages == []
