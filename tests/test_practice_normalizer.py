"""
tests/test_practice_normalizer.py
Shared normalization for practice-location consolidation.

Deterministic and hermetic: the public suffix data is vendored, so nothing here
touches the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ingestion.practice_normalizer import (  # noqa: E402
    identity_of,
    normalize_address_unit,
    normalize_phone,
    normalize_practice_name,
    normalize_zip5,
    registrable_domain,
    split_street_and_unit,
    stable_hash,
)


class TestStreetAndUnitSplit:
    """The unit is parsed out, kept, and never folded into the street."""

    def test_usps_suffix_expanded(self):
        assert split_street_and_unit("123 Main St") == ("123 main street", "")

    def test_directionals_and_suffix_expanded(self):
        street, unit = split_street_and_unit("1200 N Central Blvd")
        assert street == "1200 north central boulevard"
        assert unit == ""

    def test_abbreviated_and_spelled_forms_agree(self):
        assert (split_street_and_unit("450 W Oak Ave")[0]
                == split_street_and_unit("450 West Oak Avenue")[0])

    def test_unit_is_split_out_not_folded(self):
        street, unit = split_street_and_unit("123 Main St Ste 200")
        assert street == "123 main street"      # unit absent from the street
        assert unit == "suite 200"

    def test_hash_unit_canonicalizes_to_suite(self):
        assert split_street_and_unit("123 Main St #200")[1] == "suite 200"
        assert (split_street_and_unit("123 Main St #200")
                == split_street_and_unit("123 Main Street Suite 200"))

    def test_punctuated_designator(self):
        assert split_street_and_unit("123 Main St., Ste. 410")[1] == "suite 410"

    def test_floor_and_apartment_designators(self):
        assert split_street_and_unit("9 Doctors Park Fl 2")[1] == "floor 2"
        assert split_street_and_unit("9 Elm Rd Apt 3B")[1] == "apt 3b"

    def test_directional_is_not_mistaken_for_a_unit(self):
        """'No'/'N' map to a directional; only a designator with a VALUE opens a unit."""
        street, unit = split_street_and_unit("742 North Shore Road")
        assert street == "742 north shore road"
        assert unit == ""

    def test_leading_st_is_saint_not_street(self):
        assert split_street_and_unit("St Marys Way")[0].startswith("saint")

    def test_empty_input(self):
        assert split_street_and_unit("") == ("", "")
        assert split_street_and_unit(None) == ("", "")


class TestUnitColumn:
    def test_designator_form(self):
        assert normalize_address_unit("Ste. 200") == "suite 200"

    def test_bare_value_gets_default_designator(self):
        assert normalize_address_unit("200") == "suite 200"

    def test_empty(self):
        assert normalize_address_unit("") == ""


class TestUnitEquivalence:
    """A suite is the most specific location identifier the data carries.

    Same-suite is treated as evidence of one practice location, so every way of
    writing one suite must collapse to one token — and two genuinely different
    spaces must never collapse into one.
    """

    SAME_SUITE = [
        "Ste 360", "Suite 360", "STE 360", "#360", "Ste. 360",
        "Suite 360, 3rd Floor", "SUITE #360", "Ste #360", "suite 360",
    ]

    def test_every_spelling_of_one_suite_collapses(self):
        assert len({normalize_address_unit(s) for s in self.SAME_SUITE}) == 1
        assert normalize_address_unit("Suite 360") == "suite 360"

    def test_stray_hash_inside_a_value_is_dropped(self):
        """_tokenize_address isolates '#' so it can BE a designator; after one it
        is a separator, and keeping it split 'SUITE #114' from 'Suite 114'."""
        assert normalize_address_unit("SUITE #114") == normalize_address_unit("Suite 114")

    def test_redundant_floor_is_dropped_when_a_suite_is_present(self):
        assert normalize_address_unit("Suite 360, 3rd Floor") == "suite 360"

    def test_a_floor_alone_is_kept(self):
        """With no suite, the floor is the only identifier there is."""
        assert normalize_address_unit("Floor 3") == "floor 3"
        assert normalize_address_unit("3rd Floor") == "floor 3"

    def test_ordinal_and_cardinal_name_the_same_floor(self):
        assert normalize_address_unit("3rd Floor") == normalize_address_unit("Floor 3")

    def test_building_qualifier_is_kept_and_order_independent(self):
        """A building qualifies a suite, so it survives — but writing order must not."""
        assert (normalize_address_unit("Building A, Suite 360")
                == normalize_address_unit("Suite 360, Building A")
                == "bldg a suite 360")

    def test_suite_letter_suffix_is_a_different_space(self):
        assert normalize_address_unit("Suite 360") != normalize_address_unit("Suite 360A")

    def test_suite_and_floor_are_incomparable(self):
        assert normalize_address_unit("Suite 300") != normalize_address_unit("Floor 3")

    def test_absence_is_never_a_match(self):
        assert normalize_address_unit("") != normalize_address_unit("Suite 360")

    def test_different_suites_stay_different(self):
        assert normalize_address_unit("Suite 360") != normalize_address_unit("Suite 361")


class TestZipAndPhone:
    def test_zip5_from_plus_four(self):
        assert normalize_zip5("95823-1234") == "95823"

    def test_zip_too_short_is_empty(self):
        assert normalize_zip5("958") == ""

    def test_phone_strips_formatting(self):
        assert normalize_phone("(916) 555-0100") == "9165550100"

    def test_phone_strips_leading_country_code(self):
        assert normalize_phone("1-916-555-0100") == "9165550100"
        assert normalize_phone("+1 (916) 555-0100") == "9165550100"

    def test_extension_digits_are_a_known_hazard(self):
        """The rule is 'last 10 digits'. An appended extension shifts the window,
        so an extension-bearing phone simply fails to match a clean one rather
        than matching the wrong practice — the safe direction."""
        assert normalize_phone("916.555.0100 x22") == "6555010022"
        assert normalize_phone("916.555.0100 x22") != normalize_phone("916-555-0100")

    def test_short_phone_is_empty(self):
        assert normalize_phone("555-0100") == ""


class TestRegistrableDomain:
    def test_strips_scheme_www_and_path(self):
        assert registrable_domain("https://www.Practice.com/services") == "practice.com"

    def test_subdomain_reduced_to_etld_plus_one(self):
        assert registrable_domain("https://patients.clinic.example.com") == "example.com"

    def test_multi_label_public_suffix(self):
        """The vendored PSL keeps co.uk domains registrable, not truncated to co.uk."""
        assert registrable_domain("https://www.practice.co.uk") == "practice.co.uk"
        assert registrable_domain("https://booking.practice.co.uk") == "practice.co.uk"

    def test_bare_host_without_scheme(self):
        assert registrable_domain("practice.com") == "practice.com"

    def test_bare_public_suffix_is_not_registrable(self):
        assert registrable_domain("https://co.uk") == ""

    def test_empty_and_garbage(self):
        assert registrable_domain("") == ""
        assert registrable_domain("localhost") == ""


class TestPracticeName:
    def test_strips_legal_suffix(self):
        assert normalize_practice_name("Valley OBGYN, PLLC") == "valley obgyn"

    def test_strips_credentials(self):
        assert normalize_practice_name("Jane Smith, MD, FACOG") == "jane smith"

    def test_case_and_punctuation_collapse(self):
        assert (normalize_practice_name("NorCal  Psychiatry, Inc.")
                == normalize_practice_name("norcal psychiatry"))


class TestIdentityOf:
    def test_unit_from_street_line(self):
        ident = identity_of({
            "practice_name": "Valley OBGYN PLLC",
            "address_street": "123 Main St Ste 200",
            "address_zip": "95823-4444",
            "phone": "1 (916) 555-0100",
            "website_url": "https://www.valleyobgyn.com/about",
        })
        assert ident == {
            "street": "123 main street",
            "unit": "suite 200",
            "zip5": "95823",
            "phone": "9165550100",
            "domain": "valleyobgyn.com",
            "name": "valley obgyn",
        }

    def test_explicit_unit_column_wins(self):
        ident = identity_of({
            "practice_name": "X",
            "address_street": "123 Main St",
            "address_unit": "410",
            "address_zip": "95823",
        })
        assert ident["unit"] == "suite 410"
        assert ident["street"] == "123 main street"

    def test_missing_fields_are_empty_not_none(self):
        ident = identity_of({"practice_name": "Solo"})
        assert ident["street"] == "" and ident["unit"] == ""
        assert ident["zip5"] == "" and ident["phone"] == "" and ident["domain"] == ""


class TestStableHash:
    def test_deterministic_across_calls(self):
        assert stable_hash("a", "b") == stable_hash("a", "b")

    def test_order_sensitive_by_design(self):
        assert stable_hash("a", "b") != stable_hash("b", "a")
