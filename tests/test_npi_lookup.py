"""Tests for ingestion.npi_lookup name matching. Deterministic, no network."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ingestion.npi_lookup import _names_agree  # noqa: E402


def test_single_shared_generic_word_does_not_match():
    # Two distinct practices sharing only a city/generic word must not resolve to
    # the same NPI — a wrong NPI drives a wrong provider-taxonomy exclusion.
    assert not _names_agree("Park Dermatology", "Park Endocrinology Associates")
    assert not _names_agree("Austin OBGYN", "Austin Foot and Ankle")


def test_two_shared_significant_tokens_match():
    assert _names_agree("Riverside Fertility", "Riverside Fertility Center")
    assert _names_agree("Cedar Park Women's Health", "Women's Health of Cedar Park")


def test_single_token_name_matches_on_one_shared_token():
    assert _names_agree("Kaiser", "Kaiser Permanente")


def test_no_overlap_does_not_match():
    assert not _names_agree("Alpha Clinic", "Beta Medical")
