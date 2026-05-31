"""
playwright_extractor.py
Headless-browser text extraction for JS-heavy or bot-blocking practice sites.
Uses Playwright (Chromium) as a drop-in replacement for the requests path.
Returns ExtractionResult — same shape as web_extractor.extract_practice_text.

Runs Playwright synchronously inside a thread (called from batch_extract's
ThreadPoolExecutor) so the async Playwright API is not required.
"""

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


def launch_browser(pw):
    """Launch a headless browser, preferring the bundled Chromium.

    Falls back to an installed Google Chrome, then Microsoft Edge, when the
    bundled Chromium binary was never downloaded (`playwright install chromium`
    not run). This lets the browser path work on a machine that has Chrome but
    no Playwright browser download. Raises the original error if nothing launches.

    Stealth launch flags are applied so bot-protection pages are less likely to
    serve a CAPTCHA wall in response to the headless automation fingerprint.
    """
    attempts = [
        {},                       # bundled Chromium (playwright install chromium)
        {"channel": "chrome"},    # installed Google Chrome
        {"channel": "msedge"},    # installed Microsoft Edge
    ]
    last_error = None
    for opts in attempts:
        try:
            return pw.chromium.launch(headless=True, args=_STEALTH_ARGS, **opts)
        except Exception as e:  # try the next channel
            last_error = e
    raise last_error


# Phrases that mark a bot/security interstitial rather than real site content.
_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "verifying you are human",
    "enable javascript and cookies to continue",
    "attention required",
    "cf-browser-verification",
    "cf-challenge",
    "ddos protection by",
    "please verify you are a human",
    "ray id",
)


def _looks_like_challenge(html: str) -> bool:
    """True when the HTML is a bot/security verification page, not site content."""
    if not html:
        return False
    sample = html[:4000].lower()
    return any(marker in sample for marker in _CHALLENGE_MARKERS)


def _extract_text_from_html(html: str) -> str:
    """Strip tags and collapse whitespace — same logic as web_extractor."""
    from bs4 import BeautifulSoup
    from web_extractor import SKIP_TAGS

    soup = BeautifulSoup(html, "lxml")
    for tag in SKIP_TAGS:
        for el in soup.find_all(tag):
            el.decompose()
    text = soup.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned[:MAX_CHARS_PER_PAGE]


def crawl_with_playwright(
    url: str,
    max_pages: int = 5,
    keywords: list[str] | None = None,
    timeout_ms: int = 20000,
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

    try:
        with sync_playwright() as pw:
            browser = launch_browser(pw)
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
                # Brief settle wait for lazy-loaded content
                page.wait_for_timeout(1500)
                html = page.content()
                final_url = page.url
                # JS-based challenge pages (Cloudflare "Just a moment...") clear
                # themselves and redirect to real content after a few seconds.
                # Wait it out and re-read instead of capturing the interstitial.
                if _looks_like_challenge(html):
                    print("    [Playwright] Security challenge detected, waiting for it to clear...")
                    for _ in range(3):
                        page.wait_for_timeout(3500)
                        html = page.content()
                        final_url = page.url
                        if not _looks_like_challenge(html):
                            print("    [Playwright] Challenge cleared.")
                            break
            except PlaywrightTimeout:
                browser.close()
                return ExtractionResult(url=url, context_text="", pages_crawled=[],
                                        error=f"Timeout after {timeout_ms}ms")
            except Exception as e:
                browser.close()
                return ExtractionResult(url=url, context_text="", pages_crawled=[],
                                        error=f"Navigation error: {str(e)[:120]}")

            # If the page is still a challenge wall, report it clearly rather
            # than passing the bot-verification text downstream as "content".
            if _looks_like_challenge(html):
                browser.close()
                return ExtractionResult(
                    url=url, context_text="", pages_crawled=[],
                    error="Blocked by bot/security challenge (CAPTCHA wall)",
                )

            homepage_text = _extract_text_from_html(html)
            if homepage_text:
                all_text_blocks.append(f"[Source: {final_url}]\n{homepage_text}")
                pages_crawled.append(final_url)

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
                except Exception:
                    pass  # Skip failed subpages; homepage text is still valuable

            browser.close()

    except Exception as e:
        return ExtractionResult(url=url, context_text="", pages_crawled=[],
                                error=f"Playwright session error: {str(e)[:120]}")

    combined = "\n\n---\n\n".join(all_text_blocks)
    if len(combined) > MAX_COMBINED_CHARS:
        combined = combined[:MAX_COMBINED_CHARS] + "\n\n[... truncated for token budget ...]"

    return ExtractionResult(url=url, context_text=combined, pages_crawled=pages_crawled)
