"""
Tests for the evidence link checker CLI (check_links.py).
All classification tests use injected fake fetchers — no live network.
"""

import sys
import os

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from check_links import check_url, classify_chain


def _fetch_sequence(responses):
    """Build a fake fetcher returning queued (status, location) pairs."""
    queue = list(responses)

    def _fetch(url):
        return queue.pop(0)
    return _fetch


class TestClassifyChain:
    def test_direct_200_is_ok(self):
        assert classify_chain("https://a.com/services", "https://a.com/services", 200) == ("OK", "")

    def test_same_domain_redirect_to_page_is_ok(self):
        cls, _ = classify_chain("https://a.com/old", "https://a.com/new-page", 200)
        assert cls == "OK"

    def test_www_variant_is_same_domain(self):
        cls, _ = classify_chain("https://a.com/page", "https://www.a.com/page", 200)
        assert cls == "OK"

    def test_cross_domain_redirect_is_flagged(self):
        cls, detail = classify_chain("https://a.com/page", "https://b.com/page", 200)
        assert cls == "FLAG"
        assert "different domain" in detail

    def test_path_to_homepage_redirect_is_flagged(self):
        cls, detail = classify_chain("https://a.com/services/iud", "https://a.com/", 200)
        assert cls == "FLAG"
        assert "homepage" in detail

    def test_final_404_is_dead(self):
        cls, _ = classify_chain("https://a.com/page", "https://a.com/page", 404)
        assert cls == "DEAD"


class TestCheckUrl:
    def test_200_ok(self):
        result = check_url("https://a.com/services", fetch=_fetch_sequence([(200, "")]))
        assert result["classification"] == "OK"

    def test_follows_same_domain_redirect_ok(self):
        result = check_url(
            "https://a.com/old",
            fetch=_fetch_sequence([(301, "https://a.com/new"), (200, "")]),
        )
        assert result["classification"] == "OK"
        assert result["final_url"] == "https://a.com/new"

    def test_cross_domain_redirect_flagged(self):
        result = check_url(
            "https://a.com/page",
            fetch=_fetch_sequence([(302, "https://other.com/page"), (200, "")]),
        )
        assert result["classification"] == "FLAG"

    def test_path_to_homepage_flagged(self):
        result = check_url(
            "https://a.com/services/iud",
            fetch=_fetch_sequence([(301, "https://a.com/"), (200, "")]),
        )
        assert result["classification"] == "FLAG"

    def test_404_dead(self):
        result = check_url("https://a.com/gone", fetch=_fetch_sequence([(404, "")]))
        assert result["classification"] == "DEAD"

    def test_timeout_dead(self):
        def _timeout(url):
            raise requests.exceptions.Timeout()
        result = check_url("https://a.com/slow", fetch=_timeout)
        assert result["classification"] == "DEAD"
        assert result["detail"] == "timeout"

    def test_dns_failure_dead(self):
        def _dns(url):
            raise requests.exceptions.ConnectionError("DNS")
        result = check_url("https://nodomain.invalid/x", fetch=_dns)
        assert result["classification"] == "DEAD"

    def test_redirect_loop_dead(self):
        result = check_url(
            "https://a.com/loop",
            fetch=_fetch_sequence([(301, "https://a.com/loop")] * 10),
        )
        assert result["classification"] == "DEAD"
        assert "loop" in result["detail"]

    def test_relative_redirect_resolved(self):
        result = check_url(
            "https://a.com/old",
            fetch=_fetch_sequence([(301, "/new"), (200, "")]),
        )
        assert result["final_url"] == "https://a.com/new"
        assert result["classification"] == "OK"
