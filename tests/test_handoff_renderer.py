"""
tests/test_handoff_renderer.py
Tests for the Bullseye Sales Handoff renderer.
"""

from datetime import date

import pytest

from handoff_renderer import Account, Confidence, HandoffRun, Tier, render_handoff


def _make_account(
    name="Test Practice",
    tier=Tier.BULLSEYE,
    confidence=Confidence.HIGH,
    internal_score=85,
    **kwargs,
) -> Account:
    defaults = dict(
        city="Dallas, TX",
        phone="(214) 555-0100",
        website="testpractice.com",
        evidence_domain="testpractice.com",
        why_it_matters=["Strong fit."],
        wedge="Clear entry.",
        confirmed_signals=["TMS offered"],
        verify=["Volume"],
        landmine=None,
    )
    defaults.update(kwargs)
    return Account(
        name=name,
        tier=tier,
        confidence=confidence,
        internal_score=internal_score,
        **defaults,
    )


def _make_excluded(name="Excluded Practice", internal_score=0) -> Account:
    return Account(
        name=name,
        city="Dallas, TX",
        phone="(214) 555-0100",
        website="excluded.com",
        evidence_domain="excluded.com",
        tier=Tier.EXCLUDED,
        confidence=Confidence.HIGH,
        internal_score=internal_score,
        gate_fired="Health-system affiliation",
        evidence="System website",
        suppress_reason="No independent purchasing path.",
        revisit_if="Spins out independently",
    )


def _make_run(**kwargs) -> HandoffRun:
    defaults = dict(
        product_name="TestProduct",
        client_name="TestClient",
        run_date=date(2026, 1, 1),
        specialty_label="Interventional Psychiatry",
        metro="Dallas",
        icp_version="test-icp-v1",
        qc_reviewer="testreviewer",
        accounts=[_make_account()],
        pattern_insight=None,
    )
    defaults.update(kwargs)
    return HandoffRun(**defaults)


# ── (a) No internal_score in client-facing output ─────────────────────────────

def test_no_internal_score_in_client_output():
    """internal_score values must never appear in client-facing HTML."""
    accounts = [
        _make_account("High Scorer", internal_score=99),
        _make_account("Low Scorer", tier=Tier.CONTENDER, confidence=Confidence.LOW, internal_score=23),
        _make_excluded("Excluded One", internal_score=0),
    ]
    run = _make_run(accounts=accounts)
    html = render_handoff(run, client_facing=True)
    assert "99" not in html
    assert "23" not in html
    # Confirm 0 check: only "0" from score absence, not the excluded score
    # More precisely: the literal score values must not appear
    for acct in accounts:
        score_str = str(acct.internal_score)
        if score_str == "0":
            # 0 might appear innocuously (e.g. in CSS), so check with context
            assert f"score={score_str}" not in html
            assert f"internal_score" not in html
        else:
            assert score_str not in html, (
                f"internal_score {score_str} for '{acct.name}' found in client-facing output"
            )


def test_internal_score_absent_when_client_facing_default():
    """Default call (no client_facing arg) must also suppress scores."""
    run = _make_run(accounts=[_make_account(internal_score=77)])
    html = render_handoff(run)
    assert "77" not in html
    assert "internal_score" not in html


# ── (b) Pattern block absent when pattern_insight is None ─────────────────────

def test_pattern_block_absent_when_none():
    """When pattern_insight is None the pattern div must not appear."""
    run = _make_run(pattern_insight=None)
    html = render_handoff(run)
    assert 'class="pattern"' not in html
    assert "Pattern Across This Run" not in html


def test_pattern_block_present_when_set():
    """When pattern_insight is set the pattern div must appear with the text."""
    insight = "The standout accounts share a multi-modal interventional stack."
    run = _make_run(pattern_insight=insight)
    html = render_handoff(run)
    assert 'class="pattern"' in html
    assert "Pattern Across This Run" in html
    assert "multi-modal interventional stack" in html


# ── (c) Every account appears in the output ───────────────────────────────────

def test_all_accounts_appear_in_output():
    """Every account name in the input must appear in the rendered HTML."""
    names = [
        "Alpha Practice",
        "Beta Behavioral",
        "Gamma Psychiatry",
        "Delta Wellness",
        "Epsilon TMS Center",
    ]
    accounts = [
        _make_account(names[0], tier=Tier.BULLSEYE, confidence=Confidence.HIGH),
        _make_account(names[1], tier=Tier.BULLSEYE, confidence=Confidence.MEDIUM),
        _make_account(names[2], tier=Tier.CONTENDER, confidence=Confidence.HIGH),
        _make_account(names[3], tier=Tier.CONTENDER, confidence=Confidence.LOW),
        _make_excluded(names[4]),
    ]
    run = _make_run(accounts=accounts)
    html = render_handoff(run)
    for name in names:
        assert name in html, f"Account '{name}' missing from rendered output"


def test_no_stub_rows_in_output():
    """The renderer must not emit stub rows like '+ N more accounts'."""
    accounts = [_make_account(f"Practice {i}") for i in range(5)]
    run = _make_run(accounts=accounts)
    html = render_handoff(run)
    assert "more Validate" not in html
    assert "more Call First" not in html
    assert "more Suppress" not in html


# ── (d) Within-tier confidence ordering ───────────────────────────────────────

def test_confidence_ordering_within_tier():
    """Within each tier, HIGH confidence accounts must appear before MEDIUM before LOW."""
    accounts = [
        _make_account("Low Contender", tier=Tier.CONTENDER, confidence=Confidence.LOW),
        _make_account("Medium Contender", tier=Tier.CONTENDER, confidence=Confidence.MEDIUM),
        _make_account("High Contender", tier=Tier.CONTENDER, confidence=Confidence.HIGH),
    ]
    run = _make_run(accounts=accounts)
    html = render_handoff(run)
    pos_high = html.index("High Contender")
    pos_med = html.index("Medium Contender")
    pos_low = html.index("Low Contender")
    assert pos_high < pos_med < pos_low, (
        "Confidence ordering violated: expected HIGH < MEDIUM < LOW within tier"
    )


def test_confidence_ordering_stable():
    """Accounts with the same confidence must preserve input order."""
    accounts = [
        _make_account("First High", tier=Tier.BULLSEYE, confidence=Confidence.HIGH),
        _make_account("Second High", tier=Tier.BULLSEYE, confidence=Confidence.HIGH),
        _make_account("Third High", tier=Tier.BULLSEYE, confidence=Confidence.HIGH),
    ]
    run = _make_run(accounts=accounts)
    html = render_handoff(run)
    assert html.index("First High") < html.index("Second High") < html.index("Third High")


def test_confidence_ordering_across_tiers():
    """Ordering within each tier is independent — BULLSEYE section precedes CONTENDER."""
    accounts = [
        _make_account("Low Bullseye", tier=Tier.BULLSEYE, confidence=Confidence.LOW),
        _make_account("High Contender", tier=Tier.CONTENDER, confidence=Confidence.HIGH),
    ]
    run = _make_run(accounts=accounts)
    html = render_handoff(run)
    # The Bullseye section must appear before the Contender section
    assert html.index("Low Bullseye") < html.index("High Contender")


# ── Additional invariant tests ────────────────────────────────────────────────

def test_empty_tier_section_omitted():
    """A tier with zero accounts must not produce a section or filter chip."""
    run = _make_run(accounts=[_make_account(tier=Tier.BULLSEYE)])
    html = render_handoff(run)
    # No CONTENDER or EXCLUDED accounts — section divs and chips must be absent.
    # Use class="tsection" as the discriminator since the CSS now contains data-tier="e".
    assert 'class="tsection" data-tier="c"' not in html, "Validate (CONTENDER) section should be absent"
    assert 'class="tsection" data-tier="e"' not in html, "Suppress (EXCLUDED) section should be absent"
    assert 'data-f="c"' not in html, "Validate filter chip should be absent"
    assert 'data-f="e"' not in html, "Suppress filter chip should be absent"


def test_tier_display_words_not_enum_values():
    """Data-layer tier enum values (BULLSEYE/CONTENDER/EXCLUDED) must not appear in HTML body."""
    accounts = [
        _make_account(tier=Tier.BULLSEYE),
        _make_account(tier=Tier.CONTENDER),
        _make_excluded(),
    ]
    run = _make_run(accounts=accounts)
    html = render_handoff(run)
    # Enum string values must not appear (only mapped display words should)
    body = html.split("<body>", 1)[-1] if "<body>" in html else html
    assert "BULLSEYE" not in body
    assert "CONTENDER" not in body
    assert "EXCLUDED" not in body


def test_bold_markdown_rendered():
    """**bold** in landmine text must render as <b>bold</b>, not as literal asterisks."""
    run = _make_run(accounts=[
        _make_account(landmine="Do not lead with pricing. **Anchor on outcomes first.**"),
    ])
    html = render_handoff(run)
    assert "<b>Anchor on outcomes first.</b>" in html
    assert "**" not in html


def test_expiry_date_defaults_to_30_days():
    """expiry_date defaults to run_date + 30 days when not supplied."""
    run = HandoffRun(
        product_name="X",
        client_name="Y",
        run_date=date(2026, 6, 1),
        specialty_label="Psychiatry",
        metro="Dallas",
        icp_version="v1",
        qc_reviewer="q",
        accounts=[_make_account()],
    )
    assert run.expiry_date == date(2026, 7, 1)
