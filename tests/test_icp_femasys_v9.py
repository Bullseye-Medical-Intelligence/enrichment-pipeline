"""
test_icp_femasys_v9.py

Regression tests that encode the client-confirmed Femasys (FemaSeed) ICP v9
intent. They drive the SHIPPED cartridge (config/clients/obgyn_femasys/
icp_checklist.json) through simulate() — the same function the API and the
pipeline use, composing reinforcement + scoring + tiering + exclusion — so the
tests guard the real config, not a synthetic fixture.

Design intent under test (v9):
  - Fit is soft: nothing is required_for_bullseye; any fertility activity scores.
  - Readiness (cash-pay / elective) reinforces but can never qualify a record
    on its own.
  - Exclusions are load-bearing: IVF/REI fertility centers are dropped.
  - FemVue (the warm-lead signal) floors a confirmed practice at Contender.
  - Fit signals are phrase-bound so generic OBGYN copy does not false-positive.

These are deterministic — no API calls, no HTTP.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from simulate_icp import simulate  # noqa: E402

_CLIENT_DIR = Path(__file__).parent.parent / "config" / "clients" / "obgyn_femasys"
_ICP_PATH = _CLIENT_DIR / "icp_checklist.json"
_RUN_CONFIG_PATH = _CLIENT_DIR / "run_config.json"
# Read the Bullseye cutoff from the shipped run_config so these tests always
# reflect the threshold the pipeline actually runs with, not a hardcoded copy.
_BULLSEYE_MIN = json.loads(_RUN_CONFIG_PATH.read_text(encoding="utf-8"))["bullseye_min_score"]
_TOP_TIERS = ("Bullseye", "Contender")
_FIT_SIGNAL_IDS = ("S-ICP-001", "S-ICP-002", "S-ICP-003",
                   "S-ICP-004", "S-ICP-005", "S-ICP-006")


def _load_signals() -> list[dict]:
    """Load the shipped Femasys v9 signal list from the cartridge on disk."""
    data = json.loads(_ICP_PATH.read_text(encoding="utf-8"))
    return data["signals"]


def _states(yes_ids, confidence="high") -> dict:
    """Build a signal_states map marking the given signal_ids as 'yes'."""
    return {sid: {"state": "yes", "confidence": confidence} for sid in yes_ids}


def _run(yes_ids, confidence="high") -> dict:
    """Simulate the shipped v9 ICP with the given signals confirmed 'yes'."""
    return simulate(_load_signals(), _states(yes_ids, confidence), _BULLSEYE_MIN)


class TestFemasysV9Intent:
    """End-to-end tier outcomes for the shipped Femasys v9 cartridge."""

    def test_version_is_v9(self):
        """The shipped cartridge is the client-confirmed v9 revision."""
        data = json.loads(_ICP_PATH.read_text(encoding="utf-8"))
        assert data["version"] == "obgyn-femasys-v9"

    def test_readiness_alone_cannot_qualify(self):
        """Elective + financing with NO fertility signal stays out of the call queue.

        Readiness reinforces confidence but contributes too little fit to clear
        the Manual Review floor on its own, so it can never reach a client tier.
        """
        # Elective ("yes") infers the cash-pay readiness target; no fertility fit.
        result = _run(["S-ICP-008"])
        assert result["tier"] not in _TOP_TIERS
        assert result["tier"] == "Manual Review"

    def test_financing_confirmed_alone_cannot_qualify(self):
        """Even directly-confirmed cash-pay, with no fertility fit, cannot qualify."""
        result = _run(["S-ICP-007"])
        assert result["tier"] not in _TOP_TIERS

    def test_infertility_eval_plus_financing_reaches_top_tier(self):
        """Infertility evaluation + financing lands in a client-shipped tier."""
        result = _run(["S-ICP-003", "S-ICP-007"])
        assert result["tier"] in _TOP_TIERS

    def test_ivf_rei_excluded_despite_perfect_fit(self):
        """A confirmed IVF/REI center is Excluded even with every fit signal 'yes'."""
        all_fit_plus_ivf = list(_FIT_SIGNAL_IDS) + ["S-ICP-007", "S-ICP-009"]
        result = _run(all_fit_plus_ivf)
        assert result["tier"] == "Excluded"

    def test_femvue_floors_at_contender(self):
        """Confirmed FemVue guarantees at least Contender on an otherwise thin score."""
        result = _run(["S-ICP-006"])
        assert result["tier"] == "Contender"

    def test_femvue_alone_is_not_bullseye(self):
        """FemVue is a warm-lead accelerator, never a one-signal path to Bullseye."""
        result = _run(["S-ICP-006"])
        assert result["tier"] != "Bullseye"

    def test_bullseye_reachable_without_femvue(self):
        """Strong fertility fit reaches Bullseye on its own — FemVue is not required.

        This is the load-bearing invariant: a practice doing broad in-office
        fertility work (IUI + medicated cycles + infertility eval + cash-pay +
        cycle monitoring) must be able to reach Bullseye with NO FemVue mention.
        """
        result = _run(["S-ICP-001", "S-ICP-002", "S-ICP-003",
                       "S-ICP-007", "S-ICP-005"])
        assert result["tier"] == "Bullseye"

    def test_broad_fit_plus_femvue_can_reach_bullseye(self):
        """Broad fertility fit plus the FemVue warm lead also reaches Bullseye."""
        result = _run(list(_FIT_SIGNAL_IDS) + ["S-ICP-007"])
        assert result["tier"] == "Bullseye"

    def test_nothing_is_required_for_bullseye(self):
        """v9 dropped every hard fit gate: no signal is required_for_bullseye."""
        assert all(
            not s.get("required_for_bullseye", False) for s in _load_signals()
        )


class TestFemasysV9PhraseBinding:
    """Fit-signal prompts must be phrase-bound (runtime binding is LLM-enforced).

    Bare 'consultation' / 'testing' / 'evaluation' / 'diagnosis' false-positive on
    nearly every OBGYN site, so each fit prompt must name its exact anchors and
    explicitly forbid bare-word matches. This guards the prompt text at config
    time; the LLM enforces the binding at runtime.
    """

    REQUIRED_ANCHORS = {
        "S-ICP-001": ["IUI", "intrauterine insemination"],
        "S-ICP-002": ["medicated cycle", "ovulation induction"],
        "S-ICP-003": ["infertility evaluation", "hysterosalpingogram", "HSG"],
        "S-ICP-004": ["infertility consultation", "fertility consultation"],
        "S-ICP-005": ["follicle monitoring", "cycle monitoring"],
        "S-ICP-006": ["FemVue"],
    }

    def test_fit_prompts_contain_their_anchors(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        for sid, anchors in self.REQUIRED_ANCHORS.items():
            prompt = by_id[sid]["prompt_instruction"]
            for anchor in anchors:
                assert anchor in prompt, f"{sid} prompt missing anchor {anchor!r}"

    def test_phrase_signals_forbid_bare_words(self):
        """The signals most prone to false positives must carry a negative guard."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        for sid in ("S-ICP-001", "S-ICP-002", "S-ICP-003",
                    "S-ICP-004", "S-ICP-005"):
            prompt = by_id[sid]["prompt_instruction"]
            assert "Do NOT" in prompt, f"{sid} prompt missing a bare-word guard"


class TestFemasysV9Structure:
    """Structural invariants of the v9 cartridge that downstream logic relies on."""

    def test_ivf_rei_is_the_only_exclude_if_yes(self):
        """The IVF/REI signal is the sole exclude_if_yes route to Excluded."""
        excluders = [s["signal_id"] for s in _load_signals()
                     if s.get("exclude_if_yes")]
        assert excluders == ["S-ICP-009"]

    def test_femvue_carries_floor_tier_contender(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert by_id["S-ICP-006"].get("floor_tier") == "Contender"

    def test_elective_reinforces_cash_pay_target(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert by_id["S-ICP-008"].get("reinforces") == "S-ICP-007"
        assert by_id["S-ICP-008"]["positive_weight"] == 0
