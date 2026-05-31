"""
Regression tests for source recovery after browser recrawls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from enrichment.exclusion_checker import _assign_tier
from extraction.web_extractor import ExtractionResult, _apply_successful_extraction


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
