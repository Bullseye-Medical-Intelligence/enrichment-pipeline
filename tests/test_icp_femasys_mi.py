"""
test_icp_femasys_mi.py

Regression tests for the Femasys (FemaSeed) Michigan ICP (v10-MI). They drive the
SHIPPED cartridge (config/clients/obgyn_femasys_mi/icp_checklist.json) through
simulate() — the same function the API and pipeline use, composing reinforcement
+ scoring + tiering + exclusion — so the tests guard the real config, not a
synthetic fixture.

Design intent under test (v10-MI):
  - Fit is soft: nothing is required_for_bullseye; any fertility activity scores.
  - IUI is a DISTINCT signal (client's explicit Michigan ask), independent of the
    broad fertility-services signal.
  - Readiness (cash-pay financing AND elective/aesthetic) are positive_weight 0:
    they raise confidence but contribute zero fit, so they can never qualify a
    record on their own.
  - Contraceptive device procedures are a light fit signal, never standalone.
  - Exclusions are load-bearing: IVF/REI fertility centers are dropped.
  - FemVue (the warm-lead signal) floors a confirmed practice at Contender.
  - Fit signals are phrase-bound AND service-context-bound so generic OBGYN copy
    and editorial/blog content do not false-positive.

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

    def test_version_is_v10_mi(self):
        """The shipped cartridge is the Michigan v10-MI revision."""
        data = json.loads(_ICP_PATH.read_text(encoding="utf-8"))
        assert data["version"].startswith("obgyn-femasys-v10")

    def test_readiness_alone_cannot_qualify(self):
        """Cash-pay readiness with NO fertility signal stays out of the call queue.

        Readiness (S-MI-005 financing, S-MI-008 elective) is positive_weight 0, so
        a readiness-only practice has fit 0 and lands below the Manual Review floor
        — it can never reach a client tier.
        """
        for readiness in (["S-MI-005"], ["S-MI-008"], ["S-MI-005", "S-MI-008"]):
            result = _run(readiness)
            assert result["tier"] not in _TOP_TIERS, readiness
            assert result["tier"] == "Manual Review", readiness

    def test_fertility_services_plus_financing_reaches_top_tier(self):
        """Test #2: broad fertility activity lands in a client tier; financing
        (weight 0) rides along on confidence without being needed for the tier."""
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
    """Fit-signal prompts must carry their phrase examples and a negative guard.

    v10 binds on SERVICE CONTEXT, not strict verbatim phrases: a named service,
    a dedicated service page, a 'we treat/offer' statement, or a provider
    specialty all count, so a 'Fertility' service-menu item is detected (the v9-MI
    false negative). The negative guard is now editorial — a term appearing ONLY
    in a blog / patient-education article does not qualify. These config-time tests
    guard the prompt text (anchors present, a 'Do NOT' guard, service-context
    binding); the LLM enforces it at runtime.
    """

    REQUIRED_ANCHORS = {
        "S-MI-001": ["IUI", "intrauterine insemination", "artificial insemination"],
        "S-MI-002": ["infertility evaluation", "fertility consultation",
                     "ovulation induction", "PCOS treatment"],
        "S-MI-003": ["follicle monitoring", "cycle monitoring", "sonohysterogram"],
        "S-MI-004": ["FemVue", "FemaSeed"],
        "S-MI-006": ["IUD insertion", "Nexplanon", "long-acting reversible contraception"],
    }

    # The plain-English cash-pay phrases the prior version missed must be present.
    READINESS_ANCHORS = {
        "S-MI-005": ["cash pay", "self-pay", "CareCredit", "financing options"],
        "S-MI-008": ["Botox", "med spa", "labiaplasty", "semaglutide"],
    }

    def test_fit_prompts_contain_their_anchors(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        for sid, anchors in self.REQUIRED_ANCHORS.items():
            prompt = by_id[sid]["prompt_instruction"]
            for anchor in anchors:
                assert anchor in prompt, f"{sid} prompt missing anchor {anchor!r}"

    def test_readiness_prompts_cover_plain_english(self):
        """v10 fix: readiness prompts carry the plain-English cash-pay / elective
        phrases the prior version missed entirely."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        for sid, anchors in self.READINESS_ANCHORS.items():
            prompt = by_id[sid]["prompt_instruction"]
            for anchor in anchors:
                assert anchor in prompt, f"{sid} prompt missing anchor {anchor!r}"

    def test_fit_prompts_carry_negative_guard(self):
        """Fit prompts must carry a negative guard (editorial / generic-word) so
        the LLM has an explicit not-yes case."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        for sid in ("S-MI-001", "S-MI-002", "S-MI-003", "S-MI-006"):
            prompt = by_id[sid]["prompt_instruction"]
            assert "Do NOT" in prompt, f"{sid} prompt missing a negative guard"

    def test_fit_prompts_bind_to_service_context(self):
        """Every fit/readiness prompt must require service context (not blog/editorial)."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        for sid in ("S-MI-001", "S-MI-002", "S-MI-003", "S-MI-006"):
            prompt = by_id[sid]["prompt_instruction"].lower()
            assert "not_found" in prompt and ("blog" in prompt or "service-context" in prompt), \
                f"{sid} prompt missing service-context binding"


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

    def test_readiness_signals_are_confidence_only(self):
        """v10: both readiness signals carry positive_weight 0 so they feed
        confidence but never fit — the fix for the v9-MI bug where cash-pay
        shipped weight 16 while claiming 'confidence only'."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert by_id["S-MI-005"]["positive_weight"] == 0
        assert by_id["S-MI-008"]["positive_weight"] == 0

    def test_elective_is_separate_readiness_signal(self):
        """v10 splits cash-pay into financing (S-MI-005) and elective/aesthetic
        (S-MI-008), both readiness."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert "S-MI-008" in by_id
        assert "elective" in by_id["S-MI-008"]["signal_label"].lower()

    def test_contraceptive_is_light_fit_not_standalone(self):
        """S-MI-006 contributes fit but is too light to qualify a practice alone."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert by_id["S-MI-006"]["positive_weight"] > 0
        result = _run(["S-MI-006"])
        assert result["tier"] not in _TOP_TIERS

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
