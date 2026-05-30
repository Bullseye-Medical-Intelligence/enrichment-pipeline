"""
test_fit_confidence_status.py

Verifies that _determine_fit_confidence_status() classifies records using the
two independent scoring axes (fit_signal_score, confidence_score) — never from
the blended bullseye_score.  The HIGH FIT / LOW EVIDENCE quadrant is the
primary regression target: it must be reachable for records with strong
procedure match and thin evidence, even when the blend falls below the fit
threshold.
"""

import pytest

from enrichment.constants import HIGH_CONFIDENCE_THRESHOLD, HIGH_FIT_THRESHOLD
from enrichment.signal_extractor import _determine_fit_confidence_status


def test_high_fit_low_evidence_survives():
    """
    THE PRIMARY REGRESSION TEST.

    fit_signal_score=90 is high; confidence_score=35 is low.
    The blended bullseye_score = 0.6*90 + 0.4*35 = 68, which is BELOW
    HIGH_FIT_THRESHOLD (70).  Before this fix, the function read from the
    blend, which misclassified this record as LOW FIT — making HIGH FIT /
    LOW EVIDENCE unreachable for exactly the records it is designed to surface.

    After the fix, the function reads fit_signal_score directly (90 >= 70 → high)
    and the correct quadrant is returned regardless of the blend.
    """
    result = _determine_fit_confidence_status(fit_signal_score=90, confidence_score=35)
    assert result == "HIGH FIT / LOW EVIDENCE"


def test_high_fit_high_evidence():
    result = _determine_fit_confidence_status(fit_signal_score=90, confidence_score=80)
    assert result == "HIGH FIT / HIGH EVIDENCE"


def test_low_fit_high_evidence():
    result = _determine_fit_confidence_status(fit_signal_score=40, confidence_score=80)
    assert result == "LOW FIT / HIGH EVIDENCE"


def test_low_fit_low_evidence():
    result = _determine_fit_confidence_status(fit_signal_score=40, confidence_score=30)
    assert result == "LOW FIT / LOW EVIDENCE"


def test_boundary_exactly_at_threshold():
    """Both axes at exactly their threshold value must count as high (>= is inclusive)."""
    result = _determine_fit_confidence_status(
        fit_signal_score=HIGH_FIT_THRESHOLD,
        confidence_score=HIGH_CONFIDENCE_THRESHOLD,
    )
    assert result == "HIGH FIT / HIGH EVIDENCE"


def test_label_ignores_blended_score():
    """
    Explicit proof that the function no longer reads the blended score.

    fit=90, confidence=35 → bullseye_score = 0.6*90 + 0.4*35 = 54+14 = 68.
    68 < HIGH_FIT_THRESHOLD (70), so a blend-based function would return LOW FIT.
    The correct answer is HIGH FIT / LOW EVIDENCE because fit_signal_score (90)
    independently clears the threshold.
    """
    fit = 90
    confidence = 35
    blended = round(0.6 * fit + 0.4 * confidence)
    assert blended < HIGH_FIT_THRESHOLD, (
        f"Test precondition: blended score {blended} must be below HIGH_FIT_THRESHOLD "
        f"{HIGH_FIT_THRESHOLD} to prove independence from the blend"
    )

    result = _determine_fit_confidence_status(fit_signal_score=fit, confidence_score=confidence)
    assert result == "HIGH FIT / LOW EVIDENCE", (
        f"Got '{result}' — function appears to still use the blended score ({blended}) "
        f"instead of fit_signal_score ({fit})"
    )
