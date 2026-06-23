"""
test_icp_femasys_v9.py

Regression tests encoding the SHIPPED Femasys (FemaSeed) cartridge intent
(currently v12). They drive the real config
(config/clients/obgyn_femasys/icp_checklist.json) through simulate() — the same
composition of reinforcement + scoring + tiering + exclusion the API and the
pipeline use — so the tests guard the real cartridge, not a synthetic fixture.

v12 signal model under test:
  - Two PRIMARY must-haves define an 85-point fit base: cash_pay_signal (50) and
    fertility_services (35), both required_for_bullseye.
  - Three REINFORCERS add fit on top of the base (not members of the denominator;
    fit caps at 100): iui_listed (+15), cycle_monitoring_listed (+10),
    patient_financing_visible (+10).
  - Two NEGATIVES apply a flat 20-point penalty (full weight, not confidence-
    credited, subtracted after the 0.6/0.4 blend) and cap the tier at Contender:
    ivf_listed, rei_on_staff. Neither is a hard exclusion any longer.
  - fertility_services carries floor_tier Contender (a confirmed fertility practice
    is always at least Contender, even on a thin score).
  - The cartridge is national: target_geography is empty in run_config, and REI is
    no longer a pre-crawl taxonomy skip.

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

_PRIMARIES = ("cash_pay_signal", "fertility_services")
_REINFORCERS = ("iui_listed", "cycle_monitoring_listed", "patient_financing_visible")
_NEGATIVES = ("ivf_listed", "rei_on_staff")
_ALL_IDS = _PRIMARIES + _REINFORCERS + _NEGATIVES


def _load_signals() -> list[dict]:
    """Load the shipped Femasys v12 signal list from the cartridge on disk."""
    data = json.loads(_ICP_PATH.read_text(encoding="utf-8"))
    return data["signals"]


def _states(yes_ids, confidence="high") -> dict:
    """Build a signal_states map marking the given signal_ids 'yes'."""
    return {sid: {"state": "yes", "confidence": confidence} for sid in yes_ids}


def _run(states, confidence="high") -> dict:
    """Simulate the shipped v12 cartridge with the given signal states.

    Accepts either a list of signal_ids to mark 'yes', or a full signal_states map.
    """
    if isinstance(states, (list, tuple)):
        states = _states(states, confidence)
    return simulate(_load_signals(), states, _BULLSEYE_MIN)


class TestFemasysV12Intent:
    """End-to-end tier outcomes for the shipped v12 cartridge — the task's
    verification cases, driven through the real config."""

    def test_version_is_v12(self):
        data = json.loads(_ICP_PATH.read_text(encoding="utf-8"))
        assert data["version"] == "obgyn-femasys-v12"

    def test_both_must_haves_alone_reach_bullseye(self):
        # The two primaries fill the 85-point base -> fit 100 -> Bullseye.
        result = _run(["cash_pay_signal", "fertility_services"])
        assert result["tier"] == "Bullseye"

    def test_case1_cash_fertility_iui_is_bullseye(self):
        result = _run(["cash_pay_signal", "fertility_services", "iui_listed"])
        assert result["tier"] == "Bullseye"

    def test_case2_strong_fertility_no_cash_cannot_reach_bullseye(self):
        result = _run(["fertility_services", "iui_listed",
                       "cycle_monitoring_listed", "patient_financing_visible"])
        assert result["tier"] != "Bullseye"
        assert result["tier"] == "Needs Verification"

    def test_case3_fertility_plus_ivf_capped_at_contender(self):
        result = _run(["fertility_services", "ivf_listed"])
        assert result["tier"] == "Contender"

    def test_case3_high_fit_plus_ivf_still_capped_at_contender(self):
        # Strong fit (every positive yes) but the IVF cap holds the tier at Contender.
        result = _run(list(_PRIMARIES) + list(_REINFORCERS) + ["ivf_listed"])
        assert result["fit_signal_score"] == 100
        assert result["tier"] == "Contender"

    def test_case4_ivf_and_rei_penalized_and_manual_review(self):
        result = _run(["ivf_listed", "rei_on_staff"])
        assert result["bullseye_score"] == 0
        assert result["tier"] == "Manual Review"

    def test_ivf_rei_no_longer_excludes(self):
        # v12: IVF/REI are negative scoring signals, not a hard exclusion.
        result = _run(list(_PRIMARIES) + list(_REINFORCERS) + ["ivf_listed", "rei_on_staff"])
        assert result["tier"] != "Excluded"
        assert result["tier"] == "Contender"


class TestFemasysV12Structure:
    """Structural invariants of the v12 cartridge that scoring/tiering rely on."""

    def test_signal_id_set(self):
        ids = {s["signal_id"] for s in _load_signals()}
        assert ids == set(_ALL_IDS)

    def test_primary_weights_and_must_have_flags(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert by_id["cash_pay_signal"]["positive_weight"] == 50
        assert by_id["fertility_services"]["positive_weight"] == 35
        for sid in _PRIMARIES:
            assert by_id[sid].get("required_for_bullseye") is True
            assert not by_id[sid].get("reinforcer")

    def test_reinforcer_weights_and_flags(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert by_id["iui_listed"]["positive_weight"] == 15
        assert by_id["cycle_monitoring_listed"]["positive_weight"] == 10
        assert by_id["patient_financing_visible"]["positive_weight"] == 10
        for sid in _REINFORCERS:
            assert by_id[sid].get("reinforcer") is True
            assert not by_id[sid].get("required_for_bullseye")

    def test_negatives_penalize_and_cap_at_contender(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        for sid in _NEGATIVES:
            assert by_id[sid]["positive_weight"] == -20
            assert by_id[sid].get("cap_tier") == "Contender"
            assert not by_id[sid].get("reinforcer")

    def test_no_signal_is_exclude_if_yes(self):
        # v12 removed the IVF/REI hard exclusion; nothing routes to Excluded by signal.
        assert [s["signal_id"] for s in _load_signals() if s.get("exclude_if_yes")] == []

    def test_only_fertility_floors_at_contender(self):
        floored = [s["signal_id"] for s in _load_signals() if s.get("floor_tier")]
        assert floored == ["fertility_services"]
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert by_id["fertility_services"]["floor_tier"] == "Contender"

    def test_nothing_is_required_for_contender(self):
        assert [s["signal_id"] for s in _load_signals()
                if s.get("required_for_contender")] == []

    def test_cash_and_fertility_carry_dashboard_columns(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert by_id["cash_pay_signal"].get("column_label") == "Cash Pay"
        assert by_id["fertility_services"].get("column_label") == "Fertility"

    def test_rei_taxonomy_skip_removed_from_run_config(self):
        # Change 6: REI is no longer a pre-crawl taxonomy skip / exclusion rule.
        cfg = json.loads(_RUN_CONFIG_PATH.read_text(encoding="utf-8"))
        assert cfg.get("taxonomy_exclusion_rules") == []
        assert "rei_on_staff" not in cfg.get("active_exclusion_rules", [])


class TestFemasysV12PhraseBinding:
    """Each signal prompt must carry its word-for-word anchors, a no-inference
    guard, the three-state vocabulary, and the evidence/source instruction."""

    REQUIRED_ANCHORS = {
        "cash_pay_signal": ["cash pay", "self-pay", "CareCredit", "elective",
                            "med spa", "membership", "hormone optimization"],
        "fertility_services": ["fertility", "infertility", "trying to conceive",
                               "TTC", "ovulation induction", "preconception"],
        "iui_listed": ["IUI", "intrauterine insemination", "artificial insemination"],
        "cycle_monitoring_listed": ["cycle monitoring", "follicle monitoring",
                                    "serial ultrasound", "ovulation tracking"],
        "patient_financing_visible": ["CareCredit", "Cherry", "Sunbit",
                                      "payment plan", "financing available"],
        "ivf_listed": ["IVF", "in vitro fertilization", "embryo transfer",
                       "egg retrieval", "ICSI"],
        "rei_on_staff": ["reproductive endocrinologist", "REI",
                         "reproductive endocrinology and infertility"],
    }

    def test_prompts_contain_their_anchors(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        for sid, anchors in self.REQUIRED_ANCHORS.items():
            prompt = by_id[sid]["prompt_instruction"]
            for anchor in anchors:
                assert anchor in prompt, f"{sid} prompt missing anchor {anchor!r}"

    def test_prompts_are_three_state_no_inference(self):
        for s in _load_signals():
            prompt = s["prompt_instruction"]
            assert "not_found" in prompt
            assert "Do NOT" in prompt, f"{s['signal_id']} missing a no-inference guard"
            assert "evidence_text" in prompt and "source_url" in prompt

    def test_ivf_has_refer_vs_offer_guard(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        prompt = by_id["ivf_listed"]["prompt_instruction"].lower()
        assert "refer" in prompt and "offer" in prompt

    def test_rei_disambiguation_routes_bare_specialist_to_fertility(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        # Both the REI and the fertility prompts must carry the disambiguation rule:
        # a bare "fertility specialist" with no REI credential is fertility, not REI.
        assert "fertility specialist" in by_id["rei_on_staff"]["prompt_instruction"]
        assert "fertility specialist" in by_id["fertility_services"]["prompt_instruction"]


class TestFemasysV12CartridgeSeedParity:
    """The CLI cartridge and the API seed must carry identical signal logic, so the
    same practice scores the same in the pipeline and in the operator UI."""

    def test_cli_cartridge_matches_api_seed_signals(self):
        seed_path = (
            Path(__file__).parent.parent
            / "pipeline-api" / "seeds" / "icp_profiles" / "obgyn_femasys.json"
        )
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        cartridge_signals = _load_signals()
        keys = (
            "signal_id", "signal_label", "prompt_instruction", "positive_weight",
            "floor_tier", "cap_tier", "reinforcer", "reinforces", "exclude_if_yes",
            "required_for_bullseye", "required_for_contender", "verification_required",
            "not_found_weight", "no_weight", "column_label",
        )

        def _norm(sigs):
            return [{k: s.get(k) for k in keys} for s in sigs]

        assert _norm(cartridge_signals) == _norm(seed["signals"])
        assert seed["version"] == json.loads(_ICP_PATH.read_text(encoding="utf-8"))["version"]
