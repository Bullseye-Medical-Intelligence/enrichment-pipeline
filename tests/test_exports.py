"""
Tests for the approved-export gate (exports.is_approved).

Deterministic — no filesystem, no network. Verifies which (record, review)
pairs are eligible for the client deliverable exports, with focus on the
"Needs Verification" tier: unconfirmed accounts ship only after an analyst
confirms them with an override.
"""

import os
import sys
from pathlib import Path

# pipeline-api modules import each other by bare name; put the dir on the path.
_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import exports  # noqa: E402


def _rev(qc="approved", override=None):
    return {"qc_status": qc, "override_tier": override}


def test_approved_bullseye_is_eligible():
    rec = {"target_tier": "Bullseye", "exclusion_status": "CLEAR"}
    assert exports.is_approved(rec, _rev()) is True


def test_needs_verification_not_eligible_without_override():
    rec = {"target_tier": "Needs Verification", "exclusion_status": "CLEAR"}
    assert exports.is_approved(rec, _rev()) is False


def test_needs_verification_eligible_when_overridden_to_positive_tier():
    rec = {"target_tier": "Needs Verification", "exclusion_status": "CLEAR"}
    assert exports.is_approved(rec, _rev(override="Bullseye")) is True


def test_needs_verification_not_eligible_when_not_approved():
    rec = {"target_tier": "Needs Verification", "exclusion_status": "CLEAR"}
    assert exports.is_approved(rec, _rev(qc="pending")) is False


def test_excluded_still_blocked_without_override():
    rec = {"target_tier": "Excluded", "exclusion_status": "EXCLUDED"}
    assert exports.is_approved(rec, _rev()) is False


def test_manual_review_not_eligible_without_override():
    rec = {"target_tier": "Manual Review", "exclusion_status": "CLEAR"}
    assert exports.is_approved(rec, _rev()) is False


def test_manual_review_eligible_when_overridden_to_positive_tier():
    rec = {"target_tier": "Manual Review", "exclusion_status": "CLEAR"}
    assert exports.is_approved(rec, _rev(override="Contender")) is True


def test_low_score_contender_from_floor_signal_is_eligible():
    """The engine owns the low-score floor. When it still returns Contender at a
    thin score — a confirmed floor_tier qualifier lifted the record past the
    Manual Review gate — the export layer must ship it, not re-derive it away."""
    rec = {
        "target_tier": "Contender", "exclusion_status": "CLEAR",
        "bullseye_score": 30, "enrichment_status": "enriched",
    }
    assert exports.is_approved(rec, _rev()) is True


def test_low_score_manual_review_from_engine_still_blocked():
    """A thin record with no floor signal is tiered Manual Review by the engine,
    and the approved gate blocks it on that tier — not on the score."""
    rec = {
        "target_tier": "Manual Review", "exclusion_status": "CLEAR",
        "bullseye_score": 30, "enrichment_status": "enriched",
    }
    assert exports.is_approved(rec, _rev()) is False


def test_high_score_enriched_contender_still_eligible():
    """An ordinary score-driven Contender is eligible."""
    rec = {
        "target_tier": "Contender", "exclusion_status": "CLEAR",
        "bullseye_score": 70, "enrichment_status": "enriched",
    }
    assert exports.is_approved(rec, _rev()) is True


# ---------------------------------------------------------------------------
# Evidence columns — the proof behind a tier, flattened for CRM import
# ---------------------------------------------------------------------------

def _signal(sid, label, state="yes", weight=10, quote="Self-pay pricing listed.",
            url="https://practice.example/billing", inferred=False):
    return {
        "signal_id": sid, "signal_label": label, "signal_state": state,
        "evidence_text": quote, "source_url": url,
        "positive_weight": weight, "state_inferred": inferred,
    }


def _record_with(signals, date_enriched="2026-08-20"):
    return {"practice_name": "Acme", "date_enriched": date_enriched, "signals": signals}


def test_evidence_cells_carry_claim_quote_source_and_date():
    cells = exports.evidence_cells(_record_with([_signal("S-01", "Cash-pay")]))
    assert cells["signal_1_id"] == "S-01"
    assert cells["signal_1_claim"] == "Cash-pay"
    assert cells["signal_1_quote"] == "Self-pay pricing listed."
    assert cells["signal_1_source_url"] == "https://practice.example/billing"
    assert cells["signal_1_captured"] == "2026-08-20"


def test_unused_slots_are_blank_strings_not_missing_keys():
    """A CRM import needs a stable header: every slot is always a column."""
    cells = exports.evidence_cells(_record_with([_signal("S-01", "Cash-pay")]))
    for slot in (2, 3):
        for suffix in ("id", "claim", "quote", "source_url", "captured"):
            assert cells[f"signal_{slot}_{suffix}"] == ""


def test_record_with_no_confirmed_signals_yields_all_blanks():
    cells = exports.evidence_cells(_record_with([_signal("S-01", "X", state="not_found")]))
    assert all(value == "" for value in cells.values())


def test_inferred_signal_never_fills_a_quote_cell():
    """An inferred signal was reasoned from a proxy and has no verbatim text.
    Quoting one would invent evidence that was never on the page."""
    sig = _signal("S-01", "Cash-pay", state="not_found", inferred=True)
    sig["evidence_text"] = "Inferred from elective procedures."
    assert all(v == "" for v in exports.evidence_cells(_record_with([sig])).values())


def test_signal_without_a_real_source_url_is_excluded():
    """A claim with no page behind it is not evidence a client can check."""
    for bad_url in ("", "not_found", "n/a"):
        sig = _signal("S-01", "Cash-pay", url=bad_url)
        cells = exports.evidence_cells(_record_with([sig]))
        assert cells["signal_1_id"] == "", f"admitted source_url {bad_url!r}"


def test_signal_without_a_quote_is_excluded():
    cells = exports.evidence_cells(_record_with([_signal("S-01", "Cash-pay", quote="  ")]))
    assert cells["signal_1_id"] == ""


def test_slots_fill_by_descending_weight_then_id():
    """Deterministic ordering: two exports of one record never disagree."""
    cells = exports.evidence_cells(_record_with([
        _signal("S-03", "Light", weight=5),
        _signal("S-01", "Heavy", weight=90),
        _signal("S-02", "Medium", weight=40),
    ]))
    assert [cells["signal_1_id"], cells["signal_2_id"], cells["signal_3_id"]] == \
        ["S-01", "S-02", "S-03"]


def test_only_the_top_three_confirmed_signals_are_carried():
    cells = exports.evidence_cells(_record_with(
        [_signal(f"S-0{n}", f"Sig {n}", weight=100 - n) for n in range(1, 6)]))
    assert cells["signal_3_id"] == "S-03"
    assert "signal_4_id" not in cells


def test_long_quote_is_truncated_on_a_word_boundary_and_marked():
    """Salesforce and HubSpot truncate silently on import; an unmarked cut would
    misrepresent the practice as having said only that much."""
    long_quote = ("The practice offers comprehensive self-pay pricing options " * 12).strip()
    cells = exports.evidence_cells(_record_with([_signal("S-01", "X", quote=long_quote)]))
    quote = cells["signal_1_quote"]
    assert len(quote) <= exports.MAX_QUOTE_CHARS + 1     # +1 for the ellipsis
    assert quote.endswith("…")
    assert not quote[:-1].endswith(" ")                  # cut on a word boundary


def test_short_quote_is_never_marked_as_truncated():
    cells = exports.evidence_cells(_record_with([_signal("S-01", "X", quote="Short quote.")]))
    assert cells["signal_1_quote"] == "Short quote."


def test_quote_whitespace_is_collapsed_for_a_single_cell():
    """Crawled text carries newlines; a raw newline breaks the CSV row."""
    cells = exports.evidence_cells(
        _record_with([_signal("S-01", "X", quote="Line one.\n\n  Line two.")]))
    assert cells["signal_1_quote"] == "Line one. Line two."


def test_formula_injection_guard_covers_quote_cells():
    """A crawled quote is untrusted text and lands in a spreadsheet."""
    assert exports._escape_csv_cell("=cmd|'/c calc'!A1").startswith("'")
    assert exports._escape_csv_cell("+1 800 CALL") .startswith("'")
    assert exports._escape_csv_cell("Normal quote.") == "Normal quote."


# ---------------------------------------------------------------------------
# Territory grouping — ordering only, never a proximity claim
# ---------------------------------------------------------------------------

def _loc(name, state, city, zip_code):
    return {"practice_name": name, "address_state": state,
            "address_city": city, "address_zip": zip_code}


def test_rows_group_by_state_then_city_then_zip():
    ordered = exports._territory_sorted([
        _loc("D", "TX", "Austin", "78702"),
        _loc("B", "CA", "Sacramento", "95823"),
        _loc("C", "TX", "Austin", "78701"),
        _loc("A", "CA", "Davis", "95616"),
    ])
    assert [r["practice_name"] for r in ordered] == ["A", "B", "C", "D"]


def test_ordering_is_total_and_stable_on_identical_locations():
    """Byte-stable exports: same run, same file, every time."""
    rows = [_loc("Zeta", "CA", "Davis", "95616"), _loc("Alpha", "CA", "Davis", "95616")]
    assert [r["practice_name"] for r in exports._territory_sorted(rows)] == ["Alpha", "Zeta"]
    assert exports._territory_sorted(rows) == exports._territory_sorted(list(reversed(rows)))


def test_rows_missing_a_state_sort_last():
    """A location-less row must not interleave into a rep's territory block."""
    ordered = exports._territory_sorted([
        _loc("NoState", "", "", ""),
        _loc("Real", "CA", "Davis", "95616"),
    ])
    assert [r["practice_name"] for r in ordered] == ["Real", "NoState"]


def test_case_and_padding_do_not_split_a_territory():
    ordered = exports._territory_sorted([
        _loc("B", "ca", " davis ", "95616"),
        _loc("A", "CA", "Davis", "95616"),
    ])
    assert [r["practice_name"] for r in ordered] == ["A", "B"]
