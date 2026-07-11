"""
test_signal_filter.py

Tests for Prompt 6: the client-side signal filter on the analyst dashboard
(results.html). Display/UX only — no backend route, no scoring, no client-facing
output change. Tests use the FastAPI TestClient to render the internal dashboard
and assert on the markup (filter bar, controls, per-row data attributes).

Deterministic — no network, no subprocess.
"""

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_API_DIR = _REPO / "pipeline-api"
sys.path.insert(0, str(_API_DIR))

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("UI_USERNAME", "tester")
os.environ.setdefault("UI_PASSWORD", "secret-pw")
os.environ.setdefault("PIPELINE_REPO_PATH", str(_REPO))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import runs  # noqa: E402

_RUN_ID = "RUN-20260621-140000-cccc"


def _record(rid, signals, tier="Contender", score=72):
    """Build a complete-status enriched record with the given signals."""
    return {
        "id": rid, "record_id": rid, "practice_name": "Practice " + rid,
        "bullseye_score": score, "fit_signal_score": 68, "confidence_score": 80,
        "target_tier": tier, "exclusion_status": "CLEAR",
        "enrichment_status": "complete", "confidence_band": "Moderate",
        "address_city": "Atlanta", "address_state": "GA",
        "source_confidence": "high",
        "signals": signals,
    }


def _sig(signal_id, label, state, **extra):
    s = {
        "signal_id": signal_id, "signal_label": label, "signal_state": state,
        "evidence_text": "ev" if state == "yes" else "",
        "source_url": "https://x.example.com" if state == "yes" else "",
        "confidence": "high", "positive_weight": 20,
        "state_inferred": False, "inferred_from": "", "not_found_reason": "",
    }
    s.update(extra)
    return s


def _two_records():
    """T-1 has IUI(yes)+CashPay(yes); T-2 has IUI(yes), CashPay(not_found)."""
    return [
        _record("T-1", [
            _sig("S-ICP-001", "IUI offered", "yes"),
            _sig("S-ICP-007", "Cash-pay visible", "yes"),
        ]),
        _record("T-2", [
            _sig("S-ICP-001", "IUI offered", "yes"),
            _sig("S-ICP-007", "Cash-pay visible", "not_found"),
        ]),
    ]


def _write_run(run_directory, records):
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-06-21T14:00:00+00:00",
        "completed_at": "2026-06-21T14:30:00+00:00", "operator": "tester",
    }))
    (run_directory / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": records}, indent=2))


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path)
    return tmp_path / _RUN_ID


@pytest.fixture
def client(run_dir):
    _write_run(run_dir, _two_records())
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        yield c


def _html(c):
    r = c.get(f"/dashboard/{_RUN_ID}")
    assert r.status_code == 200
    return r.text


# ---------------------------------------------------------------------------
# 1 — filter bar renders one control per distinct "yes" signal; none for
#     signals that fired on zero cards
# ---------------------------------------------------------------------------

def test_filter_bar_one_control_per_yes_signal(client):
    html = _html(client)
    assert 'id="signal-filter-bar"' in html
    # Both S-ICP-001 and S-ICP-007 fired "yes" on at least one record.
    assert 'data-sig-id="S-ICP-001"' in html
    assert 'data-sig-id="S-ICP-007"' in html
    # Exactly two signal controls plus the "All" control.
    assert html.count('class="sig-filter-btn"') == 2  # the two signal buttons
    assert 'id="sig-filter-all"' in html


def test_no_control_for_never_yes_signal(run_dir):
    # A signal that is not_found on every record must NOT get a control.
    records = [
        _record("T-1", [
            _sig("S-ICP-001", "IUI offered", "yes"),
            _sig("S-ICP-099", "Never fires", "not_found"),
        ]),
    ]
    _write_run(run_dir, records)
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        html = c.get(f"/dashboard/{_RUN_ID}").text
    assert 'data-sig-id="S-ICP-001"' in html
    assert 'data-sig-id="S-ICP-099"' not in html


# ---------------------------------------------------------------------------
# 2 — controls show human signal_label text, not internal keys
# ---------------------------------------------------------------------------

def test_controls_show_signal_label_text(client):
    html = _html(client)
    # The button text is the human label.
    assert ">IUI offered</button>" in html
    assert ">Cash-pay visible</button>" in html


# ---------------------------------------------------------------------------
# 3 — each card carries data-signals-yes listing its "yes" signals, matching
#     the FOUND signals on that card (override-merged state from 4a)
# ---------------------------------------------------------------------------

def test_card_data_attribute_lists_yes_signals(client):
    html = _html(client)
    # T-1: both yes → both ids present in its data attribute.
    assert 'data-rid="T-1"' in html
    assert 'data-signals-yes="S-ICP-001 S-ICP-007"' in html
    # T-2: only IUI yes; cash-pay not_found → only S-ICP-001.
    assert 'data-signals-yes="S-ICP-001"' in html


def test_data_attribute_reflects_inferred_state(run_dir):
    # A state_inferred signal is treated as FOUND, so it must appear in the
    # row's data-signals-yes even though signal_state is not "yes".
    records = [
        _record("T-1", [
            _sig("S-ICP-001", "IUI offered", "yes"),
            _sig("S-ICP-007", "Cash-pay visible", "not_found",
                 state_inferred=True, inferred_from="S-ICP-001"),
        ]),
    ]
    _write_run(run_dir, records)
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        html = c.get(f"/dashboard/{_RUN_ID}").text
    assert 'data-signals-yes="S-ICP-001 S-ICP-007"' in html


# ---------------------------------------------------------------------------
# 4 — "All" is default active; on initial render no card is hidden
# ---------------------------------------------------------------------------

def test_all_control_active_by_default(client):
    html = _html(client)
    assert 'id="sig-filter-all" class="sig-filter-btn active"' in html
    # No row carries an inline display:none from the server (filtering is JS-only).
    # The signal buttons (non-All) are not active by default.
    assert html.count('class="sig-filter-btn active"') == 1  # only "All"


def test_initial_render_hides_no_rows(client):
    html = _html(client)
    # Server renders no display:none on record rows (the filter is applied client-side).
    # Confirm both record rows render normally.
    assert 'data-rid="T-1"' in html
    assert 'data-rid="T-2"' in html
    # Each .record-row opening tag carries only the cursor style — never a
    # server-rendered display:none from the signal filter. (Detail rows DO use
    # display:none legitimately, so we inspect record-row tags specifically.)
    import re
    for tag in re.findall(r'<tr class="record-row".*?>', html, re.DOTALL):
        assert "display:none" not in tag


# ---------------------------------------------------------------------------
# 5 — JS wiring: toggle function + AND semantics present in the page script
# ---------------------------------------------------------------------------

def test_filter_js_present_and_client_side(client):
    html = _html(client)
    assert "window.toggleSignalFilter = function" in html
    # AND semantics: a row matches only if it has ALL selected signal ids.
    assert "for (var id in selected) { if (!has[id]) { match = false; break; } }" in html
    # Live count text.
    assert "Showing ' + shown + ' of ' + rows.length + ' practices'" in html
    # Operates on the main results table only.
    assert "querySelectorAll('#results-table .record-row')" in html
    # No fetch / server round-trip in the filter logic.
    assert "fetch(" not in html.split("toggleSignalFilter")[1].split("</script>")[0]


# ---------------------------------------------------------------------------
# 6 — ingested (not-yet-enriched) run shows no filter bar
# ---------------------------------------------------------------------------

def test_no_filter_bar_for_ingested_run(run_dir):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "ingested",
        "created_at": "2026-06-21T14:00:00+00:00", "operator": "tester",
    }))
    (run_dir / "enriched_targets.json").write_text(json.dumps({
        "run_id": _RUN_ID,
        "records": [{
            "id": "T-1", "record_id": "T-1", "practice_name": "Acme",
            "enrichment_status": "not_enriched", "exclusion_status": "CLEAR",
            "target_tier": "", "bullseye_score": 0, "signals": [],
        }],
    }))
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        html = c.get(f"/dashboard/{_RUN_ID}").text
    assert 'id="signal-filter-bar"' not in html


# ---------------------------------------------------------------------------
# 7 — the filter UI does not appear in any client-facing template
# ---------------------------------------------------------------------------

def test_filter_absent_from_client_templates():
    client_templates = [
        _REPO / "handoff_renderer" / "templates" / "sales_handoff.html",
        _API_DIR / "reports" / "templates" / "bullseye_cards.html",
    ]
    checked = 0
    for path in client_templates:
        if not path.exists():
            continue
        checked += 1
        text = path.read_text(encoding="utf-8")
        assert "toggleSignalFilter" not in text
        assert "signal-filter-bar" not in text
        assert "data-signals-yes" not in text
    assert checked >= 1


# ---------------------------------------------------------------------------
# 8 — regression: a run with zero "yes" signals renders no filter bar but the
#     table still renders (filter bar is purely additive)
# ---------------------------------------------------------------------------

def test_no_filter_bar_when_no_yes_signals(run_dir):
    records = [
        _record("T-1", [_sig("S-ICP-001", "IUI offered", "not_found")],
                tier="Manual Review", score=10),
    ]
    _write_run(run_dir, records)
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        html = c.get(f"/dashboard/{_RUN_ID}").text
    assert 'id="signal-filter-bar"' not in html
    # The record still renders in the table.
    assert 'data-rid="T-1"' in html
    # The row carries an empty data-signals-yes attribute (no yes signals).
    assert 'data-signals-yes=""' in html
