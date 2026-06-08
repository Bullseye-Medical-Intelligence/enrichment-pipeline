"""Tests for CLI timestamp parsing."""

import pytest

from synthcopilot.cli import parse_timestamp


class TestParseTimestamp:
    def test_mm_ss(self):
        assert parse_timestamp("01:12") == pytest.approx(72.0)
        assert parse_timestamp("00:30") == pytest.approx(30.0)
        assert parse_timestamp("02:05") == pytest.approx(125.0)

    def test_mm_ss_with_decimals(self):
        assert parse_timestamp("01:12.5") == pytest.approx(72.5)

    def test_raw_seconds(self):
        assert parse_timestamp("72") == pytest.approx(72.0)
        assert parse_timestamp("30.5") == pytest.approx(30.5)

    def test_invalid_format(self):
        with pytest.raises(ValueError):
            parse_timestamp("1:2:3")
