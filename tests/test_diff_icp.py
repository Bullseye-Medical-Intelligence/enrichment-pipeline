"""
tests/test_diff_icp.py
Tests for the ICP profile diff (diff_icp.py). Deterministic — no I/O, no LLM.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diff_icp import diff_profiles


def _sig(signal_id="S-01", signal_label="IUI listed", positive_weight=30, **extra):
    """Build a minimal signal dict; extra fields override or add."""
    sig = {
        "signal_id": signal_id,
        "signal_label": signal_label,
        "prompt_instruction": "Does the practice list this?",
        "positive_weight": positive_weight,
    }
    sig.update(extra)
    return sig


def _profile(signals, **top):
    """Build a minimal ICP profile wrapper."""
    p = {"icp_id": "icp-test", "name": "Test ICP", "version": "1.0", "signals": signals}
    p.update(top)
    return p


class TestAddRemove:

    def test_added_signal(self):
        a = _profile([_sig("S-01")])
        b = _profile([_sig("S-01"), _sig("S-02", "FemVue relationship")])
        d = diff_profiles(a, b)
        assert [s["signal_id"] for s in d["added"]] == ["S-02"]
        assert d["added"][0]["signal_label"] == "FemVue relationship"
        assert d["removed"] == []

    def test_removed_signal(self):
        a = _profile([_sig("S-01"), _sig("S-02", "FemVue relationship")])
        b = _profile([_sig("S-01")])
        d = diff_profiles(a, b)
        assert [s["signal_id"] for s in d["removed"]] == ["S-02"]
        assert d["added"] == []


class TestFieldChanges:

    def test_weight_change_reported(self):
        a = _profile([_sig("S-01", positive_weight=20)])
        b = _profile([_sig("S-01", positive_weight=30)])
        d = diff_profiles(a, b)
        assert len(d["changed"]) == 1
        change = d["changed"][0]
        assert change["signal_id"] == "S-01"
        assert change["fields"]["positive_weight"] == {"old": 20, "new": 30}

    def test_flag_change_reported(self):
        a = _profile([_sig("S-01", required_for_bullseye=False)])
        b = _profile([_sig("S-01", required_for_bullseye=True)])
        d = diff_profiles(a, b)
        assert d["changed"][0]["fields"]["required_for_bullseye"] == {"old": False, "new": True}

    def test_field_added_on_b_shows_none_old(self):
        # not_found_weight absent in A, set in B -> old is None.
        a = _profile([_sig("S-01")])
        b = _profile([_sig("S-01", not_found_weight=-5)])
        d = diff_profiles(a, b)
        assert d["changed"][0]["fields"]["not_found_weight"] == {"old": None, "new": -5}

    def test_field_removed_on_b_shows_none_new(self):
        a = _profile([_sig("S-01", cap_tier="Contender")])
        b = _profile([_sig("S-01")])
        d = diff_profiles(a, b)
        assert d["changed"][0]["fields"]["cap_tier"] == {"old": "Contender", "new": None}

    def test_prompt_instruction_change(self):
        a = _profile([_sig("S-01", prompt_instruction="Old prompt")])
        b = _profile([_sig("S-01", prompt_instruction="New prompt")])
        d = diff_profiles(a, b)
        assert d["changed"][0]["fields"]["prompt_instruction"] == {"old": "Old prompt", "new": "New prompt"}

    def test_falsy_zero_weight_not_treated_as_missing(self):
        # positive_weight 0 in A vs absent default must register as a real change,
        # and 0 vs 0 must register as unchanged.
        a = _profile([_sig("S-01", positive_weight=0)])
        b = _profile([_sig("S-01", positive_weight=0)])
        d = diff_profiles(a, b)
        assert d["changed"] == []
        assert d["unchanged_count"] == 1


class TestProfileLevel:

    def test_version_and_contact_strategy_change(self):
        a = _profile([_sig("S-01")], version="7", contact_strategy="physician-first")
        b = _profile([_sig("S-01")], version="8", contact_strategy="treatment coordinator")
        d = diff_profiles(a, b)
        assert d["profile_changes"]["version"] == {"old": "7", "new": "8"}
        assert d["profile_changes"]["contact_strategy"] == {
            "old": "physician-first", "new": "treatment coordinator",
        }


class TestIdentical:

    def test_identical_profiles_no_changes(self):
        a = _profile([_sig("S-01"), _sig("S-02", "Other")])
        b = _profile([_sig("S-01"), _sig("S-02", "Other")])
        d = diff_profiles(a, b)
        assert d["added"] == []
        assert d["removed"] == []
        assert d["changed"] == []
        assert d["unchanged_count"] == 2
        assert d["profile_changes"] == {}

    def test_unchanged_signal_listed(self):
        a = _profile([_sig("S-01"), _sig("S-02", positive_weight=10)])
        b = _profile([_sig("S-01"), _sig("S-02", positive_weight=99)])
        d = diff_profiles(a, b)
        # S-01 unchanged, S-02 changed
        assert [s["signal_id"] for s in d["unchanged"]] == ["S-01"]
        assert [s["signal_id"] for s in d["changed"]] == ["S-02"]


class TestStableOrdering:

    def test_results_sorted_by_signal_id(self):
        a = _profile([_sig("S-03"), _sig("S-01")])
        b = _profile([_sig("S-03"), _sig("S-01"), _sig("S-02"), _sig("S-00")])
        d = diff_profiles(a, b)
        assert [s["signal_id"] for s in d["added"]] == ["S-00", "S-02"]
