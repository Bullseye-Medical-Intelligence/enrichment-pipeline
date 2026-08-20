"""
tests/test_browser_crawl_budget.py

The browser crawler's wall-clock bound. Every individual step was already
bounded (navigation timeout, challenge budget, per-subpage timeout) but nothing
bounded their sum, so one bot-gated domain could hold a crawl worker for
minutes while the rest of the batch waited on a free slot.

Deterministic: no Playwright, no network, no real sleeps. A fake clock is
advanced explicitly by a fake page, so every assertion is exact rather than
timing-dependent.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from extraction import playwright_extractor as pw  # noqa: E402

# _remaining_ms floors an exhausted deadline at 1ms rather than 0 (Playwright
# reads 0 as "wait forever"), so a bound may be overshot by a few of those
# floors and no more. Anything larger is a real overrun.
_FLOOR_SLACK_S = 0.01


# ---------------------------------------------------------------------------
# Fake clock + fake browser
# ---------------------------------------------------------------------------

class _Clock:
    """A monotonic clock advanced only by explicit calls."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeTime:
    """Stands in for the module's `time` import — monotonic() only."""

    def __init__(self, clock: _Clock):
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock.now


class _FakeMouse:
    """_human_nudge's target. Nudging is free; it costs no clock."""

    def move(self, x, y):
        pass

    def wheel(self, dx, dy):
        pass


class _TimeoutError(Exception):
    """Stands in for playwright.sync_api.TimeoutError."""


class _FakePage:
    """A page that charges the fake clock for every wait it is asked to make.

    `nav_cost_s` is what one navigation really takes. When the caller allows
    less than that, the navigation burns the whole allowance and raises — the
    behaviour a real timeout has, and the case the deadline must survive.
    """

    def __init__(self, clock, pages: dict, nav_cost_s: float = 10.0,
                 settle_cost_s: float = 0.5):
        self._clock = clock
        self._pages = pages
        self._nav_cost_s = nav_cost_s
        self._settle_cost_s = settle_cost_s
        self.url = ""
        self.mouse = _FakeMouse()
        self.visited: list[str] = []

    def goto(self, url, timeout=None, wait_until=None):
        assert timeout and timeout > 0, "a 0/None timeout means 'wait forever'"
        allowed_s = timeout / 1000
        self.visited.append(url)
        if self._nav_cost_s > allowed_s:
            self._clock.advance(allowed_s)
            raise _TimeoutError(f"navigation to {url} exceeded {timeout}ms")
        self._clock.advance(self._nav_cost_s)
        self.url = url

    def wait_for_load_state(self, state, timeout=None):
        assert timeout and timeout > 0, "a 0/None timeout means 'wait forever'"
        self._clock.advance(min(self._settle_cost_s, timeout / 1000))

    def wait_for_timeout(self, ms):
        assert ms and ms > 0, "a 0 wait is never intended"
        self._clock.advance(ms / 1000)

    def content(self):
        return self._pages.get(self.url, "<html><body></body></html>")


class _ForeverChallengePage(_FakePage):
    """A challenge wall that never clears, whatever it is asked to wait."""

    def content(self):
        return "<html><title>Just a moment...</title><body>Checking your browser</body></html>"


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(pw, "time", _FakeTime(c))
    return c


# ---------------------------------------------------------------------------
# _crawl_budget_seconds — the ceiling, and what it must never truncate
# ---------------------------------------------------------------------------

def test_nominal_config_uses_the_standard_ceiling(monkeypatch):
    monkeypatch.delenv("PIPELINE_BROWSER_CHALLENGE_WAIT_MS", raising=False)
    assert pw._crawl_budget_seconds(15000) == pw.PLAYWRIGHT_MAX_CRAWL_SECONDS


def test_ceiling_never_truncates_the_challenge_budget(monkeypatch):
    """Raising PIPELINE_BROWSER_CHALLENGE_WAIT_MS widens the deadline instead of
    being clipped by it. Giving up at 50s of a 60s Cloudflare timer wastes the
    whole attempt, which is the opposite of why an operator raised the knob."""
    monkeypatch.setenv("PIPELINE_BROWSER_CHALLENGE_WAIT_MS", "90000")
    budget_s = pw._crawl_budget_seconds(15000)
    assert budget_s > pw.PLAYWRIGHT_MAX_CRAWL_SECONDS
    assert budget_s >= 90.0


def test_ceiling_widens_for_a_long_navigation_timeout(monkeypatch):
    """request_timeout_seconds is threaded in from run_config; a generous one
    must not leave the homepage unable to finish inside the deadline."""
    monkeypatch.delenv("PIPELINE_BROWSER_CHALLENGE_WAIT_MS", raising=False)
    assert pw._crawl_budget_seconds(60000) > pw.PLAYWRIGHT_MAX_CRAWL_SECONDS


def test_ceiling_covers_navigation_plus_settle_plus_challenge(monkeypatch):
    monkeypatch.setenv("PIPELINE_BROWSER_CHALLENGE_WAIT_MS", "25000")
    timeout_ms = 15000
    floor_ms = (timeout_ms
                + min(timeout_ms, pw._NETWORKIDLE_SETTLE_MS)
                + pw._LAZY_CONTENT_PAUSE_MS
                + 25000)
    assert pw._crawl_budget_seconds(timeout_ms) >= floor_ms / 1000


# ---------------------------------------------------------------------------
# _remaining_ms — Playwright reads timeout=0 as "wait forever"
# ---------------------------------------------------------------------------

def test_remaining_ms_caps_at_the_requested_amount(clock):
    deadline = clock.now + 30
    assert pw._remaining_ms(deadline, 2000) == 2000


def test_remaining_ms_shrinks_to_what_is_left(clock):
    deadline = clock.now + 1.2
    assert pw._remaining_ms(deadline, 5000) == 1200


def test_remaining_ms_never_returns_zero_on_an_exhausted_deadline(clock):
    """A 0 timeout means "no timeout" to Playwright — an exhausted deadline
    must never be handed to it as an unbounded wait."""
    assert pw._remaining_ms(clock.now, 5000) == 1
    assert pw._remaining_ms(clock.now - 60, 5000) == 1


# ---------------------------------------------------------------------------
# _wait_for_real_content — the budget is wall clock, not a tally of poll steps
# ---------------------------------------------------------------------------

def test_challenge_wait_stops_at_its_wall_clock_budget(clock):
    """Regression: the loop tallied poll_ms per round and ignored everything
    else each round cost (the networkidle wait, re-reading page content), so a
    nominal 25s budget ran for roughly twice that. It was the single largest
    contributor to an unbounded crawl."""
    page = _ForeverChallengePage(clock, {}, settle_cost_s=2.0)
    start = clock.now

    pw._wait_for_real_content(page, budget_ms=25000)

    elapsed = clock.now - start
    # The only permitted overrun is _remaining_ms' 1ms floor on the final round
    # (a 0 timeout would mean "wait forever" to Playwright), never a poll cycle.
    assert elapsed <= 25.0 + _FLOOR_SLACK_S, \
        f"challenge wait overran its budget by {elapsed - 25:.1f}s"
    # And it did actually wait — a budget honoured by never polling is no fix.
    assert elapsed >= 20.0


def test_challenge_wait_returns_as_soon_as_content_appears(clock):
    """A cleared challenge must not sit out the rest of the budget."""
    real = "<html><body>" + ("Fertility and OBGYN services in Duluth. " * 20) + "</body></html>"
    page = _FakePage(clock, {"": real})
    start = clock.now

    html, _ = pw._wait_for_real_content(page, budget_ms=25000)

    assert "Fertility" in html
    assert clock.now - start == 0


# ---------------------------------------------------------------------------
# crawl_with_playwright — one deadline governs the whole site
# ---------------------------------------------------------------------------

_BODY = "Our practice offers in-office procedures and self-pay pricing. " * 6
_HOME_URL = "https://slowsite.example/"
_SUBPATHS = ["services", "providers", "about", "contact", "billing",
             "insurance", "team", "pricing", "procedures", "financial"]


def _home_html() -> str:
    links = "".join(f'<a href="/{p}">{p}</a>' for p in _SUBPATHS)
    return f"<html><body><p>{_BODY}</p>{links}</body></html>"


class _FakeSession:
    """The `with sync_playwright() as pw:` context the crawler opens."""

    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_browser(monkeypatch, page):
    """Put a fake playwright.sync_api in sys.modules for the duration of a test."""
    context = SimpleNamespace(
        add_init_script=lambda script: None,
        new_page=lambda: page,
    )
    browser = SimpleNamespace(
        new_context=lambda **kwargs: context,
        close=lambda: None,
    )
    chromium = SimpleNamespace(launch=lambda **kwargs: browser)
    sync_api = SimpleNamespace(
        sync_playwright=lambda: _FakeSession(chromium),
        TimeoutError=_TimeoutError,
    )
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace(sync_api=sync_api))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


def test_slow_site_cannot_outrun_the_crawl_budget(clock, monkeypatch):
    """A site where every page takes 10s must stop at the deadline instead of
    walking all ten subpages and holding the worker for two minutes."""
    pages = {_HOME_URL: _home_html()}
    for p in _SUBPATHS:
        pages[_HOME_URL + p] = f"<html><body><p>{p} page. {_BODY}</p></body></html>"
    page = _FakePage(clock, pages, nav_cost_s=10.0, settle_cost_s=0.5)
    _install_fake_browser(monkeypatch, page)
    start = clock.now

    result = pw.crawl_with_playwright(
        url=_HOME_URL, max_pages=len(_SUBPATHS), timeout_ms=15000, max_seconds=60,
    )

    elapsed = clock.now - start
    assert elapsed <= 60.0, f"crawl overran its 60s budget by {elapsed - 60:.1f}s"
    # It stopped early rather than crawling everything on offer.
    assert len(result.pages_crawled) < 1 + len(_SUBPATHS)
    # Hitting the deadline is not an error: the homepage was fetched inside it,
    # and the pages already captured are returned rather than discarded.
    assert result.error == ""
    assert result.success is True
    assert "in-office procedures" in result.context_text


def test_fast_site_still_crawls_its_full_depth(clock, monkeypatch):
    """The deadline must not clip an ordinary site — it exists for the slow tail."""
    pages = {_HOME_URL: _home_html()}
    for p in _SUBPATHS:
        pages[_HOME_URL + p] = f"<html><body><p>{p} page. {_BODY}</p></body></html>"
    page = _FakePage(clock, pages, nav_cost_s=0.4, settle_cost_s=0.1)
    _install_fake_browser(monkeypatch, page)

    start = clock.now
    result = pw.crawl_with_playwright(
        url=_HOME_URL, max_pages=len(_SUBPATHS), timeout_ms=15000, max_seconds=60,
    )

    offered = pw._find_relevant_subpages(
        _home_html(), _HOME_URL, max_pages=len(_SUBPATHS),
        keywords=pw.DEFAULT_SUBPAGE_KEYWORDS,
    )
    assert len(result.pages_crawled) == 1 + len(offered)
    assert clock.now - start < 60.0


def test_challenge_wall_reports_blocked_within_the_budget(clock, monkeypatch):
    """A site that never clears its challenge burns its budget and reports the
    wall, rather than passing bot-verification text downstream as content."""
    page = _ForeverChallengePage(clock, {}, nav_cost_s=1.0, settle_cost_s=2.0)
    _install_fake_browser(monkeypatch, page)
    monkeypatch.setenv("PIPELINE_BROWSER_CHALLENGE_WAIT_MS", "25000")
    monkeypatch.delenv("PIPELINE_BROWSER_HEADFUL", raising=False)
    start = clock.now

    result = pw.crawl_with_playwright(url=_HOME_URL, max_pages=5, timeout_ms=15000)

    assert clock.now - start <= pw._crawl_budget_seconds(15000)
    assert result.success is False
    assert "challenge" in result.error.lower()
    assert result.context_text == ""
