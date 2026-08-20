"""
tests/test_consolidation_visibility.py
Consolidation changes the unit the client is billed on, so the collapse has to
be visible on every surface: the run log, the run status, the roster preview,
the manifest, the CSV exports, run economics, and the review queue.

Deterministic — engine output is written to tmp dirs, no network.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = REPO_ROOT / "pipeline-api"
for path in (REPO_ROOT, _API_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import client_exports  # noqa: E402
import config  # noqa: E402
import exports  # noqa: E402
import llm_pricing  # noqa: E402
import main  # noqa: E402
import reviews  # noqa: E402
import runner  # noqa: E402
import runs  # noqa: E402
from schema import RunStatus  # noqa: E402
from ui import _consolidation_display  # noqa: E402

from output.log_writer import write_run_log  # noqa: E402

_RUN_ID = "RUN-20260820-101010-abcd"

_SUMMARY = {
    "enabled": True, "input_count": 1340, "output_count": 412,
    "merged_groups": 180, "rows_merged_away": 928, "review_pairs": 7,
    "unblocked_count": 3, "multi_location_groups": 12,
    # provider_entries is the ROW count; these two are the name parsing, so a
    # credential written where a person belongs is visible as a gap.
    "raw_provider_entries": 1512, "distinct_providers": 1340,
    "review_reasons": {"same_unit": 5, "unit_gate_block": 2},
}


def _location(rid, name="Valley Womens Health", providers=None, **over):
    record = {
        "id": rid, "record_id": rid, "practice_id": rid,
        "practice_name": name,
        "specialty": "OBGYN", "address_city": "Sacramento",
        "address_state": "CA", "address_zip": "95823",
        "address_street": "123 Main St", "address_unit": "suite 200",
        "address_street_normalized": "123 main street",
        "address_unit_normalized": "suite 200",
        "website_url": "https://valley.example", "phone": "916-555-0100",
        "bullseye_score": 88, "fit_signal_score": 88, "confidence_score": 90,
        "confidence_band": "High", "fit_confidence_status": "HIGH FIT / HIGH EVIDENCE",
        "target_tier": "Bullseye", "exclusion_status": "CLEAR",
        "enrichment_status": "complete", "source_confidence": "complete",
        "qc_status": "pending", "internal_notes": "internal only",
        "signals": [], "sales_angle": [], "call_brief": {"why_contact": "Because."},
        "providers": providers if providers is not None else [
            {"name": "Jane Smith", "credentials": ["MD"], "npi": "1111111111",
             "taxonomy_codes": [], "specialty": "OBGYN", "source_record_id": "T-1"},
            {"name": "Ann Lee", "credentials": ["DO"], "npi": "2222222222",
             "taxonomy_codes": [], "specialty": "OBGYN", "source_record_id": "T-2"},
        ],
        "provider_count": 2,
        "source_row_ids": ["T-1", "T-2"],
        "consolidation": {"rule_fired": "merged", "matched_fields": ["address", "domain"],
                          "score": 7, "merged_count": 2, "reviewed_by": "",
                          "review_candidates": []},
        "group_id": "G-abc123", "group_name": "Valley Health Group",
        "location_index": 3, "location_count": 6,
    }
    record.update(over)
    return record


def _write_run(run_directory, records, consolidation=True, canary=None, ack=None):
    run_directory.mkdir(parents=True, exist_ok=True)
    status = {
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete", "operator": "tester",
        "created_at": "2026-08-20T09:00:00+00:00",
        "completed_at": "2026-08-20T09:30:00+00:00",
        "records_input": 1340, "records_output": len(records),
    }
    if canary is not None:
        status["exclusion_canary_tripped"] = bool(canary.get("tripped"))
        status["exclusion_canary_detail"] = canary
    if ack is not None:
        status["exclusion_canary_ack"] = ack
    if consolidation:
        status.update({
            "consolidation_provider_entries": 1340,
            "consolidation_practice_locations": 412,
            "consolidation_merged_groups": 180,
            "consolidation_review_pairs": 7,
            "consolidation_multi_location_groups": 12,
        })
    (run_directory / "status.json").write_text(json.dumps(status))
    (run_directory / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": records}))
    return status


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path / "runs")
    monkeypatch.setattr(config, "OUTPUT_RUNS_PATH", tmp_path / "runs")
    return tmp_path / "runs" / _RUN_ID


def _get(path):
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        return c.get(path)


# ---------------------------------------------------------------------------
# Engine output: the run log carries the collapse
# ---------------------------------------------------------------------------

class TestRunLog:

    def test_consolidation_block_written(self, tmp_path):
        write_run_log(run_id=_RUN_ID, records=[_location("P-1")], errors=[], warnings=[],
                      input_file="in.csv", input_source_type="outscraper",
                      records_input=1340, output_dir=str(tmp_path),
                      consolidation=_SUMMARY)
        log = json.loads((tmp_path / "run_log.json").read_text())
        assert log["consolidation"] == {
            "provider_entries": 1340, "practice_locations": 412,
            "rows_merged_away": 928, "merged_groups": 180,
            "review_pairs": 7, "multi_location_groups": 12, "unblocked_count": 3,
            "raw_provider_entries": 1512, "distinct_providers": 1340,
            "review_reasons": {"same_unit": 5, "unit_gate_block": 2},
        }

    def test_absent_when_consolidation_did_not_run(self, tmp_path):
        """A pre-consolidation run stays identifiable: no block, not zeros."""
        write_run_log(run_id=_RUN_ID, records=[], errors=[], warnings=[],
                      input_file="in.csv", input_source_type="outscraper",
                      records_input=5, output_dir=str(tmp_path),
                      consolidation={"enabled": False})
        assert "consolidation" not in json.loads((tmp_path / "run_log.json").read_text())


class TestStatusPropagation:

    def test_counts_read_through_to_status(self, env, tmp_path, monkeypatch):
        env.mkdir(parents=True, exist_ok=True)
        write_run_log(run_id=_RUN_ID, records=[_location("P-1")], errors=[], warnings=[],
                      input_file="in.csv", input_source_type="outscraper",
                      records_input=1340, output_dir=str(env), consolidation=_SUMMARY)
        (env / "enriched_targets.json").write_text(
            json.dumps({"records": [_location("P-1")]}))
        counts = runner._read_completion_counts(_RUN_ID)
        assert counts["consolidation_provider_entries"] == 1340
        assert counts["consolidation_practice_locations"] == 412
        assert counts["consolidation_review_pairs"] == 7


# ---------------------------------------------------------------------------
# Roster preview shape
# ---------------------------------------------------------------------------

class TestRosterPreviewDisplay:

    def test_client_shape_numbers(self):
        display = _consolidation_display(RunStatus(
            run_id=_RUN_ID, project_id="P-1", source_type="outscraper",
            input_filename="x.csv", operator="t", status="ingested",
            created_at="2026-08-20T09:00:00+00:00",
            consolidation_provider_entries=1340,
            consolidation_practice_locations=412,
        ))
        # "1,340 provider entries -> 412 practice locations. Billable: 412."
        assert display["provider_entries_display"] == "1,340"
        assert display["practice_locations_display"] == "412"
        assert display["billable_display"] == "412"
        assert display["billable"] == display["practice_locations"] == 412
        assert display["collapsed"] == 928

    def test_none_for_a_pre_consolidation_run(self):
        assert _consolidation_display(RunStatus(
            run_id=_RUN_ID, project_id="P-1", source_type="outscraper",
            input_filename="x.csv", operator="t", status="complete",
            created_at="2026-08-20T09:00:00+00:00",
        )) is None

    def test_roster_banner_rendered(self, env):
        status = _write_run(env, [_location("P-1")])
        status["status"] = "ingested"
        (env / "status.json").write_text(json.dumps(status))
        body = _get(f"/dashboard/{_RUN_ID}").text
        assert "1,340 provider entries" in body
        assert "412 practice locations" in body
        assert "Billable: 412" in body


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class TestManifest:

    def test_manifest_reconciles_rows_to_locations(self, env):
        _write_run(env, [_location("P-1")])
        status = runs.get_run(_RUN_ID)
        manifest = json.loads(client_exports.build_run_manifest(_RUN_ID, env, status))
        block = manifest["consolidation"]
        assert block["input_provider_entries"] == 1340
        assert block["practice_locations"] == 412
        assert block["billable_practice_locations"] == 412
        assert block["rows_merged_away"] == 928
        assert block["merged_practices"] == 180
        assert block["review_queue_pairs"] == 7
        assert block["multi_location_groups"] == 12

    def test_legacy_run_has_no_consolidation_block(self, env):
        _write_run(env, [_location("P-1")], consolidation=False)
        status = runs.get_run(_RUN_ID)
        manifest = json.loads(client_exports.build_run_manifest(_RUN_ID, env, status))
        assert manifest["consolidation"] is None


# ---------------------------------------------------------------------------
# CSV exports: one row per practice location
# ---------------------------------------------------------------------------

class TestExports:

    def _rows(self, env, client_facing=True):
        _write_run(env, [_location("P-1"), _location("P-2", name="Oak Fertility")])
        csv_text = exports.build_approved_csv(
            _RUN_ID, env, client_facing=client_facing).getvalue().decode("utf-8")
        return csv_text

    def _approved(self, env, client_facing=True):
        records = [_location("P-1"), _location("P-2", name="Oak Fertility")]
        _write_run(env, records)
        (env / "reviews.json").write_text(json.dumps({
            r["id"]: {"qc_status": "approved", "override_tier": None,
                      "override_reason": None, "analyst_note": "",
                      "reviewed_by": "t", "reviewed_at": "now"} for r in records}))
        return exports.build_approved_csv(
            _RUN_ID, env, client_facing=client_facing).getvalue().decode("utf-8")

    def test_one_row_per_location_providers_never_add_rows(self, env):
        csv_text = self._approved(env)
        data_rows = [line for line in csv_text.strip().splitlines()[1:] if line.strip()]
        assert len(data_rows) == 2          # two locations, four providers

    def test_providers_rolled_into_one_cell(self, env):
        csv_text = self._approved(env)
        assert "providers_flat" in csv_text
        assert "Jane Smith, MD | Ann Lee, DO" in csv_text

    def test_group_is_legible_as_a_group(self, env):
        csv_text = self._approved(env)
        assert "location_label" in csv_text
        assert "Location 3 of 6" in csv_text
        assert "Valley Health Group" in csv_text

    def test_client_csv_hides_raw_group_keys_and_matching_artifacts(self, env):
        csv_text = self._approved(env, client_facing=True)
        header = csv_text.splitlines()[0]
        for hidden in ("group_id", "location_index", "location_count",
                       "address_street_normalized", "address_unit_normalized"):
            assert hidden not in header

    def test_scores_still_stripped_from_client_csv(self, env):
        header = self._approved(env, client_facing=True).splitlines()[0]
        for score_col in ("bullseye_score", "fit_signal_score", "confidence_score"):
            assert score_col not in header

    def test_operator_csv_keeps_the_raw_group_keys(self, env):
        header = self._approved(env, client_facing=False).splitlines()[0]
        assert "group_id" in header and "location_index" in header


# ---------------------------------------------------------------------------
# Run economics
# ---------------------------------------------------------------------------

class TestEconomics:

    def _status(self, **over):
        base = dict(
            run_id=_RUN_ID, project_id="P-1", source_type="outscraper",
            input_filename="x.csv", operator="t", status="complete",
            created_at="2026-08-20T09:00:00+00:00",
            records_input=1340, records_output=412,
            llm_input_tokens=1_000_000, llm_output_tokens=100_000, llm_call_count=412,
            consolidation_provider_entries=1340, consolidation_practice_locations=412,
        )
        base.update(over)
        return RunStatus(**base)

    def test_cost_denominator_is_practice_locations(self):
        summary = llm_pricing.cost_summary(self._status())
        assert summary["practice_locations"] == 412
        expected = round(summary["estimated_cost_usd"] / 412, 4)
        assert summary["cost_per_record_usd"] == expected

    def test_denominator_never_falls_back_to_raw_input_rows(self):
        """records_input is 1340 raw rows; the per-unit cost must not use it."""
        summary = llm_pricing.cost_summary(self._status())
        wrong = round(summary["estimated_cost_usd"] / 1340, 4)
        assert summary["cost_per_record_usd"] != wrong

    def test_pre_consolidation_runs_excluded_from_estimate_history(self, tmp_path):
        """A per-provider run would understate a per-location estimate."""
        runs_dir = tmp_path / "runs"
        legacy = runs_dir / "RUN-20260101-000000"
        legacy.mkdir(parents=True)
        (legacy / "status.json").write_text(json.dumps({
            "run_id": "RUN-20260101-000000", "status": "complete",
            "run_type": "enrichment", "records_output": 100,
            "llm_input_tokens": 600_000, "llm_output_tokens": 75_000,
        }))
        estimate = llm_pricing.estimate_run_cost(10, runs_dir)
        assert estimate["history_run_count"] == 0
        assert estimate["using_defaults"] is True


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------

class TestReviewQueue:

    def _pair_run(self, env, reason="same_unit", score=4, evidence=None):
        evidence = evidence if evidence is not None else {
            "same_unit": True, "unit_left": "suite 360", "unit_right": "suite 360",
            "domains_conflict": False, "phones_differ": True, "phone_absent": False,
            "both_organizational": True, "both_personal": False,
        }
        left = _location("P-1", name="Alpha Womens Health")
        right = _location("P-2", name="Zeta Fertility Partners")
        left["consolidation"]["review_candidates"] = [
            {"practice_id": "P-2", "score": score, "matched_fields": ["address"],
             "review_reason": reason, "evidence": evidence}]
        right["consolidation"]["review_candidates"] = [
            {"practice_id": "P-1", "score": score, "matched_fields": ["address"],
             "review_reason": reason, "evidence": evidence}]
        _write_run(env, [left, right])

    def test_queue_lists_each_pair_once(self, env):
        self._pair_run(env)
        body = _get(f"/dashboard/{_RUN_ID}/consolidation-review").text
        assert "Alpha Womens Health" in body and "Zeta Fertility Partners" in body
        assert body.count("Keep separate") == 1      # one pair, not two mirrored rows
        assert "1 of 1 judgement call" in body

    def test_admission_reason_and_evidence_are_shown(self, env):
        """The analyst is told why they are being asked, and what the engine saw."""
        self._pair_run(env)
        body = _get(f"/dashboard/{_RUN_ID}/consolidation-review").text
        assert "Same suite" in body
        assert "both at suite 360" in body
        assert "different phones" in body

    def test_unit_gate_blocks_are_a_separate_bucket_with_no_ruling(self, env):
        """Mechanical rejects must never be filed as near-match judgement calls.

        A "not close enough to merge" framing beside a Score 10 badge is the
        misleading combination this bucket exists to prevent.
        """
        self._pair_run(env, reason="unit_gate_block", score=10, evidence={
            "same_unit": False, "unit_left": "suite 360", "unit_right": "suite 400",
            "domains_conflict": False, "phones_differ": False, "phone_absent": False,
            "both_organizational": True, "both_personal": False,
        })
        body = _get(f"/dashboard/{_RUN_ID}/consolidation-review").text
        assert "1 kept apart by the suite rule" in body
        assert "suite 360 vs suite 400" in body
        assert "No judgement calls in this run." in body
        assert "Keep separate" not in body          # no ruling is asked for
        assert "0 of 0 judgement call" in body

    def test_resolution_requires_a_reason(self, env):
        self._pair_run(env)
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "tester", "password": "secret-pw"})
            r = c.post(f"/dashboard/{_RUN_ID}/consolidation-review",
                       data={"left_id": "P-1", "right_id": "P-2",
                             "decision": "separate", "reason": "  "},
                       follow_redirects=False)
        assert r.status_code == 303 and "reason%20is%20required" in r.headers["location"]
        assert reviews.get_reviews(_RUN_ID, env).get("P-1", {}).get(
            "consolidation_decision") is None

    def test_decision_is_recorded_on_both_sides(self, env):
        self._pair_run(env)
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "tester", "password": "secret-pw"})
            c.post(f"/dashboard/{_RUN_ID}/consolidation-review",
                   data={"left_id": "P-1", "right_id": "P-2",
                         "decision": "separate", "reason": "Different owners"},
                   follow_redirects=False)
        overlay = reviews.get_reviews(_RUN_ID, env)
        for rid in ("P-1", "P-2"):
            decision = overlay[rid]["consolidation_decision"]
            assert decision["decision"] == "separate"
            assert decision["reason"] == "Different owners"
            assert decision["decided_by"] == "tester"
            assert "Consolidation review" in overlay[rid]["analyst_note"]

    def test_pipeline_output_is_never_rewritten(self, env):
        self._pair_run(env)
        before = (env / "enriched_targets.json").read_text()
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "tester", "password": "secret-pw"})
            c.post(f"/dashboard/{_RUN_ID}/consolidation-review",
                   data={"left_id": "P-1", "right_id": "P-2",
                         "decision": "merge", "reason": "Same practice"},
                   follow_redirects=False)
        assert (env / "enriched_targets.json").read_text() == before

    def test_invalid_decision_rejected(self, env):
        self._pair_run(env)
        with pytest.raises(ValueError):
            reviews.save_consolidation_decision(
                _RUN_ID, "P-1", "obliterate", "why", "tester", env)


# ---------------------------------------------------------------------------
# Exclusion canary: a near-empty run cannot reach a client silently
# ---------------------------------------------------------------------------

_TRIPPED = {
    "tripped": True, "excluded": 1200, "total": 1200, "share": 1.0,
    "threshold": 0.9, "rules": {"out_of_scope_specialty": 1200},
}


class TestExclusionCanaryGate:
    """Step 6 reports rather than halts, so the block lives at the delivery edge.

    Halting after the crawl and LLM spend would destroy paid work to punish a
    config error. Blocking client delivery keeps the work and keeps the decision
    with a person.
    """

    def test_engine_writes_the_canary_to_the_run_log(self, tmp_path):
        write_run_log(run_id=_RUN_ID, records=[_location("P-1")], errors=[],
                      warnings=[], input_file="in.csv",
                      input_source_type="outscraper", records_input=1200,
                      output_dir=str(tmp_path), exclusion_canary=_TRIPPED)
        log = json.loads((tmp_path / "run_log.json").read_text())
        assert log["exclusion_canary"] == _TRIPPED

    def test_untripped_state_is_still_recorded(self, tmp_path):
        """"Not tripped" must be distinguishable from "run predates the check"."""
        clean = {**_TRIPPED, "tripped": False, "excluded": 3, "share": 0.0025}
        write_run_log(run_id=_RUN_ID, records=[_location("P-1")], errors=[],
                      warnings=[], input_file="in.csv",
                      input_source_type="outscraper", records_input=1200,
                      output_dir=str(tmp_path), exclusion_canary=clean)
        log = json.loads((tmp_path / "run_log.json").read_text())
        assert log["exclusion_canary"]["tripped"] is False

    def test_older_runs_carry_no_canary_block(self, tmp_path):
        write_run_log(run_id=_RUN_ID, records=[_location("P-1")], errors=[],
                      warnings=[], input_file="in.csv",
                      input_source_type="outscraper", records_input=1200,
                      output_dir=str(tmp_path))
        assert "exclusion_canary" not in json.loads(
            (tmp_path / "run_log.json").read_text())

    def test_client_package_is_refused_while_tripped(self, env):
        _write_run(env, [_location("P-1")], canary=_TRIPPED)
        response = _get(f"/runs/{_RUN_ID}/client-package")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "out_of_scope_specialty" in detail      # names the rule
        assert "1200 of 1200" in detail and "100.0%" in detail   # and the share

    def test_publish_is_refused_while_tripped(self, env):
        _write_run(env, [_location("P-1")], canary=_TRIPPED)
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "tester", "password": "secret-pw"})
            response = c.post(f"/runs/{_RUN_ID}/publish/sales-handoff")
        assert response.status_code == 409
        assert "out_of_scope_specialty" in response.json()["detail"]

    def test_acknowledgement_unblocks_delivery(self, env):
        # Contender, so the separate pending-Bullseye-QC guard is not in play.
        _write_run(env, [_location("P-1", target_tier="Contender")], canary=_TRIPPED)
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "tester", "password": "secret-pw"})
            c.post(f"/runs/{_RUN_ID}/acknowledge-exclusion-canary",
                   data={"reason": "Client sent an out-of-region list on purpose"},
                   follow_redirects=False)
            response = c.get(f"/runs/{_RUN_ID}/client-package")
        assert response.status_code == 200
        status = runs.get_run(_RUN_ID)
        assert status.exclusion_canary_ack["acknowledged_by"] == "tester"
        assert status.exclusion_canary_ack["reason"].startswith("Client sent")

    def test_acknowledgement_requires_a_reason(self, env):
        _write_run(env, [_location("P-1")], canary=_TRIPPED)
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "tester", "password": "secret-pw"})
            response = c.post(f"/runs/{_RUN_ID}/acknowledge-exclusion-canary",
                              data={"reason": "   "}, follow_redirects=False)
        assert response.status_code == 303
        assert "reason%20is%20required" in response.headers["location"]
        assert runs.get_run(_RUN_ID).exclusion_canary_ack is None

    def test_untripped_run_is_never_blocked(self, env):
        _write_run(env, [_location("P-1", target_tier="Contender")],
                   canary={**_TRIPPED, "tripped": False, "excluded": 3})
        assert _get(f"/runs/{_RUN_ID}/client-package").status_code == 200

    def test_run_page_offers_the_acknowledgement(self, env):
        _write_run(env, [_location("P-1")], canary=_TRIPPED)
        body = _get(f"/dashboard/{_RUN_ID}").text
        assert "Client delivery is blocked for this run." in body
        assert "Acknowledge and unblock" in body

    def test_manifest_records_the_canary_and_the_acknowledgement(self, env):
        ack = {"acknowledged_by": "tester", "acknowledged_at": "2026-08-20T10:00:00+00:00",
               "reason": "Deliberately narrow geography"}
        _write_run(env, [_location("P-1")], canary=_TRIPPED, ack=ack)
        manifest = json.loads(client_exports.build_run_manifest(
            _RUN_ID, env, runs.get_run(_RUN_ID)))
        block = manifest["exclusion_canary"]
        assert block["tripped"] is True
        assert block["rules_fired"] == {"out_of_scope_specialty": 1200}
        assert block["acknowledgement"] == ack

    def test_manifest_omits_the_block_for_older_runs(self, env):
        _write_run(env, [_location("P-1")])
        manifest = json.loads(client_exports.build_run_manifest(
            _RUN_ID, env, runs.get_run(_RUN_ID)))
        assert manifest["exclusion_canary"] is None
