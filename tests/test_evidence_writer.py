"""
Tests for the Evidence Vault writer (output/evidence_writer.py).
All deterministic — filesystem only, no network.
"""

import hashlib
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from output.evidence_writer import (
    EVIDENCE_DIRNAME,
    INDEX_FILENAME,
    MAX_COMBINED_CONTEXT_CHARS,
    read_record_context_text,
    read_record_evidence_index,
    sanitize_record_id,
    write_record_evidence,
)


def _pages():
    return [
        {"url": "https://example.com/", "text": "Homepage text about IUD insertion."},
        {"url": "https://example.com/services", "text": "Services: contraception counseling."},
    ]


class TestSanitizeRecordId:
    def test_safe_id_unchanged(self):
        assert sanitize_record_id("T-0042_a") == "T-0042_a"

    def test_traversal_characters_replaced(self):
        assert "/" not in sanitize_record_id("../../etc/passwd")
        assert ".." not in sanitize_record_id("../../etc/passwd")

    def test_length_capped(self):
        assert len(sanitize_record_id("x" * 200)) == 80


class TestWriteRecordEvidence:
    def test_writes_pages_and_index(self, tmp_path):
        count = write_record_evidence(tmp_path, "T-001", _pages())
        assert count == 2
        record_dir = tmp_path / EVIDENCE_DIRNAME / "T-001"
        index = json.loads((record_dir / INDEX_FILENAME).read_text(encoding="utf-8"))
        assert len(index) == 2
        assert index[0]["url"] == "https://example.com/"
        assert (record_dir / index[0]["file"]).read_text(encoding="utf-8") == _pages()[0]["text"]

    def test_sha256_matches_content(self, tmp_path):
        write_record_evidence(tmp_path, "T-001", _pages())
        index = read_record_evidence_index(tmp_path, "T-001")
        expected = hashlib.sha256(_pages()[0]["text"].encode("utf-8")).hexdigest()
        assert index[0]["sha256"] == expected

    def test_index_carries_timestamp_and_provenance(self, tmp_path):
        write_record_evidence(tmp_path, "T-001", _pages(), provenance="operator_supplied")
        index = read_record_evidence_index(tmp_path, "T-001")
        assert index[0]["provenance"] == "operator_supplied"
        assert index[0]["fetched_at"]  # non-empty ISO timestamp
        assert index[0]["chars"] == len(_pages()[0]["text"])

    def test_recapture_replaces_previous_snapshot(self, tmp_path):
        write_record_evidence(tmp_path, "T-001", _pages())
        write_record_evidence(tmp_path, "T-001", [{"url": "https://example.com/", "text": "New text."}])
        record_dir = tmp_path / EVIDENCE_DIRNAME / "T-001"
        index = read_record_evidence_index(tmp_path, "T-001")
        assert len(index) == 1
        # The second page file from the first capture must be gone.
        assert not (record_dir / "page-02.txt").exists()

    def test_empty_pages_write_nothing(self, tmp_path):
        assert write_record_evidence(tmp_path, "T-001", []) == 0
        assert write_record_evidence(tmp_path, "T-002", [{"url": "x", "text": "  "}]) == 0
        assert not (tmp_path / EVIDENCE_DIRNAME).exists()

    def test_blank_record_id_raises(self, tmp_path):
        import pytest
        with pytest.raises(ValueError):
            write_record_evidence(tmp_path, "   ", _pages())


class TestReadRecordEvidenceIndex:
    def test_missing_returns_empty(self, tmp_path):
        assert read_record_evidence_index(tmp_path, "T-404") == []

    def test_malformed_returns_empty(self, tmp_path):
        record_dir = tmp_path / EVIDENCE_DIRNAME / "T-001"
        record_dir.mkdir(parents=True)
        (record_dir / INDEX_FILENAME).write_text("not json", encoding="utf-8")
        assert read_record_evidence_index(tmp_path, "T-001") == []

    def test_traversal_record_id_returns_empty(self, tmp_path):
        assert read_record_evidence_index(tmp_path, "../../outside") == []


class TestReadRecordContextText:
    def test_reconstructs_source_prefixed_blocks(self, tmp_path):
        write_record_evidence(tmp_path, "T-001", _pages())
        context = read_record_context_text(tmp_path, "T-001")
        assert "[Source: https://example.com/]" in context
        assert "Homepage text about IUD insertion." in context
        assert "[Source: https://example.com/services]" in context
        assert "Services: contraception counseling." in context
        # Blocks joined by the same separator a live crawl uses.
        assert "\n\n---\n\n" in context

    def test_missing_snapshot_returns_empty(self, tmp_path):
        assert read_record_context_text(tmp_path, "T-404") == ""

    def test_traversal_record_id_returns_empty(self, tmp_path):
        assert read_record_context_text(tmp_path, "../../outside") == ""

    def test_combined_cap_applied(self, tmp_path):
        big = "x" * (MAX_COMBINED_CONTEXT_CHARS + 5000)
        write_record_evidence(tmp_path, "T-001", [{"url": "https://example.com/", "text": big}])
        context = read_record_context_text(tmp_path, "T-001")
        assert len(context) <= MAX_COMBINED_CONTEXT_CHARS + len("\n\n[... truncated for token budget ...]")
        assert context.endswith("[... truncated for token budget ...]")
