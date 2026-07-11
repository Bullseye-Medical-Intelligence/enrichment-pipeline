"""
API-layer tests for pipeline-api.
Deterministic — no network, no subprocess. Covers record_adapter, run_id
guard, review persistence/validation, filtered exports (including the
hard-exclusion bypass rule), auth, and basic route wiring.
"""

import csv
import io
import json
import os
import sys
from pathlib import Path

import pytest

# pipeline-api modules import each other by bare name; put the dir on the path.
_API_DIR = Path(__file__).resolve().parent.parent / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

# Configure required env BEFORE importing config-bound modules.
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(Path(__file__).resolve().parent.parent))

import record_adapter  # noqa: E402
import reviews  # noqa: E402
import exports  # noqa: E402
import runs  # noqa: E402
import auth  # noqa: E402
from schema import ReviewEdit  # noqa: E402


# ---------------------------------------------------------------------------
# record_adapter
# ---------------------------------------------------------------------------

def test_get_record_id_prefers_record_id():
    assert record_adapter.get_record_id({"record_id": "R-1", "id": "X"}) == "R-1"


def test_get_record_id_falls_back_to_id():
    assert record_adapter.get_record_id({"id": "X-9"}) == "X-9"


def test_get_record_id_missing_returns_empty():
    assert record_adapter.get_record_id({"practice_name": "Acme"}) == ""


def test_normalize_payload_wrapper_dict():
    payload = {"run_id": "R", "records": [{"id": 1}, {"id": 2}]}
    assert record_adapter.normalize_records_payload(payload) == [{"id": 1}, {"id": 2}]


def test_normalize_payload_bare_list():
    assert record_adapter.normalize_records_payload([{"id": 1}]) == [{"id": 1}]


def test_normalize_payload_junk_returns_empty():
    assert record_adapter.normalize_records_payload("nonsense") == []


# ---------------------------------------------------------------------------
# run_id guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rid", [
    "RUN-20260527-143000",
    "RUN-20260527-143000-a3f9",
])
def test_valid_run_ids_accepted(rid):
    assert runs.is_valid_run_id(rid) is True


@pytest.mark.parametrize("rid", [
    "../../etc/passwd",
    "RUN-../secret",
    "RUN-2026",
    "",
    "RUN-20260527-143000-XYZZ",
])
def test_invalid_run_ids_rejected(rid):
    assert runs.is_valid_run_id(rid) is False


def test_run_dir_raises_on_traversal():
    with pytest.raises(ValueError):
        runs.run_dir("../../etc")


def test_get_run_returns_none_for_invalid_id():
    assert runs.get_run("../../etc") is None


# ---------------------------------------------------------------------------
# reviews persistence + validation
# ---------------------------------------------------------------------------

def test_override_without_reason_rejected(tmp_path):
    edit = ReviewEdit(override_tier="Bullseye", override_reason="", qc_status="approved")
    with pytest.raises(ValueError):
        reviews.save_review("RUN-20260527-143000-aaaa", "T-1", edit, "tester", tmp_path)


def test_override_with_reason_persists(tmp_path):
    edit = ReviewEdit(
        override_tier="Bullseye",
        override_reason="Website confirms target service line.",
        qc_status="approved",
    )
    saved = reviews.save_review("RUN-20260527-143000-aaaa", "T-1", edit, "tester", tmp_path)
    assert saved["override_tier"] == "Bullseye"
    assert saved["reviewed_by"] == "tester"
    assert saved["reviewed_at"]  # server-set

    # reviews.json was created and holds the entry
    stored = json.loads((tmp_path / "reviews.json").read_text())
    assert stored["T-1"]["qc_status"] == "approved"


def test_review_does_not_touch_enriched_targets(tmp_path):
    target = tmp_path / "enriched_targets.json"
    original = json.dumps({"records": [{"record_id": "T-1", "target_tier": "Contender"}]})
    target.write_text(original)

    edit = ReviewEdit(qc_status="approved")
    reviews.save_review("RUN-20260527-143000-aaaa", "T-1", edit, "tester", tmp_path)

    assert target.read_text() == original  # byte-identical


# ---------------------------------------------------------------------------
# filtered exports
# ---------------------------------------------------------------------------

def _write_run(tmp_path, records, reviews_map):
    (tmp_path / "enriched_targets.json").write_text(json.dumps({"records": records}))
    (tmp_path / "reviews.json").write_text(json.dumps(reviews_map))


def _csv_ids(buf: io.BytesIO):
    text = buf.getvalue().decode("utf-8")
    return {row["record_id"] for row in csv.DictReader(io.StringIO(text))}


def test_approved_export_includes_overridden_excluded(tmp_path):
    """EXCLUDED record with explicit analyst override_tier + approved → appears in approved CSV."""
    records = [
        {"record_id": "T-1", "target_tier": "Bullseye", "exclusion_status": "CLEAR"},
        {"record_id": "T-2", "target_tier": "Excluded", "exclusion_status": "EXCLUDED"},
        {"record_id": "T-3", "target_tier": "Contender", "exclusion_status": "CLEAR"},
    ]
    reviews_map = {
        "T-1": {"override_tier": None, "override_reason": None, "qc_status": "approved",
                "analyst_note": "", "reviewed_by": "t", "reviewed_at": "now"},
        "T-2": {"override_tier": "Bullseye", "override_reason": "looks good",
                "qc_status": "approved", "analyst_note": "", "reviewed_by": "t",
                "reviewed_at": "now"},
        "T-3": {"override_tier": None, "override_reason": None, "qc_status": "pending",
                "analyst_note": "", "reviewed_by": None, "reviewed_at": None},
    }
    _write_run(tmp_path, records, reviews_map)

    ids = _csv_ids(exports.build_approved_csv("RUN-20260527-143000-aaaa", tmp_path))
    assert "T-1" in ids   # CLEAR + approved → in
    assert "T-2" in ids   # EXCLUDED + analyst override Bullseye + approved → in
    assert "T-3" not in ids  # pending → out


def test_approved_export_blocks_excluded_without_override(tmp_path):
    """EXCLUDED record with no analyst override_tier stays out of approved CSV."""
    records = [
        {"record_id": "T-X", "target_tier": "Excluded", "exclusion_status": "EXCLUDED"},
    ]
    reviews_map = {
        "T-X": {"override_tier": None, "override_reason": None, "qc_status": "approved",
                "analyst_note": "", "reviewed_by": "t", "reviewed_at": "now"},
    }
    _write_run(tmp_path, records, reviews_map)

    ids = _csv_ids(exports.build_approved_csv("RUN-20260527-143000-aaaa", tmp_path))
    assert "T-X" not in ids  # no override → hard exclusion still blocks


def test_excluded_export_includes_excluded(tmp_path):
    records = [
        {"record_id": "T-1", "target_tier": "Bullseye", "exclusion_status": "CLEAR"},
        {"record_id": "T-4", "target_tier": "Excluded", "exclusion_status": "EXCLUDED"},
    ]
    _write_run(tmp_path, records, {})
    ids = _csv_ids(exports.build_excluded_csv("RUN-20260527-143000-aaaa", tmp_path))
    assert ids == {"T-4"}


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def test_validate_credentials_correct():
    assert auth.validate_credentials("tester", "secret-pw") is True


def test_validate_credentials_wrong_password():
    assert auth.validate_credentials("tester", "nope") is False


def test_validate_credentials_unknown_user():
    assert auth.validate_credentials("ghost", "secret-pw") is False


class _RecordingResponse:
    """Captures set_cookie kwargs for assertion."""
    def __init__(self):
        self.cookie_kwargs = None

    def set_cookie(self, **kwargs):
        self.cookie_kwargs = kwargs


def test_session_cookie_secure_follows_config(monkeypatch):
    """create_session_cookie sets Secure per the SESSION_COOKIE_SECURE flag."""
    monkeypatch.setattr(auth, "SESSION_COOKIE_SECURE", True)
    resp = _RecordingResponse()
    auth.create_session_cookie(resp, "tester")
    assert resp.cookie_kwargs["secure"] is True
    assert resp.cookie_kwargs["httponly"] is True


def test_session_cookie_not_secure_by_default(monkeypatch):
    """With the flag off, the cookie is not marked Secure (local HTTP dev)."""
    monkeypatch.setattr(auth, "SESSION_COOKIE_SECURE", False)
    resp = _RecordingResponse()
    auth.create_session_cookie(resp, "tester")
    assert resp.cookie_kwargs["secure"] is False


from ui import _friendly_error, _compute_readiness, _pending_review_count, _parse_signals_from_form  # noqa: E402


# ---------------------------------------------------------------------------
# UX helpers: _friendly_error
# ---------------------------------------------------------------------------

def test_friendly_error_none_for_empty():
    assert _friendly_error(None) is None
    assert _friendly_error("") is None


def test_friendly_error_known_patterns():
    cases = [
        ("enriched_targets.json was not written",
         "Run ended before results were written. Try re-running."),
        ("malformed json in output",
         "Pipeline output file was corrupted. Try re-running."),
        ("UnicodeEncodeError in encoder",
         "Character encoding error — check that the input CSV has no unusual characters."),
        ("No module named anthropic",
         "Pipeline environment error: a required package is missing. Contact support."),
        ("SyntaxError on line 42",
         "Pipeline code error. Contact support."),
        ("Interrupted by server restart at step 3",
         "The server was restarted while this run was in progress."),
    ]
    for raw, expected in cases:
        assert _friendly_error(raw) == expected, f"Pattern mismatch for: {raw!r}"


def test_friendly_error_pass_through_patterns():
    assert _friendly_error("Missing required columns: specialty") == "Missing required columns: specialty"
    assert _friendly_error("Too many runs in progress") == "Too many runs in progress"


def test_friendly_error_fallback_truncates():
    long_raw = "X" * 400
    result = _friendly_error(long_raw)
    assert result == "X" * 300


# ---------------------------------------------------------------------------
# UX helpers: _compute_readiness
# ---------------------------------------------------------------------------

def test_compute_readiness_needs_review():
    """Pending Bullseye blocks readiness; Contender-approved does not count toward Bullseye total."""
    records = [
        {"review": {"qc_status": "pending"}, "displayed_tier": "Bullseye"},
        {"review": {"qc_status": "approved"}, "displayed_tier": "Contender"},
    ]
    r = _compute_readiness(records)
    assert r["state"] == "needs_review"
    assert r["pending_count"] == 1
    assert r["approved_count"] == 0  # only Bullseye-approved counts


def test_compute_readiness_ready():
    """Ready when all Bullseye are approved; Contender does not affect state."""
    records = [
        {"review": {"qc_status": "approved"}, "displayed_tier": "Bullseye"},
        {"review": {"qc_status": "approved"}, "displayed_tier": "Contender"},
    ]
    r = _compute_readiness(records)
    assert r["state"] == "ready"
    assert r["approved_count"] == 1  # only Bullseye-approved counts


def test_compute_readiness_no_approved():
    records = [
        {"review": {"qc_status": "rejected"}, "displayed_tier": "Bullseye"},
    ]
    r = _compute_readiness(records)
    assert r["state"] == "no_approved"


def test_compute_readiness_ignores_pending_non_call_tiers():
    """Only Bullseye blocks readiness; pending NV / Manual Review / Excluded do not."""
    records = [
        {"review": {"qc_status": "approved"}, "displayed_tier": "Bullseye"},
        {"review": {"qc_status": "pending"}, "displayed_tier": "Needs Verification"},
        {"review": {"qc_status": "pending"}, "displayed_tier": "Manual Review"},
        {"review": {"qc_status": "pending"}, "displayed_tier": "Excluded"},
    ]
    r = _compute_readiness(records)
    assert r["state"] == "ready"
    assert r["approved_count"] == 1
    assert r["pending_count"] == 0


def test_compute_readiness_excluded_not_counted():
    """Zero Bullseye records means nothing to gate — run is ready even if only Excluded exist."""
    records = [
        {"review": {"qc_status": "approved"}, "displayed_tier": "Excluded"},
    ]
    r = _compute_readiness(records)
    assert r["state"] == "ready"
    assert r["approved_count"] == 0


def test_compute_readiness_no_approved_requires_existing_bullseye():
    """no_approved only fires when there ARE Bullseye records but none have been approved."""
    records = [
        {"review": {"qc_status": "rejected"}, "displayed_tier": "Bullseye"},
        {"review": {"qc_status": "approved"}, "displayed_tier": "Excluded"},
    ]
    r = _compute_readiness(records)
    assert r["state"] == "no_approved"


def test_pending_review_count_only_counts_bullseye(tmp_path):
    """The client-package gate counts only pending Bullseye; Contender/NV/MR/Excluded are exempt."""
    records = [
        {"record_id": "T-1", "target_tier": "Bullseye", "exclusion_status": "CLEAR"},
        {"record_id": "T-2", "target_tier": "Contender", "exclusion_status": "CLEAR"},
        {"record_id": "T-3", "target_tier": "Manual Review", "exclusion_status": "CLEAR"},
        {"record_id": "T-4", "target_tier": "Needs Verification", "exclusion_status": "CLEAR"},
        {"record_id": "T-5", "target_tier": "Excluded", "exclusion_status": "EXCLUDED"},
    ]
    _write_run(tmp_path, records, {})  # all pending by default
    # Only T-1 (Bullseye) counts toward the gate.
    assert _pending_review_count("RUN-20260527-143000-aaaa", tmp_path) == 1


def test_parse_signals_skips_blank_and_preserves_hidden_fields():
    """A removed (blank-id) row is dropped; cap_tier / exclude_if_yes survive an edit."""
    form = {
        "signal_id_0": "S-1", "signal_label_0": "Cash pay", "prompt_instruction_0": "?",
        "positive_weight_0": "25", "cap_tier_0": "", "exclude_if_yes_0": "",
        # row 1 removed by the UI: disabled inputs -> absent from form data
        "signal_id_2": "S-3", "signal_label_2": "Hospital owned", "prompt_instruction_2": "?",
        "positive_weight_2": "0", "cap_tier_2": "Contender", "exclude_if_yes_2": "1",
        "reinforces_2": "S-1", "verification_required_2": "1",
    }
    out = _parse_signals_from_form(form, signal_count=3)
    ids = [s["signal_id"] for s in out]
    assert ids == ["S-1", "S-3"]              # blank/removed row 1 dropped
    s3 = next(s for s in out if s["signal_id"] == "S-3")
    assert s3["cap_tier"] == "Contender"      # preserved
    assert s3["exclude_if_yes"] is True
    assert s3["reinforces"] == "S-1"
    assert s3["verification_required"] is True


# ---------------------------------------------------------------------------
# Project edit route
# ---------------------------------------------------------------------------

def test_project_edit_route_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_RUNS_PATH", str(tmp_path))
    monkeypatch.setenv("PROJECTS_PATH", str(tmp_path))
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as client:
        client.post("/login", data={"username": "tester", "password": "secret-pw"})
        r = client.get("/projects/nonexistent-project/edit")
        assert r.status_code != 405


# ---------------------------------------------------------------------------
# SSRF guard — redirect hops are validated, not just the initial URL
# ---------------------------------------------------------------------------

class TestSafeRedirectHandler:
    """Every redirect destination must pass the public-host SSRF guard."""

    def _handler(self):
        from ui import _SafeRedirectHandler
        return _SafeRedirectHandler()

    def _attempt(self, monkeypatch, new_url, safe):
        import ui as ui_mod
        import urllib.error
        import urllib.request
        monkeypatch.setattr(ui_mod, "_is_safe_public_url", lambda u: safe)
        req = urllib.request.Request("https://example.com/")
        handler = self._handler()
        return lambda: handler.redirect_request(
            req, None, 302, "Found", {}, new_url
        )

    def test_unsafe_redirect_blocked(self, monkeypatch):
        import urllib.error
        attempt = self._attempt(monkeypatch, "http://169.254.169.254/meta", safe=False)
        with pytest.raises(urllib.error.HTTPError):
            attempt()

    def test_safe_redirect_allowed(self, monkeypatch):
        attempt = self._attempt(monkeypatch, "https://example.org/page", safe=True)
        result = attempt()
        assert result is not None
        assert result.full_url == "https://example.org/page"

    def test_fetch_returns_empty_on_unsafe_initial_url(self, monkeypatch):
        import ui as ui_mod
        monkeypatch.setattr(ui_mod, "_is_safe_public_url", lambda u: False)
        assert ui_mod._fetch_page_text("http://127.0.0.1/") == ""


# ---------------------------------------------------------------------------
# Evidence Vault viewer helpers
# ---------------------------------------------------------------------------

class TestEvidenceVaultHelpers:
    def _write_vault(self, tmp_path, record_id="T-001"):
        import json as _json
        record_dir = tmp_path / "evidence" / record_id
        record_dir.mkdir(parents=True)
        (record_dir / "page-01.txt").write_text(
            "We proudly offer IUD insertion and contraception counseling.",
            encoding="utf-8",
        )
        (record_dir / "index.json").write_text(_json.dumps([
            {"url": "https://a.example/services", "file": "page-01.txt",
             "fetched_at": "2026-06-10T12:00:00+00:00", "sha256": "abc",
             "chars": 60, "provenance": "crawl"},
        ]))
        return record_dir

    def test_record_dir_blocks_traversal(self, tmp_path):
        from ui import _evidence_record_dir
        d = _evidence_record_dir(tmp_path, "../../etc/passwd")
        assert d is not None
        assert tmp_path in d.parents  # sanitized id stays inside the run dir

    def test_record_dir_empty_id_is_none(self, tmp_path):
        from ui import _evidence_record_dir
        assert _evidence_record_dir(tmp_path, "   ") is None

    def test_records_with_evidence(self, tmp_path):
        from ui import _records_with_evidence
        self._write_vault(tmp_path, "T-001")
        records = [{"id": "T-001"}, {"id": "T-002"}]
        assert _records_with_evidence(tmp_path, records) == {"T-001"}

    def test_load_evidence_entry_matches_url(self, tmp_path):
        from ui import _load_evidence_entry
        record_dir = self._write_vault(tmp_path)
        entry, text = _load_evidence_entry(record_dir, "https://a.example/services")
        assert entry["sha256"] == "abc"
        assert "IUD insertion" in text

    def test_load_evidence_entry_falls_back_to_first_page(self, tmp_path):
        from ui import _load_evidence_entry
        record_dir = self._write_vault(tmp_path)
        entry, text = _load_evidence_entry(record_dir, "https://other.example/")
        assert entry is not None
        assert text

    def test_load_evidence_entry_ignores_path_in_index_file_field(self, tmp_path):
        import json as _json
        from ui import _load_evidence_entry
        record_dir = self._write_vault(tmp_path)
        (record_dir / "index.json").write_text(_json.dumps([
            {"url": "https://a.example/services", "file": "../../../etc/passwd",
             "fetched_at": "x", "sha256": "x", "chars": 1, "provenance": "crawl"},
        ]))
        entry, text = _load_evidence_entry(record_dir, "https://a.example/services")
        assert text == ""  # basename "passwd" does not exist in the vault dir

    def test_highlight_wraps_quote_and_escapes_html(self):
        from ui import _excerpt_snapshot
        excerpt_html, full_html, found = _excerpt_snapshot(
            "Before <script>alert(1)</script> we offer IUD insertion here.",
            "IUD insertion",
        )
        assert found
        assert "<mark" in str(full_html) and "IUD insertion" in str(full_html)
        assert "<script>" not in str(full_html)

    def test_highlight_missing_quote_reports_not_found(self):
        from ui import _excerpt_snapshot
        excerpt_html, full_html, found = _excerpt_snapshot("Some page text.", "cash pay")
        assert not found
        assert "<mark>" not in str(full_html)


# ---------------------------------------------------------------------------
# Cartridge Viewer
# ---------------------------------------------------------------------------

def _minimal_status(**overrides):
    base = dict(
        run_id="RUN-20260610-100000-aaaa", project_id="p", source_type="manual",
        input_filename="in.csv", status="complete",
        created_at="2026-06-10T10:00:00Z", operator="tester",
    )
    base.update(overrides)
    from schema import RunStatus
    return RunStatus(**base)


class TestCartridgeViewer:
    def _write_snapshots(self, run_dir, cfg, icp):
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "project_config_snapshot.json").write_text(json.dumps(cfg))
        (run_dir / "icp_snapshot.json").write_text(json.dumps(icp))

    def test_renders_cartridge_without_geography_or_brands(self, tmp_path):
        """Valid-absence states: no geography restriction, no competitive_brands."""
        from ui import _build_cartridge_context, _render
        self._write_snapshots(
            tmp_path,
            {"client_name": "Neurolief", "target_specialty": "Psychiatry",
             "active_exclusion_rules": ["hospital_owned"]},
            {"icp_id": "neuro", "name": "Neurolief ICP", "signals": [
                {"signal_id": "S-1", "signal_label": "Cash pay",
                 "positive_weight": 20, "required_for_bullseye": True},
            ]},
        )
        ctx = _build_cartridge_context(tmp_path)
        assert ctx["geography"] == []
        assert ctx["competitive_brands"] is None
        html = _render("cartridge.html", username="t", run_id="RUN-X",
                       status=_minimal_status(), cartridge=ctx).body.decode("utf-8")
        assert "No geography restriction" in html
        assert "Not configured for this ICP" in html
        assert "Cash pay" in html

    def test_renders_competitive_brands_block(self, tmp_path):
        from ui import _build_cartridge_context, _render
        self._write_snapshots(
            tmp_path,
            {"client_name": "Angel", "target_geography": ["TX", "FL"]},
            {"icp_id": "angel", "signals": [],
             "competitive_brands": {
                 "brands": ["Invisalign", "ClearCorrect"],
                 "aliases": ["iTero"],
                 "partner_tier_phrases": ["Diamond Provider"],
             }},
        )
        ctx = _build_cartridge_context(tmp_path)
        html = _render("cartridge.html", username="t", run_id="RUN-X",
                       status=_minimal_status(), cartridge=ctx).body.decode("utf-8")
        assert "Invisalign" in html and "ClearCorrect" in html
        assert "iTero" in html
        assert "Diamond Provider" in html
        assert "TX, FL" in html

    def test_missing_snapshots_is_valid_state(self, tmp_path):
        from ui import _build_cartridge_context, _render
        ctx = _build_cartridge_context(tmp_path)  # empty dir — legacy CLI run
        assert ctx["config"] is None and ctx["icp"] is None
        html = _render("cartridge.html", username="t", run_id="RUN-X",
                       status=_minimal_status(), cartridge=ctx).body.decode("utf-8")
        assert "No cartridge snapshot" in html

    def test_gates_describe_caps_and_exclusions(self, tmp_path):
        from ui import _build_cartridge_context
        self._write_snapshots(
            tmp_path, {"client_name": "C"},
            {"signals": [
                {"signal_id": "S-1", "signal_label": "REI on staff",
                 "positive_weight": 0, "exclude_if_yes": True},
                {"signal_id": "S-2", "signal_label": "Hospital affiliated",
                 "positive_weight": -30, "cap_tier": "Needs Verification"},
            ]},
        )
        gates = _build_cartridge_context(tmp_path)["gates"]
        rules = {g["signal_id"]: "; ".join(g["rules"]) for g in gates}
        assert "excludes" in rules["S-1"]
        assert "Needs Verification" in rules["S-2"]


# ---------------------------------------------------------------------------
# Evidence Link Checker (API side)
# ---------------------------------------------------------------------------

class TestLinkCheckReport:
    def test_collect_links_filters_tiers_and_bad_urls(self):
        from ui import _collect_evidence_links
        records = [
            {"id": "T-1", "practice_name": "A", "displayed_tier": "Bullseye",
             "signals": [
                 {"signal_label": "IUD", "source_url": "https://a.com/services"},
                 {"signal_label": "Cash", "source_url": "not_found"},
                 {"signal_label": "Hours", "source_url": ""},
             ]},
            {"id": "T-2", "practice_name": "B", "displayed_tier": "Excluded",
             "signals": [{"signal_label": "X", "source_url": "https://b.com/"}]},
            {"id": "T-3", "practice_name": "C", "displayed_tier": "Contender",
             "signals": [{"signal_label": "Y", "source_url": "https://c.com/y"}]},
        ]
        links = _collect_evidence_links(records)
        urls = [l["url"] for l in links]
        assert urls == ["https://a.com/services", "https://c.com/y"]

    def test_report_written_and_run_outputs_untouched(self, tmp_path):
        import hashlib
        from ui import _build_link_check_report, _read_link_check_report
        import reviews as reviews_mod
        # A run output file that must not change.
        enriched = tmp_path / "enriched_targets.json"
        enriched.write_text(json.dumps({"records": [{"id": "T-1"}]}))
        before = hashlib.sha256(enriched.read_bytes()).hexdigest()

        links = [{"record_id": "T-1", "practice_name": "A",
                  "signal_label": "IUD", "url": "https://a.com/x"}]
        results = [{"url": "https://a.com/x", "classification": "DEAD",
                    "detail": "timeout", "final_url": "https://a.com/x"}]
        report = _build_link_check_report(links, results)
        reviews_mod._atomic_write(tmp_path / "link_check_report.json", report)

        loaded = _read_link_check_report(tmp_path)
        assert loaded["total_checked"] == 1
        assert loaded["flagged"] == 1
        assert loaded["results"][0]["classification"] == "DEAD"
        after = hashlib.sha256(enriched.read_bytes()).hexdigest()
        assert before == after  # run output byte-identical

    def test_summary_counts(self):
        from ui import _build_link_check_report
        links = [
            {"record_id": "T-1", "practice_name": "A", "signal_label": "S", "url": "https://a.com/1"},
            {"record_id": "T-2", "practice_name": "B", "signal_label": "S", "url": "https://a.com/2"},
        ]
        results = [
            {"url": "https://a.com/1", "classification": "OK", "detail": "", "final_url": ""},
            {"url": "https://a.com/2", "classification": "FLAG", "detail": "x", "final_url": ""},
        ]
        report = _build_link_check_report(links, results)
        assert report["total_checked"] == 2
        assert report["ok"] == 1
        assert report["flagged"] == 1


# ---------------------------------------------------------------------------
# Cost-per-run display
# ---------------------------------------------------------------------------

class TestCostPerRun:
    def test_cost_math(self):
        import llm_pricing
        # 2M input @ $3/M + 1M output @ $15/M = $21
        assert llm_pricing.estimate_cost_usd(2_000_000, 1_000_000) == 21.0

    def test_cost_summary_with_per_record_division(self):
        import llm_pricing
        status = _minimal_status(
            records_output=50,
            llm_input_tokens=1_000_000, llm_output_tokens=200_000, llm_call_count=50,
        )
        summary = llm_pricing.cost_summary(status)
        assert summary["estimated_cost_usd"] == 6.0   # 3 + 3
        assert summary["cost_per_record_usd"] == 0.12
        assert summary["llm_calls"] == 50
        assert summary["rates_as_of"] == llm_pricing.LAST_VERIFIED

    def test_pre_capture_run_returns_none_not_zero(self):
        """status.json without token fields → 'not captured' message, never $0."""
        import llm_pricing
        status = _minimal_status()  # no llm_* fields
        assert status.llm_call_count is None
        assert llm_pricing.cost_summary(status) is None

    def test_zero_records_avoids_division_error(self):
        import llm_pricing
        status = _minimal_status(
            records_output=0,
            llm_input_tokens=100, llm_output_tokens=100, llm_call_count=1,
        )
        summary = llm_pricing.cost_summary(status)
        assert summary["cost_per_record_usd"] is None
