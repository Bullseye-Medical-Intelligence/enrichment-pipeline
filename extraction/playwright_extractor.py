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
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                java_script_enabled=True,
            )
            page = context.new_page()

            # --- Homepage ---
            print(f"    [Playwright] Fetching homepage: {url}")
            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                # Brief settle wait for lazy-loaded content
                page.wait_for_timeout(1500)
                html = page.content()
                final_url = page.url
            except PlaywrightTimeout:
                browser.close()
                return ExtractionResult(url=url, context_text="", pages_crawled=[],
                                        error=f"Timeout after {timeout_ms}ms")
            except Exception as e:
                browser.close()
                return ExtractionResult(url=url, context_text="", pages_crawled=[],
                                        error=f"Navigation error: {str(e)[:120]}")

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
