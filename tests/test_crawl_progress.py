"""
Tests that the slow network phases (URL validation, web extraction) report
per-record progress, so the run dashboard's progress bar advances instead of
sitting frozen and looking stuck.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from extraction.url_validator import batch_validate_urls
from extraction.web_extractor import ExtractionResult, batch_extract


def _fake_extract(url, **kwargs):
    """Stand in for a real crawl: a successful, content-rich ExtractionResult."""
    return ExtractionResult(url=url, context_text="x" * 4000,
                            pages_crawled=[url, url + "/services"])


def test_batch_extract_reports_progress_per_record():
    """batch_extract ticks progress_callback once per crawled record, ending at (N, N)."""
    records = [
        {"practice_name": f"Clinic {i}", "website_url": f"https://c{i}.example", "_url_valid": True}
        for i in range(3)
    ]
    calls = []
    with patch("extraction.web_extractor.extract_practice_text", side_effect=_fake_extract):
        batch_extract(records, max_workers=1,
                      progress_callback=lambda done, total: calls.append((done, total)))

    assert [c[0] for c in calls] == [1, 2, 3]       # monotonic, one tick per record
    assert all(total == 3 for _, total in calls)
    assert calls[-1] == (3, 3)


def test_batch_extract_progress_total_counts_only_crawlable_records():
    """Records without a valid URL are skipped, so the total reflects the crawlable
    set and the bar can still reach 100%."""
    records = [
        {"practice_name": "Has URL", "website_url": "https://a.example", "_url_valid": True},
        {"practice_name": "No URL", "website_url": "", "_url_valid": False},
    ]
    calls = []
    with patch("extraction.web_extractor.extract_practice_text", side_effect=_fake_extract):
        batch_extract(records, max_workers=1,
                      progress_callback=lambda done, total: calls.append((done, total)))
    assert calls == [(1, 1)]


def test_batch_extract_without_callback_is_a_noop():
    """No progress_callback (the default) must not raise — back-compat for callers."""
    records = [{"practice_name": "C", "website_url": "https://c.example", "_url_valid": True}]
    with patch("extraction.web_extractor.extract_practice_text", side_effect=_fake_extract):
        out = batch_extract(records, max_workers=1)
    assert out is records


def test_batch_validate_urls_reports_progress_per_record():
    """batch_validate_urls ticks progress_callback once per validated record."""
    records = [
        {"practice_name": f"Clinic {i}", "website_url": f"https://c{i}.example"}
        for i in range(3)
    ]

    class _Result:
        is_valid = True
        final_url = "https://final.example"
        error = ""

    calls = []
    with patch("extraction.url_validator.validate_url", return_value=_Result()):
        batch_validate_urls(records, max_workers=1,
                            progress_callback=lambda done, total: calls.append((done, total)))

    assert [c[0] for c in calls] == [1, 2, 3]
    assert all(total == 3 for _, total in calls)
