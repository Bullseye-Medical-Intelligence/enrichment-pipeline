"""
test_review_fixes.py

Regression tests for the code-review fixes:
- Structural pre-filter exclusions (wrong_specialty / outside_geography).
- Evidence gate clears stale evidence/source on downgrade.
- CSV header case normalization end-to-end in both adapters.
"""

from enrichment.exclusion_checker import check_structural_exclusions
from enrichment.signal_extractor import _validate_and_clean_signals
from ingestion.outscraper_adapter import load_outscraper_csv
from ingestion.manual_adapter import load_manual_csv


class TestStructuralExclusions:
    """check_structural_exclusions: deterministic, signal-independent rules."""

    RC = {"target_specialty": "OBGYN", "target_geography": ["FL"]}

    def test_wrong_specialty_fires_on_known_mismatch(self):
        triggered, _ = check_structural_exclusions(
            {"specialty": "Dentistry", "address_state": "FL"}, self.RC
        )
        assert "wrong_specialty" in triggered

    def test_wrong_specialty_does_not_fire_on_unknown(self):
        # "Unknown" is absent data, not a confirmed mismatch.
        triggered, _ = check_structural_exclusions(
            {"specialty": "Unknown", "address_state": "FL"}, self.RC
        )
        assert "wrong_specialty" not in triggered

    def test_outside_geography_fires(self):
        triggered, _ = check_structural_exclusions(
            {"specialty": "OBGYN", "address_state": "CA"}, self.RC
        )
        assert "outside_geography" in triggered

    def test_matching_record_has_no_structural_exclusion(self):
        triggered, _ = check_structural_exclusions(
            {"specialty": "OBGYN", "address_state": "FL"}, self.RC
        )
        assert triggered == []

    def test_no_targets_means_no_exclusion(self):
        triggered, _ = check_structural_exclusions(
            {"specialty": "Dentistry", "address_state": "CA"}, {}
        )
        assert triggered == []


class TestEvidenceGateClearsStaleFields:
    """A downgraded 'yes' must not leave stale evidence/source behind."""

    ICP = [{"signal_id": "S1", "signal_label": "Test", "positive_weight": 10}]

    def test_unsourced_yes_is_downgraded_and_cleared(self):
        raw = [{
            "signal_id": "S1", "signal_state": "yes",
            "evidence_text": "", "source_url": "", "confidence": "high",
        }]
        sig = _validate_and_clean_signals(raw, self.ICP)[0]
        assert sig["signal_state"] == "not_found"
        assert sig["not_found_reason"] == "evidence_gate"
        assert sig["evidence_text"] == ""
        assert sig["source_url"] == ""

    def test_yes_with_evidence_text_but_no_source_is_cleared(self):
        raw = [{
            "signal_id": "S1", "signal_state": "yes",
            "evidence_text": "We offer IUI", "source_url": "", "confidence": "high",
        }]
        sig = _validate_and_clean_signals(raw, self.ICP)[0]
        assert sig["signal_state"] == "not_found"
        # Stale evidence must be wiped so it cannot render under a not_found badge.
        assert sig["evidence_text"] == ""
        assert sig["source_url"] == ""

    def test_fully_sourced_yes_is_preserved(self):
        raw = [{
            "signal_id": "S1", "signal_state": "yes",
            "evidence_text": "We offer IUI", "source_url": "https://x.com",
            "confidence": "high",
        }]
        sig = _validate_and_clean_signals(raw, self.ICP)[0]
        assert sig["signal_state"] == "yes"
        assert sig["evidence_text"] == "We offer IUI"
        assert sig["source_url"] == "https://x.com"

    def test_yes_with_not_found_source_is_downgraded(self):
        # The prompt tells the model to emit source_url "not_found" when it cannot
        # attribute a page; that non-empty sentinel must not pass the gate.
        raw = [{
            "signal_id": "S1", "signal_state": "yes",
            "evidence_text": "We offer IUI", "source_url": "not_found", "confidence": "high",
        }]
        sig = _validate_and_clean_signals(raw, self.ICP)[0]
        assert sig["signal_state"] == "not_found"
        assert sig["not_found_reason"] == "evidence_gate"

    def test_yes_with_non_url_source_is_downgraded(self):
        raw = [{
            "signal_id": "S1", "signal_state": "yes",
            "evidence_text": "We offer IUI", "source_url": "the services page", "confidence": "high",
        }]
        sig = _validate_and_clean_signals(raw, self.ICP)[0]
        assert sig["signal_state"] == "not_found"

    def test_yes_with_placeholder_evidence_is_downgraded(self):
        raw = [{
            "signal_id": "S1", "signal_state": "yes",
            "evidence_text": "not_found", "source_url": "https://x.com", "confidence": "high",
        }]
        sig = _validate_and_clean_signals(raw, self.ICP)[0]
        assert sig["signal_state"] == "not_found"


class TestCallClaudeTruncation:
    """A response truncated at the token cap must raise so the record is flagged
    needs_review instead of shipping a silently-incomplete signal set."""

    def _client(self, stop_reason):
        import types
        usage = types.SimpleNamespace(
            input_tokens=10, cache_creation_input_tokens=0,
            cache_read_input_tokens=0, output_tokens=5,
        )
        block = types.SimpleNamespace(text='{"signals": [], "sales_angle": []}')
        message = types.SimpleNamespace(content=[block], stop_reason=stop_reason, usage=usage)
        messages = types.SimpleNamespace(create=lambda **kw: message)
        return types.SimpleNamespace(messages=messages)

    def test_truncated_response_raises(self):
        import pytest
        from enrichment.signal_extractor import _call_claude
        with pytest.raises(ValueError):
            _call_claude("sys", "msg", self._client("max_tokens"), "claude-x", retries=0)

    def test_complete_response_returns_text(self):
        from enrichment.signal_extractor import _call_claude
        text, usage = _call_claude("sys", "msg", self._client("end_turn"), "claude-x", retries=0)
        assert "signals" in text
        assert usage["output_tokens"] == 5


class TestCsvHeaderNormalization:
    """Case-variant CSV headers must import the same as lowercase ones."""

    def test_outscraper_uppercase_headers(self, tmp_path):
        p = tmp_path / "out.csv"
        p.write_text("Name,City,State\nTest Clinic,Austin,TX\n")
        recs = load_outscraper_csv(str(p))
        assert len(recs) == 1
        assert recs[0]["practice_name"] == "Test Clinic"

    def test_manual_uppercase_headers(self, tmp_path):
        p = tmp_path / "man.csv"
        p.write_text("Practice_Name,Address_State\nFoo Practice,TX\n")
        recs = load_manual_csv(str(p))
        assert len(recs) == 1
        assert recs[0]["practice_name"] == "Foo Practice"
