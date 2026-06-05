"""
Tests for customer suppression list matching logic.
All tests are deterministic — no API calls, no HTTP, no filesystem (except tmpdir).
"""

import csv
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingestion.customer_suppression import (
    SuppressionList,
    _name_tokens,
    _zip5,
    _state2,
    check_suppression,
    load_suppression_list,
)


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_name_tokens_strips_noise(self):
        tokens = _name_tokens("Riverside Medical Group LLC")
        assert "medical" not in tokens
        assert "group" not in tokens
        assert "llc" not in tokens
        assert "riverside" in tokens

    def test_name_tokens_empty(self):
        assert _name_tokens("") == frozenset()
        assert _name_tokens(None) == frozenset()

    def test_name_tokens_all_noise(self):
        assert _name_tokens("Health Care Group LLC") == frozenset()

    def test_zip5_strips_extension(self):
        assert _zip5("90210-1234") == "90210"
        assert _zip5("90210") == "90210"
        assert _zip5("9021") == ""  # too short
        assert _zip5("") == ""
        assert _zip5(None) == ""

    def test_state2_normalizes(self):
        assert _state2("ca") == "CA"
        assert _state2(" TX ") == "TX"
        assert _state2("California") == ""  # > 2 chars
        assert _state2("") == ""


# ---------------------------------------------------------------------------
# load_suppression_list
# ---------------------------------------------------------------------------

def _write_csv(rows: list[dict], fieldnames: list[str]) -> str:
    """Write rows to a temp CSV file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


class TestLoadSuppressionList:
    def test_missing_file_returns_empty(self):
        result = load_suppression_list("/tmp/does_not_exist_xyz.csv")
        assert result.is_empty
        assert result.row_count == 0

    def test_loads_npi_column(self):
        path = _write_csv(
            [{"npi_number": "1234567890"}, {"npi_number": "0987654321"}],
            ["npi_number"],
        )
        try:
            sl = load_suppression_list(path)
            assert "1234567890" in sl.npi_set
            assert "0987654321" in sl.npi_set
            assert sl.row_count == 2
        finally:
            os.unlink(path)

    def test_loads_name_zip(self):
        path = _write_csv(
            [{"practice_name": "Valley Women's Health", "address_zip": "90210"}],
            ["practice_name", "address_zip"],
        )
        try:
            sl = load_suppression_list(path)
            assert "90210" in sl.by_zip
            assert sl.row_count == 1
        finally:
            os.unlink(path)

    def test_loads_name_state(self):
        path = _write_csv(
            [{"practice_name": "Sunrise Fertility Center", "address_state": "TX"}],
            ["practice_name", "address_state"],
        )
        try:
            sl = load_suppression_list(path)
            assert "TX" in sl.by_state
        finally:
            os.unlink(path)

    def test_flexible_column_names(self):
        """Alternate column header names are accepted."""
        path = _write_csv(
            [{"npi": "1111111111", "name": "Test Practice", "zip": "12345", "state": "CA"}],
            ["npi", "name", "zip", "state"],
        )
        try:
            sl = load_suppression_list(path)
            assert "1111111111" in sl.npi_set
            assert "12345" in sl.by_zip
            assert "CA" in sl.by_state
        finally:
            os.unlink(path)

    def test_rows_with_only_noise_name_skipped(self):
        """A practice name that produces no significant tokens is silently ignored."""
        path = _write_csv(
            [{"practice_name": "Health Care Group", "address_zip": "10001"}],
            ["practice_name", "address_zip"],
        )
        try:
            sl = load_suppression_list(path)
            assert "10001" not in sl.by_zip
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# check_suppression
# ---------------------------------------------------------------------------

class TestCheckSuppression:
    def _make_record(self, name="", zip_code="", state="", npi=""):
        return {
            "practice_name": name,
            "address_zip": zip_code,
            "address_state": state,
            "npi_number": npi,
        }

    def test_empty_list_never_matches(self):
        sl = SuppressionList()
        record = self._make_record(name="Any Practice", zip_code="90210")
        suppressed, reason = check_suppression(record, sl)
        assert not suppressed
        assert reason == ""

    def test_npi_exact_match(self):
        path = _write_csv([{"npi_number": "1234567890"}], ["npi_number"])
        try:
            sl = load_suppression_list(path)
            record = self._make_record(npi="1234567890")
            suppressed, reason = check_suppression(record, sl)
            assert suppressed
            assert "NPI match" in reason
            assert "1234567890" in reason
        finally:
            os.unlink(path)

    def test_npi_no_match(self):
        path = _write_csv([{"npi_number": "1234567890"}], ["npi_number"])
        try:
            sl = load_suppression_list(path)
            record = self._make_record(npi="9999999999")
            suppressed, _ = check_suppression(record, sl)
            assert not suppressed
        finally:
            os.unlink(path)

    def test_name_zip_match_two_tokens(self):
        path = _write_csv(
            [{"practice_name": "Riverside Valley Obstetrics", "address_zip": "90210"}],
            ["practice_name", "address_zip"],
        )
        try:
            sl = load_suppression_list(path)
            # Record shares "riverside" + "valley" with suppression row, same ZIP
            record = self._make_record(
                name="Riverside Valley Women's Clinic", zip_code="90210"
            )
            suppressed, reason = check_suppression(record, sl)
            assert suppressed
            assert "name+ZIP" in reason
        finally:
            os.unlink(path)

    def test_name_zip_wrong_zip_no_match(self):
        path = _write_csv(
            [{"practice_name": "Riverside Valley Obstetrics", "address_zip": "90210"}],
            ["practice_name", "address_zip"],
        )
        try:
            sl = load_suppression_list(path)
            record = self._make_record(
                name="Riverside Valley Women's Clinic", zip_code="10001"
            )
            suppressed, _ = check_suppression(record, sl)
            assert not suppressed
        finally:
            os.unlink(path)

    def test_name_zip_insufficient_tokens(self):
        """Only 1 shared token — below the 2-token threshold."""
        path = _write_csv(
            [{"practice_name": "Riverside Obstetrics", "address_zip": "90210"}],
            ["practice_name", "address_zip"],
        )
        try:
            sl = load_suppression_list(path)
            # "riverside" matches but "obstetrics" is not in our record name
            record = self._make_record(
                name="Riverside Fertility Center", zip_code="90210"
            )
            suppressed, _ = check_suppression(record, sl)
            # 1 shared token ("riverside") — below threshold of 2
            assert not suppressed
        finally:
            os.unlink(path)

    def test_name_state_match_three_tokens(self):
        path = _write_csv(
            [{"practice_name": "Sunrise Westside Fertility Associates", "address_state": "TX"}],
            ["practice_name", "address_state"],
        )
        try:
            sl = load_suppression_list(path)
            # Record shares "sunrise" + "westside" + "fertility" = 3 tokens, same state
            record = self._make_record(
                name="Sunrise Westside Fertility Clinic", state="TX"
            )
            suppressed, reason = check_suppression(record, sl)
            assert suppressed
            assert "name+state" in reason
        finally:
            os.unlink(path)

    def test_name_state_insufficient_tokens_no_match(self):
        """2 tokens + state is insufficient (threshold is 3 for state)."""
        path = _write_csv(
            [{"practice_name": "Sunrise Westside Associates", "address_state": "TX"}],
            ["practice_name", "address_state"],
        )
        try:
            sl = load_suppression_list(path)
            record = self._make_record(
                name="Sunrise Westside Obstetrics", state="TX"
            )
            # "sunrise" + "westside" = 2 shared tokens — below state threshold of 3
            suppressed, _ = check_suppression(record, sl)
            assert not suppressed
        finally:
            os.unlink(path)

    def test_npi_priority_over_name(self):
        """NPI match returns immediately without checking name."""
        path = _write_csv(
            [{"npi_number": "5555555555", "practice_name": "Completely Different Name",
              "address_zip": "99999"}],
            ["npi_number", "practice_name", "address_zip"],
        )
        try:
            sl = load_suppression_list(path)
            record = self._make_record(npi="5555555555", name="Other Practice", zip_code="00000")
            suppressed, reason = check_suppression(record, sl)
            assert suppressed
            assert "NPI match" in reason
        finally:
            os.unlink(path)

    def test_record_with_empty_name_no_match(self):
        path = _write_csv(
            [{"practice_name": "Valley Health", "address_zip": "90210"}],
            ["practice_name", "address_zip"],
        )
        try:
            sl = load_suppression_list(path)
            record = self._make_record(name="", zip_code="90210")
            suppressed, _ = check_suppression(record, sl)
            assert not suppressed
        finally:
            os.unlink(path)

    def test_cross_zip_no_false_positive(self):
        """Two suppression rows with the same token but different ZIPs must not cross-match."""
        path = _write_csv(
            [
                {"practice_name": "Valley Coastal Obstetrics", "address_zip": "90210"},
                {"practice_name": "Coastal Valley Pediatrics", "address_zip": "10001"},
            ],
            ["practice_name", "address_zip"],
        )
        try:
            sl = load_suppression_list(path)
            # "valley" + "coastal" matches row 1, but record ZIP is 10001 (row 2's ZIP)
            # Row 2 tokens: "coastal" + "valley" — same 2 tokens, ZIP 10001
            # This SHOULD match because row 2 has the same tokens AND same ZIP
            record = self._make_record(name="Valley Coastal Fertility", zip_code="10001")
            suppressed, _ = check_suppression(record, sl)
            assert suppressed  # matches row 2 (coastal+valley at 10001)
        finally:
            os.unlink(path)
