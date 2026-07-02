"""
test_attribution_guard.py

Regression tests for the generic false-positive attribution guard (prompt v4 +
_validate_and_clean_signals): a capability "yes" only stands when the evidence
attributes the capability to THIS practice as a current, offered service.
Bio / blog / referral-out / testimonial / historical mentions are downgraded to
not_found (never to "no"). The guard lives in the generic prompt-assembly layer;
the only specialty words below are inside TEST fixture strings, never engine code.

Deterministic — no API calls.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from enrichment.signal_extractor import (  # noqa: E402
    PROMPT_VERSION,
    SERVICE_ATTRIBUTIONS,
    _SYSTEM_TEMPLATE_PATH,
    _validate_and_clean_signals,
)

# The exact evidence from the live-run false positive: a physician's personal
# bio, not a service offering — the signal must resolve not_found, not yes.
IVF_BIO_EVIDENCE = (
    "With two children conceived with IVF, she has a deep interest in "
    "infertility and helping patients build their families."
)

# A condition-explainer / blog page: mentions the topic, offers nothing.
BLOG_EVIDENCE = (
    "Endometriosis affects roughly one in ten women. In this article we explain "
    "the symptoms, diagnosis options, and the treatments available today."
)

ICP = [{
    "signal_id": "S-CAP-01",
    "signal_label": "Capability offered in-house",
    "prompt_instruction": "Does the practice offer this capability as a service?",
    "positive_weight": 30,
}]


def _raw(state="yes", attribution=None, evidence="We offer this in-office.",
         source="https://example.com/services"):
    sig = {
        "signal_id": "S-CAP-01", "signal_state": state, "confidence": "high",
        "evidence_text": evidence, "source_url": source,
    }
    if attribution is not None:
        sig["attribution"] = attribution
    return sig


def test_ivf_bio_evidence_resolves_not_found():
    sig = _validate_and_clean_signals(
        [_raw(attribution="physician_bio", evidence=IVF_BIO_EVIDENCE,
              source="https://example.com/team/dr-x")], ICP)[0]
    assert sig["signal_state"] == "not_found", "bio-attributed capability must not be YES"
    assert sig["not_found_reason"] == "attribution_gate"
    assert sig["evidence_text"] == ""
    assert sig["source_url"] == ""


def test_blog_condition_page_resolves_not_found():
    sig = _validate_and_clean_signals(
        [_raw(attribution="educational_blog", evidence=BLOG_EVIDENCE,
              source="https://example.com/blog/condition-explained")], ICP)[0]
    assert sig["signal_state"] == "not_found"
    assert sig["not_found_reason"] == "attribution_gate"


@pytest.mark.parametrize("category", sorted(SERVICE_ATTRIBUTIONS))
def test_service_attributions_keep_yes(category):
    sig = _validate_and_clean_signals([_raw(attribution=category)], ICP)[0]
    assert sig["signal_state"] == "yes"
    assert sig["not_found_reason"] == ""


@pytest.mark.parametrize("category", [
    "referral_out", "patient_testimonial", "historical_or_aspirational",
])
def test_non_service_attributions_downgrade(category):
    sig = _validate_and_clean_signals([_raw(attribution=category)], ICP)[0]
    assert sig["signal_state"] == "not_found"
    assert sig["not_found_reason"] == "attribution_gate"


def test_unknown_attribution_fails_closed():
    sig = _validate_and_clean_signals([_raw(attribution="homepage_banner")], ICP)[0]
    assert sig["signal_state"] == "not_found"


def test_missing_attribution_passes_through():
    # Pre-v4 recorded responses and legacy fixtures carry no attribution key;
    # they are not retroactively downgraded.
    sig = _validate_and_clean_signals([_raw()], ICP)[0]
    assert sig["signal_state"] == "yes"


def test_no_and_not_found_semantics_unchanged():
    # The guard converts mis-attributed YES only: an explicit "no" stays "no"
    # even when the surrounding text is a bio, and not_found stays not_found.
    no_sig = _validate_and_clean_signals(
        [_raw(state="no", attribution="physician_bio",
              evidence="We do not offer this procedure.")], ICP)[0]
    assert no_sig["signal_state"] == "no"
    assert no_sig["not_found_reason"] == ""

    nf_sig = _validate_and_clean_signals(
        [_raw(state="not_found", attribution="educational_blog", evidence="")], ICP)[0]
    assert nf_sig["signal_state"] == "not_found"
    assert nf_sig["not_found_reason"] == ""


def test_attribution_never_written_to_output():
    sig = _validate_and_clean_signals([_raw(attribution="treatment_menu")], ICP)[0]
    assert "attribution" not in sig


def test_prompt_v4_requires_attribution():
    assert PROMPT_VERSION == "signal_extraction_v4"
    text = _SYSTEM_TEMPLATE_PATH.read_text(encoding="utf-8")
    assert _SYSTEM_TEMPLATE_PATH.name == "signal_extraction_system_v4.txt"
    for token in ("attribution", "physician_bio", "educational_blog",
                  "referral_out", "patient_testimonial",
                  "historical_or_aspirational", "service_description",
                  "treatment_menu", "capability_statement"):
        assert token in text, f"prompt v4 missing '{token}'"
    # The guard is generic: the lines v4 ADDS over v3 carry no client or
    # specialty terms. (v3 already contained neutral illustrative examples like
    # "aligner marketing"; the acceptance constraint is on the new guard text.)
    v3 = (_SYSTEM_TEMPLATE_PATH.parent / "signal_extraction_system_v3.txt").read_text(encoding="utf-8")
    added = "\n".join(line for line in text.splitlines() if line not in set(v3.splitlines()))
    assert "attribution" in added
    for banned in ("IVF", "IUI", "OBGYN", "fertility", "Femasys", "aligner", "ortho"):
        assert banned.lower() not in added.lower(), f"specialty term '{banned}' in the new guard text"
