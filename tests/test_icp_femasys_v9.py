"""
test_icp_femasys_v9.py

Regression tests that encode the client-confirmed Femasys (FemaSeed) ICP intent
(now the national v11 merge). They drive the SHIPPED cartridge
(config/clients/obgyn_femasys/icp_checklist.json) through simulate() — the same
function the API and the pipeline use, composing reinforcement + scoring +
tiering + exclusion — so the tests guard the real config, not a synthetic fixture.

Design intent under test (v11 — national merge of the prior national v10 and the
Michigan v10-MI cartridge):
  - Fit is soft on fertility: nothing is required_for_bullseye; any fertility
    activity scores, and the fertility signals bind on SERVICE CONTEXT (a named
    service, a service/fertility page, a 'we treat/offer' statement, or a provider
    specialty) — not only verbatim phrases.
  - Cash-pay (S-ICP-007) is now a GATE: positive_weight 20 + required_for_contender.
    A practice with no confirmed cash-pay/self-pay capability is routed to Manual
    Review (held out of every call tier) — UNLESS the elective/aesthetic proxy
    (S-ICP-008) infers it, which suppresses the gate.
  - Exclusions are load-bearing: IVF/REI fertility centers are dropped (with a
    refer-vs-offer guard so community OBGYNs who merely refer out are not excluded).
  - FemVue / FemaSeed (the warm-lead signal) floors a confirmed practice at Contender.
  - The cartridge is national: target_geography is empty in run_config.

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
    """Load the shipped Femasys v11 signal list from the cartridge on disk."""
    data = json.loads(_ICP_PATH.read_text(encoding="utf-8"))
    return data["signals"]


def _states(yes_ids, confidence="high") -> dict:
    """Build a signal_states map marking the given signal_ids as 'yes'."""
    return {sid: {"state": "yes", "confidence": confidence} for sid in yes_ids}


def _run(states, confidence="high") -> dict:
    """Simulate the shipped v11 ICP with the given signal states.

    Accepts either a list of signal_ids to mark 'yes', or a full signal_states map.
    """
    if isinstance(states, (list, tuple)):
        states = _states(states, confidence)
    return simulate(_load_signals(), states, _BULLSEYE_MIN)


class TestFemasysV11Intent:
    """End-to-end tier outcomes for the shipped Femasys cartridge (v11 national merge)."""

    def test_version_is_v11(self):
        """The shipped cartridge is the v11 national merge."""
        data = json.loads(_ICP_PATH.read_text(encoding="utf-8"))
        assert data["version"] == "obgyn-femasys-v11"

    def test_readiness_alone_cannot_qualify(self):
        """Elective (which infers cash-pay) with NO fertility signal stays out of the
        call queue.

        The elective proxy satisfies the cash-pay gate by inference, but it carries
        zero fit weight, so a readiness-only practice has too little fit to clear the
        Manual Review floor and can never reach a client tier.
        """
        result = _run(["S-ICP-008"])
        assert result["tier"] not in _TOP_TIERS
        assert result["tier"] == "Manual Review"

    def test_financing_confirmed_alone_cannot_qualify(self):
        """Directly-confirmed cash-pay, with no fertility fit, cannot qualify."""
        result = _run(["S-ICP-007"])
        assert result["tier"] not in _TOP_TIERS

    def test_infertility_eval_plus_financing_reaches_top_tier(self):
        """Fertility services + confirmed cash-pay lands in a client-shipped tier."""
        result = _run(["S-ICP-003", "S-ICP-007"])
        assert result["tier"] in _TOP_TIERS

    def test_ivf_rei_excluded_despite_perfect_fit(self):
        """A confirmed IVF/REI center is Excluded even with every fit signal 'yes'."""
        all_fit_plus_ivf = list(_FIT_SIGNAL_IDS) + ["S-ICP-007", "S-ICP-009"]
        result = _run(all_fit_plus_ivf)
        assert result["tier"] == "Excluded"

    def test_femvue_floors_at_contender(self):
        """Confirmed FemVue/FemaSeed guarantees at least Contender on a thin score —
        but the cash-pay gate must be satisfied first (floor cannot override it)."""
        result = _run(["S-ICP-006", "S-ICP-007"])
        assert result["tier"] == "Contender"

    def test_femvue_alone_is_not_bullseye(self):
        """FemVue is a warm-lead accelerator, never a one-signal path to Bullseye."""
        result = _run(["S-ICP-006", "S-ICP-007"])
        assert result["tier"] != "Bullseye"

    def test_bullseye_reachable_without_femvue(self):
        """Strong fertility fit + confirmed cash-pay reaches Bullseye on its own —
        FemVue is not required.

        This is the load-bearing invariant: a practice doing broad in-office
        fertility work (IUI + medicated cycles + fertility services + cash-pay +
        cycle monitoring) must reach Bullseye with NO FemVue mention.
        """
        result = _run(["S-ICP-001", "S-ICP-002", "S-ICP-003",
                       "S-ICP-007", "S-ICP-005"])
        assert result["tier"] == "Bullseye"

    def test_broad_fit_plus_femvue_can_reach_bullseye(self):
        """Broad fertility fit plus cash-pay plus the FemVue warm lead reaches Bullseye."""
        result = _run(list(_FIT_SIGNAL_IDS) + ["S-ICP-007"])
        assert result["tier"] == "Bullseye"


class TestFemasysV11CashPayGate:
    """v11 makes cash-pay a must-have-or-Manual-Review qualifier (required_for_contender)."""

    def test_cash_pay_signal_is_the_gate(self):
        """S-ICP-007 carries positive_weight 20 and required_for_contender true."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        cash = by_id["S-ICP-007"]
        assert cash["positive_weight"] == 20
        assert cash.get("required_for_contender") is True

    def test_strong_fit_without_cash_is_manual_review(self):
        """A strong fertility practice with cash-pay not_found is held in Manual Review
        regardless of score — the gate binds even above the score floor."""
        result = _run(["S-ICP-001", "S-ICP-002", "S-ICP-003", "S-ICP-005"])
        assert result["tier"] == "Manual Review"
        assert "required to qualify" in result["tier_cap_reason"]

    def test_strong_fit_with_cash_no_is_manual_review(self):
        """Cash-pay confirmed 'no' also fails the qualifier gate -> Manual Review."""
        states = _states(["S-ICP-001", "S-ICP-002", "S-ICP-003", "S-ICP-005"])
        states["S-ICP-007"] = {"state": "no", "confidence": "high"}
        result = _run(states)
        assert result["tier"] == "Manual Review"

    def test_elective_proxy_satisfies_cash_pay_gate(self):
        """The elective/aesthetic proxy (S-ICP-008) infers cash-pay and suppresses the
        gate, so a strong-fit practice qualifies without explicit cash-pay copy."""
        result = _run(["S-ICP-001", "S-ICP-002", "S-ICP-003", "S-ICP-005", "S-ICP-008"])
        assert result["tier"] in _TOP_TIERS

    def test_confirmed_cash_pay_satisfies_gate(self):
        """Explicit cash-pay confirmed 'yes' satisfies the gate; a strong-fit practice
        reaches a client tier."""
        result = _run(["S-ICP-001", "S-ICP-002", "S-ICP-003", "S-ICP-005", "S-ICP-007"])
        assert result["tier"] in _TOP_TIERS


class TestFemasysV11PhraseBinding:
    """Fit-signal prompts must carry their phrase anchors and a negative guard.

    v11 binds on SERVICE CONTEXT (a named service, a dedicated service page, a 'we
    treat/offer' statement, or a provider specialty) rather than strict verbatim
    phrases, so a 'Fertility' service-menu item is detected. The negative guard is
    editorial — a term appearing ONLY in a blog / patient-education article does not
    qualify. These config-time tests guard the prompt text; the LLM enforces it at
    runtime.
    """

    REQUIRED_ANCHORS = {
        "S-ICP-001": ["IUI", "intrauterine insemination",
                      "artificial insemination", "in-office insemination"],
        "S-ICP-002": ["medicated cycle", "ovulation induction"],
        # S-ICP-003 v11: Michigan broad service-context language.
        "S-ICP-003": ["infertility evaluation", "hysterosalpingogram", "HSG",
                      "fertility care", "fertility services", "PCOS treatment",
                      "recurrent pregnancy loss", "anovulation", "Family Building"],
        "S-ICP-004": ["infertility consultation", "fertility consultation",
                      "infertility counseling", "fertility consultations"],
        # S-ICP-005 v11: Michigan expanded ultrasound phrases.
        "S-ICP-005": ["follicle monitoring", "follicular monitoring", "cycle monitoring",
                      "saline sonohysterogram", "sonohysterogram", "hysterosonography"],
        # S-ICP-006 v11: FemVue OR FemaSeed.
        "S-ICP-006": ["FemVue", "FemaSeed"],
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

    def test_fertility_prompts_bind_to_service_context(self):
        """v11: every fertility fit prompt must require service context and exclude
        blog/editorial mentions."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        for sid in ("S-ICP-001", "S-ICP-002", "S-ICP-003", "S-ICP-004"):
            prompt = by_id[sid]["prompt_instruction"].lower()
            assert "not_found" in prompt
            assert "service" in prompt and "blog" in prompt, \
                f"{sid} prompt missing service-context / editorial binding"

    def test_cash_pay_signal_contains_plain_english_terms(self):
        """S-ICP-007 must contain plain-English self-pay language and merged
        Michigan financing terms."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        prompt = by_id["S-ICP-007"]["prompt_instruction"]
        for phrase in ("self-pay", "cash pay", "financing available", "payment plan",
                       "Ally Lending", "PatientFi"):
            assert phrase in prompt, f"S-ICP-007 prompt missing phrase: {phrase!r}"


class TestFemasysV11Structure:
    """Structural invariants of the cartridge that downstream logic relies on."""

    def test_ivf_rei_is_the_only_exclude_if_yes(self):
        """The IVF/REI signal is the sole exclude_if_yes route to Excluded."""
        excluders = [s["signal_id"] for s in _load_signals()
                     if s.get("exclude_if_yes")]
        assert excluders == ["S-ICP-009"]

    def test_ivf_rei_has_refer_vs_offer_guard(self):
        """v11: the IVF/REI exclusion carries the Michigan refer-vs-offer guard so
        community OBGYNs who merely refer out are not excluded."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        prompt = by_id["S-ICP-009"]["prompt_instruction"].lower()
        assert "refer" in prompt and "offer" in prompt

    def test_femvue_carries_floor_tier_contender(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert by_id["S-ICP-006"].get("floor_tier") == "Contender"

    def test_femvue_matches_femaseed(self):
        """v11: S-ICP-006 also matches FemaSeed (the strongest warm lead)."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert "FemaSeed" in by_id["S-ICP-006"]["prompt_instruction"]
        assert "FemaSeed" in by_id["S-ICP-006"]["signal_label"]

    def test_elective_reinforces_cash_pay_target(self):
        by_id = {s["signal_id"]: s for s in _load_signals()}
        assert by_id["S-ICP-008"].get("reinforces") == "S-ICP-007"
        assert by_id["S-ICP-008"]["positive_weight"] == 0

    def test_elective_brand_list_expanded(self):
        """v11: S-ICP-008 adopts the Michigan fuller brand list."""
        by_id = {s["signal_id"]: s for s in _load_signals()}
        prompt = by_id["S-ICP-008"]["prompt_instruction"]
        for brand in ("Botox", "semaglutide", "labiaplasty", "med spa", "Emsella"):
            assert brand in prompt, f"S-ICP-008 prompt missing brand: {brand!r}"

    def test_s_icp_010_not_in_cartridge(self):
        """S-ICP-010 is reserved for in-house ultrasound (pending clinical confirmation)."""
        ids = {s["signal_id"] for s in _load_signals()}
        assert "S-ICP-010" not in ids, "S-ICP-010 must not be added until clinical confirmation"

    def test_signal_id_set_is_the_national_nine(self):
        """v11 keeps the national signal IDs (no Michigan S-MI-* IDs); the
        contraception signal S-ICP-011 was removed, leaving nine."""
        ids = {s["signal_id"] for s in _load_signals()}
        assert ids == {
            "S-ICP-001", "S-ICP-002", "S-ICP-003", "S-ICP-004", "S-ICP-005",
            "S-ICP-006", "S-ICP-007", "S-ICP-008", "S-ICP-009",
        }

    def test_cash_pay_is_the_only_required_for_contender(self):
        """Cash-pay (S-ICP-007) is the sole qualifier gate."""
        gated = [s["signal_id"] for s in _load_signals()
                 if s.get("required_for_contender")]
        assert gated == ["S-ICP-007"]

    def test_nothing_is_required_for_bullseye(self):
        """v11 keeps the soft-bullseye design: no signal is required_for_bullseye."""
        assert all(
            not s.get("required_for_bullseye", False) for s in _load_signals()
        )

    def test_cli_cartridge_matches_api_seed_signals(self):
        """The CLI cartridge and the API seed must carry identical signal logic.

        Two homes for the same ICP (CLI reads the cartridge, the operator UI reads
        the seeded profile); a drift between them would score the same practice two
        different ways.
        """
        seed_path = (
            Path(__file__).parent.parent
            / "pipeline-api" / "seeds" / "icp_profiles" / "obgyn_femasys.json"
        )
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        cartridge_signals = _load_signals()
        keys = (
            "signal_id", "positive_weight", "floor_tier", "cap_tier",
            "reinforces", "exclude_if_yes", "required_for_bullseye",
            "required_for_contender", "verification_required",
            "not_found_weight", "no_weight",
        )

        def _norm(sigs):
            return [{k: s.get(k) for k in keys} for s in sigs]
        assert _norm(cartridge_signals) == _norm(seed["signals"])
        assert seed["version"] == json.loads(_ICP_PATH.read_text(encoding="utf-8"))["version"]
