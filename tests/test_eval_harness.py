"""
tests/test_eval_harness.py

Command-level tests for eval_signals.main(): review status is authoritative.

Only reviewed: true cases may contribute to metrics or a baseline. Drafts are
excluded from every aggregate, visible in the report, fail --check even when
they cannot execute, and can never be written into baseline.json.

Deterministic — no API calls. "Live" paths are monkeypatched; offline paths
replay recorded responses through the real validator.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import eval_signals  # noqa: E402

_PAGE = "Welcome to the clinic. We offer IUI in office. Self-pay pricing available."
_ANCHORS = {"s1": "We offer IUI in office", "s2": "Self-pay pricing available"}


def _write_icp(tmp_path: Path) -> Path:
    icp = {"signals": [
        {"signal_id": "s1", "signal_label": "IUI offered",
         "prompt_instruction": "Is IUI offered?", "positive_weight": 40,
         "required_for_bullseye": True},
        {"signal_id": "s2", "signal_label": "Cash pay",
         "prompt_instruction": "Cash pay visible?", "positive_weight": 30},
    ]}
    p = tmp_path / "icp.json"
    p.write_text(json.dumps(icp))
    return p


def _write_case(golden: Path, name: str, reviewed: bool = True,
                expected: dict | None = None, with_recording: bool = True,
                page: str = _PAGE) -> Path:
    """One golden case whose recording, replayed through the real validator,
    reproduces `expected` exactly (anchored evidence, https source)."""
    expected = expected or {"s1": "yes", "s2": "yes"}
    case = golden / name
    case.mkdir(parents=True)
    (case / "page.txt").write_text(page)
    labels = {
        "practice_name": name, "website_url": "https://example.invalid",
        "reviewed": reviewed,
        "rubric_version": "bemi-labeling-v2",
        "page_sha256": eval_signals.page_fingerprint(page),
        "anchors": {sid: _ANCHORS[sid] for sid, st in expected.items()
                    if st in ("yes", "no")},
        "expected": expected,
    }
    (case / "labels.json").write_text(json.dumps(labels, indent=2))
    if with_recording:
        signals = [
            {"signal_id": sid, "signal_state": st, "confidence": "high",
             "evidence_text": _ANCHORS.get(sid, "") if st == "yes" else "",
             "source_url": "https://example.invalid/svc" if st == "yes" else ""}
            for sid, st in expected.items()
        ]
        (case / "recorded_response.json").write_text(json.dumps({"signals": signals}))
    return case


def _passing_baseline(tmp_path: Path) -> Path:
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({
        "min_state_accuracy": 0.1, "min_must_have_recall": 0.1,
        "min_exclusion_recall": 0.1, "min_other_recall": 0.1,
        "min_yes_precision": 0.1, "min_anchor_rate": 0.1,
    }))
    return p


def _args(tmp_path: Path, golden: Path, *extra: str) -> list[str]:
    return ["--icp", str(_write_icp(tmp_path)),
            "--config", str(tmp_path / "no_config.json"),
            "--golden", str(golden),
            "--baseline", str(_passing_baseline(tmp_path)),
            *extra]


# ---------------------------------------------------------------------------
# Reviewed-only metrics + separate counts in the report
# ---------------------------------------------------------------------------

def test_unreviewed_case_excluded_from_metrics(tmp_path, capsys):
    golden = tmp_path / "golden"
    _write_case(golden, "verified", reviewed=True)
    # A draft whose labels disagree with its recording — if it leaked into the
    # metrics, state_accuracy would drop below 100%.
    _write_case(golden, "draft", reviewed=False,
                expected={"s1": "no", "s2": "no"})

    rc = eval_signals.main(_args(tmp_path, golden, "--offline"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "2 discovered, 1 reviewed, 1 excluded (unreviewed), 1 evaluated" in out
    assert "metrics over 1 reviewed case(s)" in out
    assert "state accuracy   100.0%" in out          # draft did not pollute
    assert "WARNING: 1 unreviewed case(s)" in out    # but stays visible


def test_all_unreviewed_fails_clearly(tmp_path, capsys):
    golden = tmp_path / "golden"
    _write_case(golden, "draft_a", reviewed=False)
    _write_case(golden, "draft_b", reviewed=False)

    rc = eval_signals.main(_args(tmp_path, golden, "--offline"))

    assert rc == 2
    err = capsys.readouterr().err
    assert "none are" in err and "reviewed: true" in err


def test_reviewed_but_unexecutable_set_fails_clearly(tmp_path, capsys):
    golden = tmp_path / "golden"
    _write_case(golden, "verified", reviewed=True, with_recording=False)

    rc = eval_signals.main(_args(tmp_path, golden, "--offline"))

    assert rc == 2
    assert "no reviewed case could be evaluated" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --check: unreviewed cases fail, even when they cannot execute
# ---------------------------------------------------------------------------

def test_check_fails_on_unreviewed_without_recording(tmp_path, capsys):
    golden = tmp_path / "golden"
    _write_case(golden, "verified", reviewed=True)
    # Draft with NO recorded_response.json: it cannot run offline, but --check
    # must still see and fail on it. (--dev-dataset so the small demo-sized set
    # clears the production preflight and the unreviewed gate itself is tested.)
    _write_case(golden, "draft", reviewed=False, with_recording=False)

    rc = eval_signals.main(_args(tmp_path, golden, "--offline", "--check", "--dev-dataset"))

    assert rc == 1
    assert "unreviewed case(s)" in capsys.readouterr().out


def test_check_passes_on_fully_reviewed_set(tmp_path):
    golden = tmp_path / "golden"
    _write_case(golden, "verified", reviewed=True)
    assert eval_signals.main(
        _args(tmp_path, golden, "--offline", "--check", "--dev-dataset")) == 0


# ---------------------------------------------------------------------------
# --update-baseline: refuses drafts, refuses offline
# ---------------------------------------------------------------------------

def test_update_baseline_refuses_offline(tmp_path, capsys):
    golden = tmp_path / "golden"
    _write_case(golden, "verified", reviewed=True)
    baseline = tmp_path / "new_baseline.json"

    rc = eval_signals.main([
        "--icp", str(_write_icp(tmp_path)), "--config", str(tmp_path / "nc.json"),
        "--golden", str(golden), "--baseline", str(baseline),
        "--offline", "--update-baseline"])

    assert rc == 1
    assert "requires --live" in capsys.readouterr().err
    assert not baseline.exists()  # nothing written


def test_update_baseline_refuses_unreviewed(tmp_path, capsys, monkeypatch):
    """A draft in the golden set blocks a baseline write at preflight, before
    any extractor call."""
    golden = tmp_path / "golden"
    _write_case(golden, "verified", reviewed=True)
    _write_case(golden, "draft", reviewed=False)
    baseline = tmp_path / "new_baseline.json"
    monkeypatch.setattr(eval_signals, "run_case_live", _fake_live)

    rc = eval_signals.main([
        "--icp", str(_write_icp(tmp_path)), "--config", str(tmp_path / "nc.json"),
        "--golden", str(golden), "--baseline", str(baseline),
        "--live", "--update-baseline"])

    assert rc == 2
    assert "unreviewed" in capsys.readouterr().err
    assert not baseline.exists()


def test_update_baseline_refuses_dev_dataset(tmp_path, capsys):
    golden = tmp_path / "golden"
    _write_case(golden, "verified", reviewed=True)
    baseline = tmp_path / "new_baseline.json"

    rc = eval_signals.main([
        "--icp", str(_write_icp(tmp_path)), "--config", str(tmp_path / "nc.json"),
        "--golden", str(golden), "--baseline", str(baseline),
        "--live", "--update-baseline", "--dev-dataset"])

    assert rc == 1
    assert "never valid with --dev-dataset" in capsys.readouterr().err
    assert not baseline.exists()


def _fake_live(case_dir, icp_signals, target_specialty, contact_strategy):
    """Deterministic stand-in for the extractor: echoes the case's own labels."""
    labels = json.loads((case_dir / "labels.json").read_text())
    return {sid: {"signal_id": sid, "signal_state": st, "confidence": "high",
                  "evidence_text": _ANCHORS.get(sid, "") if st == "yes" else "",
                  "source_url": "https://example.invalid/svc"}
            for sid, st in labels["expected"].items()}


def test_draft_in_production_dataset_blocks_all_live_spend(tmp_path, monkeypatch):
    """Production preflight halts a --live run BEFORE any extractor call when a
    draft is present — zero tokens spent."""
    golden = tmp_path / "golden"
    _write_case(golden, "verified", reviewed=True)
    _write_case(golden, "draft", reviewed=False)
    ran = []

    def _spy(case_dir, *a, **k):
        ran.append(case_dir.name)
        return _fake_live(case_dir, *a, **k)

    monkeypatch.setattr(eval_signals, "run_case_live", _spy)
    rc = eval_signals.main(_args(tmp_path, golden, "--live"))

    assert rc == 2
    assert ran == []  # preflight blocked the run entirely


def test_unreviewed_cases_never_spend_live_tokens_dev_mode(tmp_path, monkeypatch):
    """In explicit dev mode the run proceeds, but drafts still never reach the
    extractor — only reviewed cases spend tokens."""
    golden = tmp_path / "golden"
    _write_case(golden, "verified", reviewed=True)
    _write_case(golden, "draft", reviewed=False)
    ran = []

    def _spy(case_dir, *a, **k):
        ran.append(case_dir.name)
        return _fake_live(case_dir, *a, **k)

    monkeypatch.setattr(eval_signals, "run_case_live", _spy)
    rc = eval_signals.main(_args(tmp_path, golden, "--live", "--dev-dataset"))

    assert rc == 0
    assert ran == ["verified"]  # the draft never reached the extractor


# ---------------------------------------------------------------------------
# Scaffold drafts are born unreviewed
# ---------------------------------------------------------------------------

def test_scaffold_emits_reviewed_false(tmp_path):
    run_dir = tmp_path / "RUN-20260101-120000"
    run_dir.mkdir()
    (run_dir / "enriched_targets.json").write_text(json.dumps({"records": [{
        "id": "T-1", "practice_name": "Scaffold Clinic",
        "website_url": "https://example.invalid",
        "signals": [{"signal_id": "s1", "signal_state": "yes",
                     "confidence": "high", "evidence_text": "We offer IUI in office",
                     "source_url": "https://example.invalid/svc"}],
    }]}))
    from output.evidence_writer import write_record_evidence
    write_record_evidence(run_dir, "T-1", [{"url": "https://example.invalid", "text": _PAGE}])

    golden = tmp_path / "golden"
    icp = json.loads(_write_icp(tmp_path).read_text())["signals"]
    created, _ = eval_signals.scaffold_from_run(run_dir, golden, icp)

    assert created == ["scaffold_clinic"]
    labels = json.loads((golden / "scaffold_clinic" / "labels.json").read_text())
    assert labels["reviewed"] is False
    # Scaffolds emit the enforced schema: fingerprint computed, draft anchors
    # prefilled from the run's own evidence, rubric_version left for the human.
    page = (golden / "scaffold_clinic" / "page.txt").read_text()
    assert labels["page_sha256"] == eval_signals.page_fingerprint(page)
    assert labels["anchors"] == {"s1": "We offer IUI in office"}
    assert labels["rubric_version"] == ""
