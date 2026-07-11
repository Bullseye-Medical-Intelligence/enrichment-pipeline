"""
test_dashboard_ux.py

Tests for the operator-dashboard UX additions:
- "Sales Hook" column on the results table (first grounded sales angle, falling
  back to call_brief.why_contact) and its "Exclusion Reason" twin on the
  excluded tables — same macro cell, header varies per table.
- HIGH FIT / LOW EVIDENCE badge in the Tier cell (anti-averaging surfaced).
- Dark-mode option: pre-paint theme script + navbar toggle in base.html, the
  [data-theme="dark"] block in style.css, and a guard that none of it leaks
  into the self-contained client templates.

Deterministic — no network, no subprocess. Mirrors the test_signal_columns.py
fixture pattern.
"""

import json
import os
import re
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

import config  # noqa: E402
import icp_profiles  # noqa: E402
import main  # noqa: E402
import runs  # noqa: E402

_RUN_ID = "RUN-20260702-090000-eeee"

_PLAIN_ICP = {
    "icp_id": "obgyn_femasys", "name": "Test ICP", "version": "test-v1",
    "signals": [
        {"signal_id": "S-1", "signal_label": "x", "prompt_instruction": "y",
         "positive_weight": 10},
    ],
}


def _record(rid, **over):
    rec = {
        "id": rid, "record_id": rid, "practice_name": "Practice " + rid,
        "bullseye_score": 72, "target_tier": "Contender", "exclusion_status": "CLEAR",
        "enrichment_status": "complete", "confidence_band": "Moderate",
        "address_city": "Atlanta", "address_state": "GA", "source_confidence": "complete",
        "signals": [], "sales_angle": [], "call_brief": {},
    }
    rec.update(over)
    return rec


def _write_run(run_directory, records):
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / "status.json").write_text(json.dumps({
        "run_id": _RUN_ID, "project_id": "P-1", "source_type": "outscraper",
        "input_filename": "x.csv", "status": "complete",
        "created_at": "2026-07-02T09:00:00+00:00",
        "completed_at": "2026-07-02T09:30:00+00:00", "operator": "tester",
        "icp_profile_id": "obgyn_femasys",
    }))
    (run_directory / "enriched_targets.json").write_text(
        json.dumps({"run_id": _RUN_ID, "records": records}, indent=2))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated run + ICP store; startup seed-sync stubbed out."""
    monkeypatch.setattr(runs, "OUTPUT_RUNS_PATH", tmp_path / "runs")
    icp_dir = tmp_path / "icp"
    icp_dir.mkdir()
    monkeypatch.setattr(config, "ICP_PROFILES_PATH", icp_dir)
    monkeypatch.setattr(icp_profiles, "sync_seed_profile", lambda *a, **k: False)
    (icp_dir / "obgyn_femasys.json").write_text(json.dumps(_PLAIN_ICP))
    return tmp_path / "runs" / _RUN_ID


def _get(path):
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "tester", "password": "secret-pw"})
        return c.get(path).text


# ---------------------------------------------------------------------------
# Sales Hook column
# ---------------------------------------------------------------------------

def test_sales_hook_column_renders_first_angle(env):
    _write_run(env, [_record(
        "T-1", sales_angle=["Practice runs high-volume IUI. Ready for a device add-on."],
    )])
    html = _get(f"/dashboard/{_RUN_ID}")
    assert ">Sales Hook</th>" in html
    assert "Practice runs high-volume IUI." in html
    assert 'class="hook-cell"' in html


def test_sales_hook_falls_back_to_why_contact(env):
    _write_run(env, [_record(
        "T-1", sales_angle=[],
        call_brief={"why_contact": "Confirmed cash-pay and fertility services on the site."},
    )])
    html = _get(f"/dashboard/{_RUN_ID}")
    assert "Confirmed cash-pay and fertility services" in html


def test_sales_hook_dash_when_no_evidence(env):
    _write_run(env, [_record("T-1")])
    html = _get(f"/dashboard/{_RUN_ID}")
    assert ">Sales Hook</th>" in html  # header renders; cell shows a muted dash


# ---------------------------------------------------------------------------
# Exclusion Reason column (excluded table)
# ---------------------------------------------------------------------------

def test_excluded_table_shows_exclusion_reason(env):
    _write_run(env, [
        _record("T-1"),
        _record("T-2", target_tier="Excluded", exclusion_status="EXCLUDED",
                exclusion_reason="Hospital-owned practice", bullseye_score=40),
    ])
    html = _get(f"/dashboard/{_RUN_ID}")
    assert ">Exclusion Reason</th>" in html
    # The reason renders in the excluded row's hook-cell (not only the detail panel).
    assert 'hook-cell"><span title="Hospital-owned practice"' in html


# ---------------------------------------------------------------------------
# HIGH FIT / LOW EVIDENCE badge (anti-averaging surfaced)
# ---------------------------------------------------------------------------

def test_hfle_badge_renders_only_for_high_fit_low_evidence(env):
    _write_run(env, [
        _record("T-1", fit_confidence_status="HIGH FIT / LOW EVIDENCE"),
        _record("T-2", fit_confidence_status="HIGH FIT / HIGH EVIDENCE"),
    ])
    html = _get(f"/dashboard/{_RUN_ID}")
    assert html.count("badge-hfle") == 1


def test_hfle_badge_absent_on_excluded_records(env):
    _write_run(env, [_record(
        "T-1", target_tier="Excluded", exclusion_status="EXCLUDED",
        exclusion_reason="Wrong specialty", fit_confidence_status="HIGH FIT / LOW EVIDENCE",
    )])
    html = _get(f"/dashboard/{_RUN_ID}")
    assert "badge-hfle" not in html


# ---------------------------------------------------------------------------
# Dark-mode option
# ---------------------------------------------------------------------------

def test_theme_toggle_and_prepaint_script_present(env):
    _write_run(env, [_record("T-1")])
    html = _get(f"/dashboard/{_RUN_ID}")
    assert 'id="theme-toggle"' in html
    assert "bemi_theme" in html           # pre-paint script in <head>
    assert "toggleTheme()" in html        # navbar button wiring


def test_style_css_defines_dark_theme_block():
    css = (_API_DIR / "static" / "style.css").read_text(encoding="utf-8")
    assert 'html[data-theme="dark"]' in css
    assert "color-scheme: dark" in css
    # Terracotta accent (and its darker shade) is never remapped by the dark
    # theme (design system). Everything after the first dark selector is scanned.
    dark_block = css.split('html[data-theme="dark"]', 1)[1]
    assert "--accent:" not in dark_block
    assert "--accent-dk:" not in dark_block


def test_dark_theme_never_remaps_tier_stat_colors():
    """The six tier stat colors are NEVER remapped in dark mode (design system) —
    each dark-block declaration must keep its canonical hex, identical to light."""
    css = (_API_DIR / "static" / "style.css").read_text(encoding="utf-8")
    tier_hex = {
        ".stat-bullseye": "#b91c1c",
        ".stat-needs-verification": "#b45309",
        ".stat-contender": "#9a3823",
        ".stat-manual-review": "#475569",
        ".stat-excluded": "#1e2530",
        ".stat-pending": "#5b21b6",
    }
    for cls, hex_val in tier_hex.items():
        # Light (:root-scope) declaration carries the canonical value.
        assert re.search(rf"(?<!])\s{re.escape(cls)}\s*\{{\s*background:\s*{hex_val};",
                         css), f"light {cls} != {hex_val}"
        # Dark-block re-declaration must match the same value, not a dark-tuned one.
        assert re.search(
            rf'html\[data-theme="dark"\]\s{re.escape(cls)}\s*\{{\s*background:\s*{hex_val};',
            css), f"dark {cls} remapped away from {hex_val}"


def test_dark_theme_inverts_text_and_surface_for_legibility():
    """The dark block must flip --ink light and --surface dark so the body
    text/background pair can never collapse to illegible same-on-same."""
    css = (_API_DIR / "static" / "style.css").read_text(encoding="utf-8")
    dark_block = css.split('html[data-theme="dark"]', 1)[1].split("}", 1)[0]
    assert re.search(r"--ink:\s*#ece", dark_block)      # light body text
    assert re.search(r"--surface:\s*#0f0f0e", dark_block)  # dark page background


def test_dark_mode_and_hook_absent_from_client_templates():
    """The theme toggle, theme key, and new operator columns must never leak into
    the self-contained client/prospect templates."""
    client_templates = [
        _REPO / "handoff_renderer" / "templates" / "sales_handoff.html",
        _API_DIR / "reports" / "templates" / "bullseye_cards.html",
        _API_DIR / "reports" / "templates" / "executive_target_report.html",
        _API_DIR / "reports" / "templates" / "sales_handoff.html",
        _API_DIR / "templates" / "sales_brief.html",
    ]
    checked = 0
    for path in client_templates:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        # Markers are operator-distinctive: the client Sales Handoff carries its
        # OWN self-contained day/night toggle (theme-toggle / toggleTheme ids),
        # so those generic names are not usable as leak markers.
        for marker in ("bemi_theme", "nav-theme-toggle", "hook-cell", "badge-hfle"):
            assert marker not in text, f"'{marker}' leaked into client template {path.name}"
        checked += 1
    assert checked >= 1
