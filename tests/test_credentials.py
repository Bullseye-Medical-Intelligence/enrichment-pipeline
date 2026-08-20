"""
tests/test_credentials.py
Provider-name parsing: a credential is an attribute of a person, never a person.

Deterministic and hermetic — pure functions over fixtures, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ingestion.credentials import (  # noqa: E402
    credential_tokens,
    default_credential_tokens,
    is_credential,
    normalize_credential_token,
    split_name_and_credentials,
)
from ingestion.consolidator import consolidate_records  # noqa: E402
from ingestion.manual_adapter import _map_row, _parse_provider_names  # noqa: E402


class TestCredentialTokenList:
    """The list is operator-maintained data, not a literal in engine code."""

    def test_loaded_from_config_file(self):
        from ingestion.credentials import CREDENTIAL_TOKENS_PATH
        assert CREDENTIAL_TOKENS_PATH.exists()
        assert CREDENTIAL_TOKENS_PATH.suffix == ".json"
        assert "config" in CREDENTIAL_TOKENS_PATH.parts

    def test_covers_the_common_credentials(self):
        tokens = default_credential_tokens()
        for token in ("md", "do", "np", "pa-c", "agnp-c", "facog", "phd"):
            assert token in tokens, token

    def test_normalization_ignores_dots_case_and_spaces(self):
        assert normalize_credential_token("Ph.D.") == "phd"
        assert normalize_credential_token(" P A - C ") == "pa-c"
        assert is_credential("M.D.")
        assert is_credential("FACOG")

    def test_a_person_is_not_a_credential(self):
        assert not is_credential("Jane Smith")
        assert not is_credential("")

    def test_run_config_can_extend_the_list(self):
        tokens = credential_tokens({"additional_credential_tokens": ["xyz-q"]})
        assert "xyz-q" in tokens and "md" in tokens

    def test_run_config_can_replace_the_list(self):
        tokens = credential_tokens({"credential_tokens": ["md"]})
        assert tokens == frozenset({"md"})

    def test_list_carries_no_client_or_specialty_names(self):
        for token in default_credential_tokens():
            for banned in ("femasys", "neurolief", "proliv", "obgyn"):
                assert banned not in token


class TestSplitNameAndCredentials:

    def test_single_credential(self):
        assert split_name_and_credentials("Jane Smith, MD") == ("Jane Smith", ["MD"])

    def test_double_credential(self):
        assert split_name_and_credentials("Jane Smith, MD, FACOG") == (
            "Jane Smith", ["MD", "FACOG"])

    def test_each_named_credential_form(self):
        for cred in ("MD", "DO", "NP", "PA-C", "AGNP-C", "FACOG", "PhD"):
            name, creds = split_name_and_credentials(f"Jane Smith, {cred}")
            assert (name, creds) == ("Jane Smith", [cred]), cred

    def test_no_credential_leaves_the_name_whole(self):
        assert split_name_and_credentials("Jane Smith") == ("Jane Smith", [])

    def test_non_credential_comma_part_stays_on_the_name(self):
        """A genuine comma inside a name must not be silently truncated."""
        assert split_name_and_credentials("Smith, Jane") == ("Smith, Jane", [])

    def test_empty_input(self):
        assert split_name_and_credentials("") == ("", [])


class TestManualAdapterProviderNames:

    def test_name_with_credential_is_one_provider(self):
        assert _parse_provider_names("Jane Smith, MD") == ["Jane Smith, MD"]

    def test_two_people_each_with_credentials(self):
        assert _parse_provider_names("Jane Smith, MD, John Doe, DO") == [
            "Jane Smith, MD", "John Doe, DO"]

    def test_double_credential_stays_with_its_person(self):
        assert _parse_provider_names("Jane Smith, MD, FACOG") == [
            "Jane Smith, MD, FACOG"]

    def test_pipe_separated_is_unchanged(self):
        assert _parse_provider_names("Jane Smith, MD|John Doe, DO") == [
            "Jane Smith, MD", "John Doe, DO"]

    def test_plain_comma_list_of_people_still_splits(self):
        assert _parse_provider_names("Jane Smith, John Doe") == [
            "Jane Smith", "John Doe"]

    def test_leading_credential_is_not_attached_to_nothing(self):
        assert _parse_provider_names("MD, Jane Smith") == ["MD", "Jane Smith"]


class TestProvidersAfterConsolidation:
    """End state: the credential rides on the provider, not beside them."""

    def _record(self, provider_names):
        row = _map_row({"practice_name": "Valley Clinic",
                        "provider_names": provider_names,
                        "address_street": "123 Main St",
                        "address_zip": "95823",
                        "phone": "916-555-0100",
                        "website_url": "https://valleyclinic.com"}, 2)
        return consolidate_records([row], {})

    def test_one_person_with_a_credential_is_one_provider(self):
        records, summary = self._record("Jane Smith, MD")
        providers = records[0]["providers"]
        assert len(providers) == 1
        assert providers[0]["name"] == "Jane Smith"
        assert providers[0]["credentials"] == ["MD"]
        assert records[0]["provider_count"] == 1

    def test_double_credential_is_still_one_provider(self):
        records, _ = self._record("Jane Smith, MD, FACOG")
        assert len(records[0]["providers"]) == 1
        assert records[0]["providers"][0]["credentials"] == ["MD", "FACOG"]

    def test_two_credentialled_people_are_two_providers(self):
        records, _ = self._record("Jane Smith, MD, John Doe, PA-C")
        providers = records[0]["providers"]
        assert [p["name"] for p in providers] == ["Jane Smith", "John Doe"]
        assert [p["credentials"] for p in providers] == [["MD"], ["PA-C"]]

    def test_a_bare_credential_never_becomes_a_provider(self):
        """Defence in depth: even if an upstream parse hands us a lone credential."""
        row = _map_row({"practice_name": "Valley Clinic",
                        "address_street": "123 Main St", "address_zip": "95823"}, 2)
        row["provider_names"] = ["Jane Smith", "AGNP-C"]
        records, _ = consolidate_records([row], {})
        assert [p["name"] for p in records[0]["providers"]] == ["Jane Smith"]

    def test_solo_practitioner_keeps_their_own_name(self):
        """The never-a-person rule must not fire on a phantom second provider."""
        records, _ = self._record("Jane Smith, MD")
        assert records[0]["provider_count"] == 1
        assert records[0]["practice_name"] == "Valley Clinic"

    def test_summary_reports_raw_entries_and_distinct_providers(self):
        _, summary = self._record("Jane Smith, MD, FACOG")
        assert summary["raw_provider_entries"] == 1
        assert summary["distinct_providers"] == 1

    def test_summary_gap_is_visible_when_input_writes_letters_as_people(self):
        row = _map_row({"practice_name": "Valley Clinic",
                        "address_street": "123 Main St", "address_zip": "95823"}, 2)
        row["provider_names"] = ["Jane Smith", "MD", "John Doe", "DO"]
        _, summary = consolidate_records([row], {})
        assert summary["raw_provider_entries"] == 4
        assert summary["distinct_providers"] == 2
