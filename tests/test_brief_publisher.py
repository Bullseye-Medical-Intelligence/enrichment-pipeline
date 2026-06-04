"""
tests/test_brief_publisher.py
Unit tests for brief_publisher (non-SFTP functionality only).
"""

import json

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'pipeline-api'))

import brief_publisher


def test_client_slug_basic():
    assert brief_publisher.client_slug_from_name("Right at Home") == "right-at-home"


def test_client_slug_special_chars():
    assert brief_publisher.client_slug_from_name("Dr. Smith's Clinic!") == "dr-smiths-clinic"


def test_client_slug_empty():
    assert brief_publisher.client_slug_from_name("") == "client"


def test_client_slug_max_length():
    long_name = "a" * 60
    result = brief_publisher.client_slug_from_name(long_name)
    assert len(result) <= 40


def test_get_published_briefs_missing(tmp_path):
    """Returns empty dict when no published_briefs.json exists."""
    assert brief_publisher.get_published_briefs(tmp_path) == {}


def test_get_published_briefs_corrupt(tmp_path):
    """Returns empty dict on corrupt JSON."""
    (tmp_path / "published_briefs.json").write_text("not json", encoding="utf-8")
    assert brief_publisher.get_published_briefs(tmp_path) == {}


def test_save_and_read_published_brief(tmp_path):
    """Round-trips a single entry through save/get."""
    entry = {
        "public_url": "https://briefs.bullseyemedical.ai/client/2026-06-04/sales-handoff-abc.html",
        "storage_path": "/home/u1/public_html/client/2026-06-04/sales-handoff-abc.html",
        "filename": "sales-handoff-abc.html",
        "published_at": "2026-06-04T10:00:00+00:00",
    }
    brief_publisher.save_published_brief(tmp_path, "sales-handoff", entry)
    result = brief_publisher.get_published_briefs(tmp_path)
    assert result["sales-handoff"]["public_url"] == entry["public_url"]


def test_save_multiple_briefs(tmp_path):
    """Multiple saves accumulate without overwriting other entries."""
    brief_publisher.save_published_brief(tmp_path, "sales-handoff", {"public_url": "https://a.example/s.html", "filename": "s.html", "storage_path": "/s.html", "published_at": "2026-06-04T10:00:00+00:00"})
    brief_publisher.save_published_brief(tmp_path, "executive-report", {"public_url": "https://a.example/e.html", "filename": "e.html", "storage_path": "/e.html", "published_at": "2026-06-04T10:01:00+00:00"})
    briefs = brief_publisher.get_published_briefs(tmp_path)
    assert "sales-handoff" in briefs
    assert "executive-report" in briefs


def test_publish_brief_no_config(monkeypatch):
    """publish_brief raises RuntimeError when HOSTINGER_SFTP_HOST is not set."""
    import config
    monkeypatch.setattr(config, "HOSTINGER_SFTP_HOST", "")
    with pytest.raises(RuntimeError, match="not configured"):
        brief_publisher.publish_brief(b"<html></html>", "test-client", "sales-handoff")
