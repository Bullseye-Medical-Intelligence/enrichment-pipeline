"""
test_icp_femasys_mi.py

Regression tests for the Femasys (FemaSeed) Michigan ICP (v9-MI). They drive the
SHIPPED cartridge (config/clients/obgyn_femasys_mi/icp_checklist.json) through
simulate() — the same function the API and pipeline use, composing reinforcement
+ scoring + tiering + exclusion — so the tests guard the real config, not a
synthetic fixture.

Design intent under test (v9-MI):
  - Fit is soft: nothing is required_for_bullseye; any fertility activity scores.
  - IUI is a DISTINCT signal (client's explicit Michigan ask), independent of the
    broad fertility-services signal.
  - Readiness (cash-pay / elective) reinforces confidence but can never qualify a
    record on its own.
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

_CLIENT_DIR = Path(__file__).parent.parent / "config" / "clients" / "obgyn_femasys_mi"
_ICP_PATH = _CLIENT_DIR / "icp_checklist.json"
_RUN_CONFIG_PATH = _CLIENT_DIR / "run_config.json"
# Read the Bullseye cutoff from the shipped run_config so these tests always
# reflect the threshold the pipeline actually runs with, not a hardcoded copy.
_BULLSEYE_MIN = json.loads(_RUN_CONFIG_PATH.read_text(encoding="utf-8"))["bullseye_min_score"]
_TOP_TIERS = ("Bullseye", "Contender")


def _load_signals() -> list[dict]:
    """Load the shipped Femasys v9-MI signal list from the cartridge on disk."""
    data = json.loads(_ICP_PATH.read_text(encoding="utf-8"))
    return data["signals"]


def _states(yes_ids, confidence="high") -> dict:
    """Build a signal_states map marking the given signal_ids as 'yes'."""
    return {sid: {"state": "yes", "confidence": confidence} for sid in yes_ids}


def _run(yes_ids, confidence="high") -> dict:
    """Simulate the shipped v9-MI ICP with the given signals confirmed 'yes'."""
    return simulate(_load_signals(), _states(yes_ids, confidence), _BULLSEYE_MIN)


class TestFemasysMIIntent:
    """End-to-end tier outcomes for the shipped Femasys v9-MI cartridge."""

    def test_version_is_v9_mi(self):
        """The shipped cartridge is the Michigan v9-MI revision."""
        data = json.loads(_ICP_PATH.read_text(encoding="utf-8"))
        assert data["version"].startswith("obgyn-femasys-v9-mi")

    def test_readiness_alone_cannot_qualify(self):
        """Cash-pay readiness with NO fertility signal stays out of the call queue.

        Test #1 from the spec: readiness reinforces confidence but contributes too
        little fit to clear the Manual Review floor on its own, so it can never
        reach a client tier. S-MI-005 consolidates all cash-pay proxies (explicit
        financing AND elective service lines) into a single signal.
        """
        result = _run(["S-MI-005"])
        assert result["tier"] not in _TOP_TIERS
        assert result["tier"] == "Manual Review"

    def test_fertility_services_plus_financing_reaches_top_tier(self):
        """Test #2: 'infertility evaluation' + financing lands in a client tier."""
        result = _run(["S-MI-002", "S-MI-005"])
        assert result["tier"] in _TOP_TIERS

    def test_iui_is_its_own_signal(self):
        """Test #3: IUI fires independently of the broad fertility-services signal.

        With ONLY IUI confirmed (fertility-services not_found), the record still
        scores on IUI's own weight and reaches a client tier — proof the two are
        distinct signals, not folded together.
        """
        result = _run(["S-MI-001"])
        assert result["tier"] in _TOP_TIERS
        assert result["fit_signal_score"] > 0

    def test_ivf_rei_excluded_despite_perfect_fit(self):
        """Test #4: a confirmed IVF/REI center is Excluded even with every fit signal 'yes'."""
        all_fit_plus_ivf = [
            "S-MI-001", "S-MI-002", "S-MI-003", "S-MI-004", "S-MI-005", "S-MI-007",
        ]
        result = _run(all_fit_plus_ivf)
        assert result["tier"] == "Excluded"

    def test_femvue_floors_at_contender(self):
        """Test #5: confirmed FemVue guarantees at least Contender on a thin score."""
        result = _run(["S-MI-004"])
        assert result["tier"] == "Contender"

    def test_femvue_alone_is_not_bullseye(self):
        """FemVue is a warm-lead accelerator, never a one-signal path to Bullseye."""
        result = _run(["S-MI-004"])
        assert result["tier"] != "Bullseye"

    def test_bullseye_reachable_without_femvue(self):
        """Strong fertility fit reaches Bullseye on its own — FemVue is not required."""
        result = _run(["S-MI-001", "S-MI-002", "S-MI-003", "S-MI-005"])
        assert result["tier"] == "Bullseye"

    def test_nothing_is_required_for_bullseye(self):
        """v9-MI keeps v9 intent: no signal is required_for_bullseye."""
        assert all(
            not s.get("required_for_bullseye", False) for s in _load_signals()
        )


class TestFemasysMIPhraseBinding:
    """Fit-signal prompts must be phrase-bound (runtime binding is LLM-enforced).

    Test #6 from the spec lives here at config time: bare 'consultation' /
    'treatment' / 'evaluation' / 'testing' must be explicitly forbidden in the
    fit prompts so generic OBGYN copy does not false-positive. The LLM enforces
    the binding at runtime; this guards the prompt text that drives it.
    """

    REQUIRED_ANCHORS = {
        "S-MI-001": ["IUI", "intrauterine insemination"],
        "S-MI-002": ["infertility evaluation", "fertility consultation", "ovulation induction"],
        "S-MI-003": ["follicle monitoring", "cycle monitoring"],
        "S-MI-004": ["FemVue"],
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
        for sid in ("S-MI-001", "S-MI-002", "S-MI-003"):
            prompt = by_id[sid]["prompt_instruction"]
            assert "Do NOT" in prompt, f"{sid} prompt missing a bare-word guard"


class TestFemasysMIStructure:
    """Structural invariants of the v9-MI cartridge that downstream logic relies on."""

    def test_ivf_rei_is_the_only_exclude_if_yes(self):
        """The IVF/REI signal is the sole exclude_if_yes route to Excluded."""
        excluders = [s["signal_id"] for s in _load_signals()
                     if s.get("exclude_if_yes")]
        assert excluders == ["S-MI-007"]

    def test_femvue_carries_floor_tier_contender(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert by_id["S-MI-004"].get("floor_tier") == "Contender"

    def test_cash_pay_is_single_consolidated_signal(self):
        """r2: cash-pay readiness is one signal (S-MI-005) covering all proxies."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert "S-MI-006" not in by_id, "S-MI-006 was removed — cash-pay is now S-MI-005 only"
        assert by_id["S-MI-005"]["positive_weight"] == 16

    def test_cli_cartridge_matches_api_seed_signals(self):
        """The CLI cartridge and the API seed must carry identical signal logic.

        Two homes for the same ICP (CLI reads the cartridge, the operator UI reads
        the seeded profile); a drift between them would score the same practice two
        different ways.
        """
        seed_path = (
            Path(__file__).parent.parent
            / "pipeline-api" / "seeds" / "icp_profiles" / "obgyn_femasys_mi.json"
        )
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        cartridge_signals = _load_signals()
        # Compare the scoring-relevant fields signal-by-signal.
        keys = (
            "signal_id", "positive_weight", "floor_tier", "cap_tier",
            "reinforces", "exclude_if_yes", "required_for_bullseye",
            "verification_required", "not_found_weight", "no_weight",
        )
        def _norm(sigs):
            return [{k: s.get(k) for k in keys} for s in sigs]
        assert _norm(cartridge_signals) == _norm(seed["signals"])
        assert seed["version"] == json.loads(_ICP_PATH.read_text(encoding="utf-8"))["version"]
