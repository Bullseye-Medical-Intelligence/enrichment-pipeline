"""
tests/test_anchor_audit.py
Evidence Anchor Audit: audit_anchors.py (CLI logic) and the API's
/check-anchors + /anchor-audit surfaces.

Report-only guarantee under test: fabricated or drifted evidence quotes on
client-shipped tiers (Bullseye/Contender) are flagged against the Evidence
Vault; nothing is ever mutated. Deterministic — no LLM, no network (the route
test's subprocess runs the local CLI on local files).
"""

import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "pipeline-api"

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO_ROOT))

sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_API_DIR))

from audit_anchors import run_anchor_audit  # noqa: E402
from output.evidence_writer import write_record_evidence  # noqa: E402


_PAGE_ONE = "Welcome to Alpha Women's Health. We offer annual exams and ultrasound."
_PAGE_TWO = "Our services include IUI and fertility workups performed in office."


def _signal(sid, evidence, state="yes", **over):
    base = {
        "signal_id": sid, "signal_label": f"Signal {sid}", "signal_state": state,
        "evidence_text": evidence, "source_url": "https://alpha.example/services",
        "confidence": "high", "state_inferred": False,
    }
    base.update(over)
    return base


def _seed_run(run_dir: Path, records, with_vault=True):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "enriched_targets.json").write_text(
        json.dumps({"run_id": run_dir.name, "records": records}), encoding="utf-8")
    if with_vault:
        for rec in records:
            write_record_evidence(run_dir, rec["id"], [
                {"url": "https://alpha.example/", "text": _PAGE_ONE},
                {"url": "https://alpha.example/services", "text": _PAGE_TWO},
            ])


class TestAuditCli:

    def test_verbatim_quote_anchors(self, tmp_path):
        _seed_run(tmp_path, [{"id": "T-1", "signals": [
            _signal("S-1", "We offer annual exams and ultrasound.")]}])
        results = run_anchor_audit(tmp_path, [{"record_id": "T-1", "signal_ids": ["S-1"]}])
        assert results[0]["status"] == "all_anchored"
        assert results[0]["signals"][0]["classification"] == "anchored"

    def test_fabricated_quote_fails(self, tmp_path):
        _seed_run(tmp_path, [{"id": "T-1", "signals": [
            _signal("S-1", "We proudly offer cash-pay IVF packages.")]}])
        results = run_anchor_audit(tmp_path, [{"record_id": "T-1", "signal_ids": ["S-1"]}])
        assert results[0]["status"] == "has_failures"
        assert results[0]["signals"][0]["classification"] == "not_anchored"

    def test_whitespace_and_case_normalized_match(self, tmp_path):
        _seed_run(tmp_path, [{"id": "T-1", "signals": [
            _signal("S-1", "we OFFER   annual\nexams and    ultrasound.")]}])
        results = run_anchor_audit(tmp_path, [{"record_id": "T-1", "signal_ids": ["S-1"]}])
        assert results[0]["signals"][0]["classification"] == "anchored"

    def test_quote_on_later_page_found_and_named(self, tmp_path):
        """Per-page matching: a quote beyond the combined-context budget's
        first page still anchors, and the report names its page."""
        _seed_run(tmp_path, [{"id": "T-1", "signals": [
            _signal("S-1", "IUI and fertility workups performed in office")]}])
        results = run_anchor_audit(tmp_path, [{"record_id": "T-1", "signal_ids": ["S-1"]}])
        sig = results[0]["signals"][0]
        assert sig["classification"] == "anchored"
        assert sig["page"] == "page-02.txt"
        assert sig["page_url"] == "https://alpha.example/services"

    def test_empty_evidence_classified_no_quote(self, tmp_path):
        _seed_run(tmp_path, [{"id": "T-1", "signals": [_signal("S-1", "")]}])
        results = run_anchor_audit(tmp_path, [{"record_id": "T-1", "signal_ids": ["S-1"]}])
        assert results[0]["status"] == "has_failures"
        assert results[0]["signals"][0]["classification"] == "no_evidence_text"

    def test_missing_vault_is_not_auditable_never_a_pass(self, tmp_path):
        _seed_run(tmp_path, [{"id": "T-1", "signals": [
            _signal("S-1", "We offer annual exams and ultrasound.")]}],
            with_vault=False)
        results = run_anchor_audit(tmp_path, [{"record_id": "T-1", "signal_ids": ["S-1"]}])
        assert results[0]["status"] == "not_auditable"
        assert results[0]["signals"] == []

    def test_unknown_record_is_not_auditable(self, tmp_path):
        _seed_run(tmp_path, [{"id": "T-1", "signals": []}])
        results = run_anchor_audit(tmp_path, [{"record_id": "T-GONE", "signal_ids": ["S-1"]}])
        assert results[0]["status"] == "not_auditable"

    def test_only_requested_signals_audited(self, tmp_path):
        _seed_run(tmp_path, [{"id": "T-1", "signals": [
            _signal("S-1", "We offer annual exams and ultrasound."),
            _signal("S-2", "fabricated"),
        ]}])
        results = run_anchor_audit(tmp_path, [{"record_id": "T-1", "signal_ids": ["S-1"]}])
        assert results[0]["status"] == "all_anchored"
        assert [s["signal_id"] for s in results[0]["signals"]] == ["S-1"]


# ---------------------------------------------------------------------------
# API surfaces
# ---------------------------------------------------------------------------

_RUN_ID = "RUN-20260817-100000-aaaa"


def _record(rid, tier, signals):
    return {
        "id": rid, "practice_name": f"Practice {rid}", "target_tier": tier,
        "bullseye_score": 85, "exclusion_status": "CLEAR",
        "enrichment_status": "complete", "source_confidence": "complete",
        "signals": signals,
    }


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    import runs
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path)
    run_directory = tmp_path / _RUN_ID
    run_directory.mkdir()
    (run_directory / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-08-17T10:00:00+00:00", "operator": "tester",
    }))
    return run_directory


@pytest.fixture
def client(run_env):
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app, follow_redirects=False) as c:
        r = c.post("/login", data={"username": "tester", "password": "secret-pw"})
        assert r.status_code in (200, 302, 303)
        yield c


class TestWorklistEligibility:

    def test_worklist_scope(self):
        import ui
        records = [
            {**_record("T-1", "Bullseye", [
                _signal("S-1", "quote a"),
                _signal("S-2", "", state="not_found"),          # not "yes"
                _signal("S-3", "quote c", state_inferred=True), # inferred
                {**_signal("S-4", "operator quote"), "is_override": True},
            ]), "displayed_tier": "Bullseye", "record_id": "T-1"},
            {**_record("T-2", "Needs Verification", [_signal("S-1", "q")]),
             "displayed_tier": "Needs Verification", "record_id": "T-2"},
            {**_record("T-3", "Contender", [_signal("S-1", "q")]),
             "displayed_tier": "Contender", "record_id": "T-3"},
        ]
        worklist, skipped = ui._build_anchor_worklist(records)
        by_id = {w["record_id"]: w for w in worklist}
        assert set(by_id) == {"T-1", "T-3"}      # shipped tiers only
        assert by_id["T-1"]["signal_ids"] == ["S-1"]  # yes, direct, not overridden
        assert skipped == 1


class TestAnchorAuditRoutes:

    def _seed(self, run_env, evidence_ok=True):
        good = _record("T-1", "Bullseye", [
            _signal("S-1", "We offer annual exams and ultrasound.")])
        bad = _record("T-2", "Contender", [
            _signal("S-1", "Completely invented cash-pay promise.")])
        (run_env / "enriched_targets.json").write_text(json.dumps(
            {"run_id": _RUN_ID, "records": [good, bad]}), encoding="utf-8")
        if evidence_ok:
            for rid in ("T-1", "T-2"):
                write_record_evidence(run_env, rid, [
                    {"url": "https://alpha.example/", "text": _PAGE_ONE}])

    def test_audit_end_to_end_writes_report_and_renders(self, client, run_env):
        self._seed(run_env)
        r = client.post(f"/runs/{_RUN_ID}/check-anchors")
        assert r.status_code == 303

        report = json.loads(
            (run_env / "anchor_audit_report.json").read_text(encoding="utf-8"))
        assert report["records_audited"] == 2
        assert report["signals_failed"] == 1
        by_id = {row["record_id"]: row for row in report["results"]}
        assert by_id["T-1"]["status"] == "all_anchored"
        assert by_id["T-2"]["status"] == "has_failures"

        page = client.get(f"/dashboard/{_RUN_ID}/anchor-audit")
        assert page.status_code == 200
        assert "Practice T-2" in page.text          # flagged row shown
        assert "NOT ANCHORED" in page.text
        assert "Practice T-1" not in page.text      # clean record not listed

    def test_audit_is_report_only(self, client, run_env):
        self._seed(run_env)
        before = (run_env / "enriched_targets.json").read_bytes()
        client.post(f"/runs/{_RUN_ID}/check-anchors")
        assert (run_env / "enriched_targets.json").read_bytes() == before

    def test_missing_vault_reported_not_auditable(self, client, run_env):
        self._seed(run_env, evidence_ok=False)
        r = client.post(f"/runs/{_RUN_ID}/check-anchors")
        assert r.status_code == 303
        report = json.loads(
            (run_env / "anchor_audit_report.json").read_text(encoding="utf-8"))
        assert report["records_not_auditable"] == 2
        page = client.get(f"/dashboard/{_RUN_ID}/anchor-audit")
        assert "NOT AUDITABLE" in page.text

    def test_incomplete_run_refused(self, client, run_env):
        (run_env / "status.json").write_text(json.dumps({
            "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
            "input_filename": "x.csv", "status": "running",
            "created_at": "2026-08-17T10:00:00+00:00", "operator": "tester",
        }))
        r = client.post(f"/runs/{_RUN_ID}/check-anchors")
        assert r.status_code == 409

    def test_page_before_first_audit_offers_run(self, client, run_env):
        self._seed(run_env)
        page = client.get(f"/dashboard/{_RUN_ID}/anchor-audit")
        assert page.status_code == 200
        assert "No anchor audit has been run yet" in page.text
