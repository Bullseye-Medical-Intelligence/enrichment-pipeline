"""
tests/test_eval_preflight.py

Unit tests for eval_signals.preflight_golden — the dataset gate that runs
before any live extractor call.

Production mode enforces the full LABELING_SOP.md contract (exactly 20
reviewed cases, exact ICP key coverage, valid values, >=4 labeled-yes per
signal, rubric version, page fingerprint, anchored yes/no labels, nonempty
page.txt). Dev mode keeps only the schema checks. Deterministic — no API.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import eval_signals  # noqa: E402

_RUBRIC = "femaseed-rubric-v1"
_PAGE = "Welcome. We offer IUI in office. Self-pay pricing available. No IVF is performed here."
_ANCHORS = {
    "s1": "We offer IUI in office",
    "s2": "Self-pay pricing available",
    "s3": "No IVF is performed here",
}
_ICP = [
    {"signal_id": "s1", "signal_label": "IUI", "prompt_instruction": "?",
     "positive_weight": 40, "required_for_bullseye": True},
    {"signal_id": "s2", "signal_label": "Cash pay", "prompt_instruction": "?",
     "positive_weight": 30},
    {"signal_id": "s3", "signal_label": "IVF", "prompt_instruction": "?",
     "positive_weight": -20},
]


def _write_case(golden: Path, name: str, expected: dict, reviewed: bool = True,
                rubric: str = _RUBRIC, page: str = _PAGE,
                fingerprint: str | None = None, anchors: dict | None = None,
                raw_labels_text: str | None = None) -> Path:
    case = golden / name
    case.mkdir(parents=True)
    (case / "page.txt").write_text(page)
    if raw_labels_text is not None:
        (case / "labels.json").write_text(raw_labels_text)
        return case
    labels = {
        "practice_name": name,
        "reviewed": reviewed,
        "rubric_version": rubric,
        "page_sha256": fingerprint if fingerprint is not None
        else eval_signals.page_fingerprint(page),
        "anchors": anchors if anchors is not None
        else {sid: _ANCHORS[sid] for sid, st in expected.items() if st in ("yes", "no")},
        "expected": expected,
    }
    (case / "labels.json").write_text(json.dumps(labels, indent=2))
    return case


def _production_set(golden: Path, count: int = 20) -> None:
    """A dataset satisfying the full production gate: `count` reviewed cases,
    every ICP key covered, >=4 yes for s1/s2 and >=4 no... s3 yes via 4 cases."""
    for i in range(count):
        if i < 4:
            expected = {"s1": "yes", "s2": "yes", "s3": "yes"}
        elif i < 8:
            expected = {"s1": "yes", "s2": "yes", "s3": "no"}
        else:
            expected = {"s1": "not_found", "s2": "not_found", "s3": "not_found"}
        _write_case(golden, f"case_{i:02d}", expected)


def _preflight(golden: Path, production: bool = True) -> list[str]:
    cases = eval_signals.discover_cases(golden)
    return eval_signals.preflight_golden(cases, _ICP, production=production)


# ---------------------------------------------------------------------------
# Production gate
# ---------------------------------------------------------------------------

def test_complete_production_set_passes(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    assert _preflight(golden) == []


def test_wrong_case_count_fails(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden, count=19)
    violations = _preflight(golden)
    assert any("reviewed case count is 19" in v and "exactly 20" in v for v in violations)


def test_unreviewed_draft_fails_production(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    _write_case(golden, "zz_draft", {"s1": "yes", "s2": "yes", "s3": "not_found"},
                reviewed=False)
    violations = _preflight(golden)
    assert any("zz_draft" in v and "unreviewed draft" in v for v in violations)


def test_missing_icp_signal_key_fails(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    # Drop s3 from one case — an incomplete label set.
    labels_path = golden / "case_00" / "labels.json"
    labels = json.loads(labels_path.read_text())
    del labels["expected"]["s3"]
    labels_path.write_text(json.dumps(labels))
    violations = _preflight(golden)
    assert any("case_00" in v and "missing ICP signal(s): s3" in v for v in violations)


def test_unknown_obsolete_signal_key_fails(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    labels_path = golden / "case_00" / "labels.json"
    labels = json.loads(labels_path.read_text())
    labels["expected"]["old_renamed_signal"] = "yes"
    labels_path.write_text(json.dumps(labels))
    violations = _preflight(golden)
    assert any("case_00" in v and "unknown/obsolete" in v
               and "old_renamed_signal" in v for v in violations)


def test_duplicate_expected_key_fails(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    raw = json.dumps({
        "practice_name": "dup", "reviewed": True, "rubric_version": _RUBRIC,
        "page_sha256": eval_signals.page_fingerprint(_PAGE),
        "anchors": {"s1": _ANCHORS["s1"]},
        "expected": {"s1": "yes", "s2": "not_found", "s3": "not_found"},
    })
    # Inject a literal duplicate key into the expected object.
    raw = raw.replace('"expected": {"s1": "yes"', '"expected": {"s1": "no", "s1": "yes"')
    # Replace one valid case so the count stays 20.
    case = golden / "case_19"
    (case / "labels.json").write_text(raw)
    violations = _preflight(golden)
    assert any("case_19" in v and "duplicate key(s)" in v and "s1" in v for v in violations)


def test_invalid_expected_value_fails(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    labels_path = golden / "case_00" / "labels.json"
    labels = json.loads(labels_path.read_text())
    labels["expected"]["s2"] = "Yes"  # wrong case — not a valid state literal
    labels_path.write_text(json.dumps(labels))
    violations = _preflight(golden)
    assert any("case_00" in v and "invalid expected value" in v and "'Yes'" in v
               for v in violations)


def test_too_few_yes_examples_fails_naming_signal(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    # Demote one of s3's four yes labels: 3 < 4 minimum.
    labels_path = golden / "case_00" / "labels.json"
    labels = json.loads(labels_path.read_text())
    labels["expected"]["s3"] = "not_found"
    del labels["anchors"]["s3"]
    labels_path.write_text(json.dumps(labels))
    violations = _preflight(golden)
    assert any("signal s3 has only 3 labeled-yes" in v for v in violations)


def test_missing_rubric_version_fails(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    labels_path = golden / "case_00" / "labels.json"
    labels = json.loads(labels_path.read_text())
    labels["rubric_version"] = ""
    labels_path.write_text(json.dumps(labels))
    violations = _preflight(golden)
    assert any("case_00" in v and "rubric_version" in v for v in violations)


def test_mixed_rubric_versions_fail(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    labels_path = golden / "case_00" / "labels.json"
    labels = json.loads(labels_path.read_text())
    labels["rubric_version"] = "femaseed-rubric-v0-draft"
    labels_path.write_text(json.dumps(labels))
    violations = _preflight(golden)
    assert any("mixed rubric_version" in v for v in violations)


def test_missing_fingerprint_fails(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    labels_path = golden / "case_00" / "labels.json"
    labels = json.loads(labels_path.read_text())
    del labels["page_sha256"]
    labels_path.write_text(json.dumps(labels))
    violations = _preflight(golden)
    assert any("case_00" in v and "page_sha256" in v for v in violations)


def test_fingerprint_mismatch_fails(tmp_path):
    """page.txt edited after labeling must invalidate the case."""
    golden = tmp_path / "golden"
    _production_set(golden)
    (golden / "case_00" / "page.txt").write_text(_PAGE + " Newly added sentence.")
    violations = _preflight(golden)
    assert any("case_00" in v and "changed after labeling" in v for v in violations)


def test_yes_without_anchor_fails(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    labels_path = golden / "case_00" / "labels.json"
    labels = json.loads(labels_path.read_text())
    del labels["anchors"]["s1"]
    labels_path.write_text(json.dumps(labels))
    violations = _preflight(golden)
    assert any("case_00" in v and "s1=yes has no verbatim anchor" in v for v in violations)


def test_no_label_also_requires_anchor(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    labels_path = golden / "case_04" / "labels.json"  # s3 == "no" in cases 4-7
    labels = json.loads(labels_path.read_text())
    labels["anchors"].pop("s3", None)
    labels_path.write_text(json.dumps(labels))
    violations = _preflight(golden)
    assert any("case_04" in v and "s3=no has no verbatim anchor" in v for v in violations)


def test_anchor_not_in_page_fails(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    labels_path = golden / "case_00" / "labels.json"
    labels = json.loads(labels_path.read_text())
    labels["anchors"]["s1"] = "we perform hysteroscopy on-site"  # not on the page
    labels_path.write_text(json.dumps(labels))
    violations = _preflight(golden)
    assert any("case_00" in v and "anchor for s1 not found in page.txt" in v
               for v in violations)


def test_anchor_matches_under_documented_normalization(tmp_path):
    """Case and whitespace differences must NOT fail — the policy is lowercase
    + collapsed whitespace, identical to the evaluator's anchor_rate check."""
    golden = tmp_path / "golden"
    _production_set(golden)
    labels_path = golden / "case_00" / "labels.json"
    labels = json.loads(labels_path.read_text())
    labels["anchors"]["s1"] = "WE   OFFER\n IUI IN     OFFICE"
    labels_path.write_text(json.dumps(labels))
    assert _preflight(golden) == []


def test_empty_page_txt_fails(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    (golden / "case_00" / "page.txt").write_text("   \n ")
    violations = _preflight(golden)
    assert any("case_00" in v and "page.txt is missing or empty" in v for v in violations)


def test_unreadable_labels_json_fails(tmp_path):
    golden = tmp_path / "golden"
    _production_set(golden)
    (golden / "case_00" / "labels.json").write_text("{ not valid json")
    violations = _preflight(golden)
    assert any("case_00" in v and "labels.json unreadable" in v for v in violations)


# ---------------------------------------------------------------------------
# Dev mode: schema checks only, everything else relaxed
# ---------------------------------------------------------------------------

def test_dev_mode_accepts_small_demo_set(tmp_path):
    golden = tmp_path / "golden"
    _write_case(golden, "demo_a", {"s1": "yes", "s2": "not_found", "s3": "not_found"})
    _write_case(golden, "demo_b", {"s1": "not_found", "s2": "yes", "s3": "not_found"})
    assert _preflight(golden, production=False) == []


def test_dev_mode_still_enforces_schema(tmp_path):
    golden = tmp_path / "golden"
    _write_case(golden, "demo_a", {"s1": "maybe", "s2": "yes"})  # bad value + missing s3
    violations = _preflight(golden, production=False)
    assert any("invalid expected value" in v for v in violations)
    assert any("missing ICP signal(s): s3" in v for v in violations)


def test_dev_mode_ignores_metadata_and_count(tmp_path):
    golden = tmp_path / "golden"
    case = golden / "bare"
    case.mkdir(parents=True)
    (case / "page.txt").write_text(_PAGE)
    (case / "labels.json").write_text(json.dumps({
        "practice_name": "bare", "reviewed": True,
        "expected": {"s1": "not_found", "s2": "not_found", "s3": "not_found"},
    }))  # no rubric_version, no fingerprint, no anchors — dev mode tolerates
    assert _preflight(golden, production=False) == []


# ---------------------------------------------------------------------------
# The shipped synthetic fixtures stay usable in dev mode
# ---------------------------------------------------------------------------

def test_shipped_demo_fixtures_pass_dev_schema_checks():
    repo = Path(__file__).resolve().parent.parent
    golden = repo / "evals" / "golden"
    cases = eval_signals.discover_cases(golden)
    assert len(cases) >= 2  # cedar_park + lone_star ship with the repo
    icp = eval_signals.load_icp_signals(
        repo / "config" / "clients" / "obgyn_femasys" / "icp_checklist.json")
    assert eval_signals.preflight_golden(cases, icp, production=False) == []
