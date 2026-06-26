"""
playwright_extractor.py
Headless-browser text extraction for JS-heavy or bot-blocking practice sites.
Uses Playwright (Chromium) as a drop-in replacement for the requests path.
Returns ExtractionResult — same shape as web_extractor.extract_practice_text.

Runs Playwright synchronously inside a thread (called from batch_extract's
ThreadPoolExecutor) so the async Playwright API is not required.
"""

import os
import re
import sys
from pathlib import Path

# Allow both `import playwright_extractor` (run from extraction/) and
# `from extraction.playwright_extractor import ...` (run from repo root).
_this_dir = str(Path(__file__).parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

from web_extractor import (  # noqa: E402
    ExtractionResult,
    MAX_CHARS_PER_PAGE,
    MAX_COMBINED_CHARS,
    DEFAULT_SUBPAGE_KEYWORDS,
    _find_relevant_subpages,
    looks_like_challenge,
)


# Launch flags that suppress the headless automation tells bot-protection
# (Cloudflare, etc.) fingerprints on. --disable-blink-features=AutomationControlled
# removes the navigator.webdriver banner; the rest quiet sandbox/automation noise.
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

# Injected before any page script runs. Masks the remaining headless tells a
# challenge page checks: navigator.webdriver, empty plugins, missing languages.
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""

# Realistic desktop Chrome UA — no "HeadlessChrome" token, which is an instant tell.
_REAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _headful_requested() -> bool:
    """True when the operator asked for a visible (headed) browser.

    A real headed window clears Cloudflare / Turnstile JS challenges far more
    reliably than headless Chromium, which those challenges are tuned to detect
    and never clear. Set PIPELINE_BROWSER_HEADFUL=1 on a machine with a display
    (e.g. the operator's laptop) for the toughest bot-gated sites.
    """
    return os.environ.get("PIPELINE_BROWSER_HEADFUL", "").strip().lower() in ("1", "true", "yes")


def launch_browser(pw):
    """Launch a browser, preferring the bundled Chromium.

    Falls back to an installed Google Chrome, then Microsoft Edge, when the
    bundled Chromium binary was never downloaded (`playwright install chromium`
    not run). This lets the browser path work on a machine that has Chrome but
    no Playwright browser download. Raises the original error if nothing launches.

    When PIPELINE_BROWSER_HEADFUL is set, a headed window is tried first (best
    for bot challenges) and headless is the fallback if no display is available.
    Stealth launch flags are always applied to quiet the automation fingerprint.
    """
    headless_modes = [False, True] if _headful_requested() else [True]
    channels = [{}, {"channel": "chrome"}, {"channel": "msedge"}]
    last_error = None
    for headless in headless_modes:
        for opts in channels:
            try:
                return pw.chromium.launch(headless=headless, args=_STEALTH_ARGS, **opts)
            except Exception as e:  # try the next channel / mode
                last_error = e
    raise last_error


# Bot-interstitial detection (markers + looks_like_challenge) is shared with the
# requests crawler in web_extractor so both paths classify a challenge wall the
# same way; imported above. _looks_like_challenge keeps the module-local name.
_looks_like_challenge = looks_like_challenge


# Minimum rendered text (chars) that counts as "real content has loaded" — used
# to know a JS challenge has actually handed off to the site, not just changed
# its own wording.
_MIN_REAL_TEXT = 200

# How long to keep waiting for a challenge to clear, total, in ms. These pages
# typically run a 5-10s timer; give generous headroom. Operator-overridable.
def _challenge_wait_budget_ms() -> int:
    try:
        return max(5000, int(os.environ.get("PIPELINE_BROWSER_CHALLENGE_WAIT_MS", "25000")))
    except ValueError:
        return 25000


def _human_nudge(page) -> None:
    """Small mouse move + scroll so a JS challenge sees human-like interaction."""
    try:
        page.mouse.move(240, 260)
        page.mouse.wheel(0, 600)
        page.mouse.move(520, 420)
    except Exception:
        pass


def _wait_for_real_content(page, budget_ms: int, poll_ms: int = 2000):
    """Wait out a JS/Cloudflare challenge until real content appears.

    Polls on a steady cadence, nudging like a human each round, until the page is
    no longer a challenge wall AND has rendered meaningful text — or the budget is
    exhausted. Returns (html, final_url). Patience is the point: these interstitials
    clear themselves on a timer (often 5-10s) and only then redirect to the site.
    """
    html = page.content()
    final_url = page.url
    waited = 0
    while _looks_like_challenge(html) or len(_extract_text_from_html(html)) < _MIN_REAL_TEXT:
        if waited >= budget_ms:
            break
        _human_nudge(page)
        page.wait_for_timeout(poll_ms)
        waited += poll_ms
        # Let any challenge-triggered navigation/network settle before re-reading.
        try:
            page.wait_for_load_state("networkidle", timeout=poll_ms)
        except Exception:
            pass
        html = page.content()
        final_url = page.url
    return html, final_url


def _extract_text_from_html(html: str) -> str:
    """Strip tags and collapse whitespace — same logic as web_extractor.
    Also surfaces <img> alt/title/src-basename as [image: ...] tokens."""
    from bs4 import BeautifulSoup
    from web_extractor import SKIP_TAGS

    soup = BeautifulSoup(html, "lxml")

    # Collect image metadata before img tags are removed by SKIP_TAGS.
    img_tokens = []
    for img in soup.find_all("img"):
        parts = []
        alt      = (img.get("alt")   or "").strip()
        title    = (img.get("title") or "").strip()
        src      = (img.get("src")   or "").strip()
        src_name = src.rsplit("/", 1)[-1].rsplit("?", 1)[0] if src else ""
        if alt:
            parts.append(alt)
        if title and title != alt:
            parts.append(title)
        if src_name and src_name != alt and src_name != title:
            parts.append(src_name)
        if parts:
            img_tokens.append("[image: " + " | ".join(parts) + "]")

    for tag in SKIP_TAGS:
        for el in soup.find_all(tag):
            el.decompose()
    text = soup.get_text(separator="\n", strip=True)
    if img_tokens:
        text = text + "\n\n" + "\n".join(img_tokens)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned[:MAX_CHARS_PER_PAGE]


def crawl_with_playwright(
    url: str,
    max_pages: int = 5,
    keywords: list[str] | None = None,
    timeout_ms: int = 30000,
) -> ExtractionResult:
    """Extract visible text using a headless Chromium browser.

    Falls back gracefully: if Playwright is not installed or the page fails,
    returns an empty ExtractionResult with the error recorded.

    Args:
        url: Practice homepage URL.
        max_pages: Maximum pages to crawl.
        keywords: Subpage-relevance keywords (defaults to generic set).
        timeout_ms: Per-navigation timeout in milliseconds.

    Returns:
        ExtractionResult with combined context_text and metadata.
    """
    if not url:
        return ExtractionResult(url="", context_text="", pages_crawled=[], error="No URL provided")

    if keywords is None:
        keywords = DEFAULT_SUBPAGE_KEYWORDS

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return ExtractionResult(
            url=url, context_text="", pages_crawled=[],
            error="Playwright not installed — run: playwright install chromium",
        )

    pages_crawled = []
    all_text_blocks = []
    evidence_pages = []

    try:
        with sync_playwright() as pw:
            browser = launch_browser(pw)
            try:
                context = browser.new_context(
                    user_agent=_REAL_USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    java_script_enabled=True,
                    locale="en-US",
                    timezone_id="America/Chicago",
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept": ("text/html,application/xhtml+xml,application/xml;"
                                   "q=0.9,image/avif,image/webp,*/*;q=0.8"),
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "none",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                # Mask headless tells before any page script runs.
                context.add_init_script(_STEALTH_INIT_SCRIPT)
                page = context.new_page()

                # --- Homepage ---
                print(f"    [Playwright] Fetching homepage: {url}")
                try:
                    page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                    # Let the page (and any JS challenge timer) run: settle to network
                    # idle if we can, then a brief pause for lazy content.
                    try:
                        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8000))
                    except Exception:
                        pass
                    page.wait_for_timeout(1500)
                    html = page.content()
                    final_url = page.url
                    # JS-based challenge pages (Cloudflare "Just a moment...") clear on
                    # a 5-10s timer and only then redirect to real content. Wait it out
                    # patiently — nudging like a human — instead of grabbing the wall.
                    if _looks_like_challenge(html) or len(_extract_text_from_html(html)) < _MIN_REAL_TEXT:
                        budget = _challenge_wait_budget_ms()
                        print(f"    [Playwright] Challenge/thin page — waiting up to {budget // 1000}s for it to clear...")
                        html, final_url = _wait_for_real_content(page, budget)
                        if not _looks_like_challenge(html):
                            print("    [Playwright] Challenge cleared / content loaded.")
                except PlaywrightTimeout:
                    return ExtractionResult(url=url, context_text="", pages_crawled=[],
                                            error=f"Timeout after {timeout_ms}ms")
                except Exception as e:
                    return ExtractionResult(url=url, context_text="", pages_crawled=[],
                                            error=f"Navigation error: {str(e)[:120]}")

                # If the page is still a challenge wall, report it clearly rather
                # than passing the bot-verification text downstream as "content".
                if _looks_like_challenge(html):
                    hint = (
                        "" if _headful_requested()
                        else " — retry with PIPELINE_BROWSER_HEADFUL=1 (a visible window "
                             "clears most of these), or paste the page content manually"
                    )
                    return ExtractionResult(
                        url=url, context_text="", pages_crawled=[],
                        error=f"Blocked by bot/security challenge (CAPTCHA wall){hint}",
                    )

                homepage_text = _extract_text_from_html(html)
                if homepage_text:
                    all_text_blocks.append(f"[Source: {final_url}]\n{homepage_text}")
                    pages_crawled.append(final_url)
                    evidence_pages.append({"url": final_url, "text": homepage_text})

                # --- Subpages ---
                subpages = _find_relevant_subpages(html, final_url, max_pages=max_pages, keywords=keywords)
                print(f"    [Playwright] Found {len(subpages)} subpages")

                for sub_url in subpages:
                    if len(all_text_blocks) >= max_pages:
                        break
                    print(f"    [Playwright] Fetching subpage: {sub_url}")
                    try:
                        page.goto(sub_url, timeout=timeout_ms, wait_until="domcontentloaded")
                        page.wait_for_timeout(800)
                        sub_html = page.content()
                        sub_final = page.url
                        sub_text = _extract_text_from_html(sub_html)
                        if sub_text:
                            all_text_blocks.append(f"[Source: {sub_final}]\n{sub_text}")
                            pages_crawled.append(sub_final)
                            evidence_pages.append({"url": sub_final, "text": sub_text})
                    except Exception:
                        pass  # Skip failed subpages; homepage text is still valuable
            finally:
                # Guarantee Chromium closes even if an exception fires mid-crawl, so a
                # failed record can never leak a zombie browser process. close() is
                # wrapped so a teardown error cannot mask the real failure.
                try:
                    browser.close()
                except Exception:
                    pass

    except Exception as e:
        return ExtractionResult(url=url, context_text="", pages_crawled=[],
                                error=f"Playwright session error: {str(e)[:120]}")

    combined = "\n\n---\n\n".join(all_text_blocks)
    if len(combined) > MAX_COMBINED_CHARS:
        combined = combined[:MAX_COMBINED_CHARS] + "\n\n[... truncated for token budget ...]"

    return ExtractionResult(url=url, context_text=combined, pages_crawled=pages_crawled,
                            pages=evidence_pages)
