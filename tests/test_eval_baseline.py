"""Tests for eval_signals.check_baseline — golden-dataset gate enforcement.

check_baseline is pure and deterministic (dicts in, bool + prints out); no API,
no HTTP. These lock the guardrail semantics:
  - a measured metric that meets its floor passes; below its floor fails;
  - a measured metric with NO floor in the baseline fails (an unenforced gate is
    a regression, not a silent pass);
  - a metric with no labeled examples (value None) is skipped, floor or not;
  - any unreviewed case fails the check.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import eval_signals  # noqa: E402


def _metrics(**over):
    """A fully-passing metrics dict; override individual metrics per case."""
    base = {
        "state_accuracy": 0.90,
        "must_have_recall": 0.96,
        "exclusion_recall": 0.96,
        "other_recall": 0.92,
        "yes_precision": 0.90,
        "anchor_rate": 1.0,
    }
    base.update(over)
    return base


def _baseline(**over):
    """A baseline with every floor present; override/remove per case."""
    base = {
        "min_state_accuracy": 0.85,
        "min_must_have_recall": 0.95,
        "min_exclusion_recall": 0.95,
        "min_other_recall": 0.90,
        "min_yes_precision": 0.85,
        "min_anchor_rate": 1.0,
    }
    base.update(over)
    return base


def test_all_metrics_meet_floors_passes():
    assert eval_signals.check_baseline(_metrics(), _baseline()) is True


def test_metric_below_floor_fails():
    assert eval_signals.check_baseline(_metrics(must_have_recall=0.80), _baseline()) is False


def test_measured_metric_with_no_floor_fails():
    # exclusion_recall is measured (0.96) but the baseline carries no floor for it:
    # the gate is unenforced, so --check must FAIL rather than silently pass.
    bl = _baseline()
    del bl["min_exclusion_recall"]
    assert eval_signals.check_baseline(_metrics(), bl) is False


def test_metric_with_no_examples_and_no_floor_is_skipped():
    # No labeled exclusion examples (value None) AND no floor: the gate is inert,
    # so it is skipped (not "unprotected") and the check still passes.
    bl = _baseline()
    del bl["min_exclusion_recall"]
    assert eval_signals.check_baseline(_metrics(exclusion_recall=None), bl) is True


def test_metric_with_no_examples_but_floor_present_passes():
    # Value None with a floor present: nothing to compare, gate skipped, check passes.
    assert eval_signals.check_baseline(_metrics(exclusion_recall=None), _baseline()) is True


def test_unreviewed_cases_fail():
    assert eval_signals.check_baseline(_metrics(), _baseline(), unreviewed=["case-a"]) is False
