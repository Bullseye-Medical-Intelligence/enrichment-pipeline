"""
Regression tests for ICP signal generation cleanup.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline-api"))

from signal_generator import _drop_redundant_billing_inverse_signals


def _signal(signal_id, label, prompt, positive_weight):
    return {
        "signal_id": signal_id,
        "signal_label": label,
        "prompt_instruction": prompt,
        "positive_weight": positive_weight,
        "no_weight": 0,
        "not_found_weight": 0,
        "required_for_bullseye": False,
        "source_type": "scraped",
    }


def test_drops_inverse_billing_signal_even_with_cash_pay_signal_present():
    signals = [
        _signal(
            "S-001",
            "Cash-pay or out-of-network service line",
            "Does the website explicitly indicate cash-pay, self-pay, or out-of-network capability?",
            25,
        ),
        _signal(
            "S-002",
            "Payer-restricted billing model",
            "Does the website indicate that the practice only accepts insurance billing?",
            -10,
        ),
    ]

    filtered = _drop_redundant_billing_inverse_signals(signals)

    assert [s["signal_id"] for s in filtered] == ["S-001"]


def test_drops_inverse_billing_signal_without_cash_pay_signal_present():
    signals = [
        _signal(
            "S-001",
            "TMS services offered",
            "Does the practice website explicitly list TMS as a treatment?",
            20,
        ),
        _signal(
            "S-002",
            "Payer-restricted billing model",
            "Does the website indicate that the practice only accepts insurance billing?",
            -10,
        ),
    ]

    filtered = _drop_redundant_billing_inverse_signals(signals)

    assert [s["signal_id"] for s in filtered] == ["S-001"]
