"""
web_extractor.py
Extracts visible text from practice websites using requests + BeautifulSoup.
Identifies and crawls relevant subpages (services, providers, about, contact).
Phase 2: Playwright for JS-heavy sites. MVP: static HTML only.

batch_extract() sets source_confidence from extraction quality:
  2+ pages crawled AND > 3000 chars -> "complete"
  1 page crawled OR <= 3000 chars   -> "partial"
  No text extracted                 -> "limited"
It does not override a "limited"/"failed" confidence already set by
url_validator (those records failed before reaching web extraction).
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# Default headers to reduce bot-blocking.
# Missing Accept-Encoding and Upgrade-Insecure-Requests are common bot-detection signals.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}

# Generic, specialty-agnostic default keywords for relevant-subpage scoring.
# Specialty-specific terms belong in run_config.json ("subpage_keywords"),
# not in source — keeps the extractor portable across campaigns.
DEFAULT_SUBPAGE_KEYWORDS = [
    "service", "procedure", "treatment", "care",
    "provider", "physician", "doctor", "staff", "team", "about",
    "speciali", "contact",
]

# Max characters of text to extract per page (before combining)
MAX_CHARS_PER_PAGE = 8000

# Max combined characters across all pages for a single record
MAX_COMBINED_CHARS = 25000

# Tags whose content we always skip (not visible or not useful)
SKIP_TAGS = {
    "script", "style", "noscript", "meta", "head", "header",
    "footer", "nav", "form", "iframe", "img", "svg",
    "link", "button", "input", "select", "textarea",
}


class ExtractionResult:
    """Result of web text extraction for a single practice."""

    def __init__(self, url: str, context_text: str, pages_crawled: list[str],
                 error: str = ""):
        self.url = url
        self.context_text = context_text
        self.pages_crawled = pages_crawled
        self.error = error
        self.success = bool(context_text)


def _fetch_html(url: str, timeout: int = 15, retries: int = 3) -> tuple[str, str, str]:
    """
    Fetch HTML from a URL with retry logic.
    Returns (html_content, final_url, error) where error is "" on success.
    """
    last_error = ""
    for attempt in range(retries + 1):
        if attempt > 0:
            wait = 2 ** attempt
            time.sleep(wait)
        try:
            response = requests.get(
                url,
                headers=DEFAULT_HEADERS,
                timeout=timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
            # Respect content type — skip non-HTML
            ct = response.headers.get("content-type", "")
            if "html" not in ct.lower():
                return "", response.url, f"Non-HTML content type: {ct}"
            return response.text, response.url, ""
        except requests.exceptions.HTTPError as e:
            last_error = f"HTTP error: {e}"
            if hasattr(e, 'response') and e.response is not None and e.response.status_code < 500:
                break  # Don't retry 4xx
        except requests.exceptions.Timeout:
            last_error = f"Timeout after {timeout}s"
        except requests.exceptions.SSLError as e:
            last_error = f"SSL error: {str(e)[:80]}"
            break
        except Exception as e:
            last_error = f"Error: {str(e)[:80]}"
            break

    return "", url, last_error


def _extract_visible_text(html: str) -> str:
    """
    Parse HTML and extract visible text content.
    Strips tags, scripts, styles, and navigation noise.
    Returns cleaned plain text.
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "lxml")

    # Remove noise elements
    for tag in SKIP_TAGS:
        for element in soup.find_all(tag):
            element.decompose()

    # Get text
    text = soup.get_text(separator="\n", strip=True)

    # Clean up: collapse multiple blank lines, strip excess whitespace
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]  # remove empty lines
    cleaned = "\n".join(lines)

    # Collapse runs of 3+ newlines to 2
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    return cleaned[:MAX_CHARS_PER_PAGE]


def _find_relevant_subpages(html: str, base_url: str,
                              max_pages: int = 5,
                              keywords: list[str] = None) -> list[str]:
    """
    Parse the homepage HTML and find internal links to relevant subpages.
    Returns a list of absolute URLs to crawl (excluding base_url itself).
    """
    if not html:
        return []

    if keywords is None:
        keywords = DEFAULT_SUBPAGE_KEYWORDS

    soup = BeautifulSoup(html, "lxml")
    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc.lower()

    candidates = {}  # url → relevance score

    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "").strip()
        link_text = (a_tag.get_text() or "").lower().strip()

        # Skip empty, anchor-only, mailto, tel, js links
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        # Build absolute URL
        abs_url = urljoin(base_url, href).split("#")[0].rstrip("/")

        # Only follow links on the same domain
        parsed = urlparse(abs_url)
        if parsed.netloc.lower() != base_domain:
            continue

        # Skip if it's the base page itself
        if abs_url == base_url.rstrip("/"):
            continue

        # Score by keyword presence in URL path + link text
        combined = f"{parsed.path.lower()} {link_text}"
        score = sum(1 for kw in keywords if kw in combined)

        if score > 0 and abs_url not in candidates:
            candidates[abs_url] = score

    # Sort by relevance score, take top N (excluding base)
    sorted_urls = sorted(candidates.keys(), key=lambda u: candidates[u], reverse=True)
    return sorted_urls[: max_pages - 1]  # reserve 1 slot for homepage


def extract_practice_text(url: str, timeout: int = 15, retries: int = 3,
                            max_pages: int = 5,
                            keywords: list[str] = None) -> ExtractionResult:
    """
    Extract visible text from a practice website.
    Crawls homepage + up to (max_pages - 1) relevant subpages.

    Args:
        url: Practice homepage URL (already validated).
        timeout: Per-request timeout in seconds.
        retries: Retry attempts per request.
        max_pages: Maximum pages to crawl per practice.
        keywords: Subpage-relevance keywords (defaults to generic set).

    Returns:
        ExtractionResult with combined context_text and metadata.
    """
    if not url:
        return ExtractionResult(url="", context_text="",
                                 pages_crawled=[], error="No URL provided")

    pages_crawled = []
    all_text_blocks = []

    # Step 1: Fetch and extract homepage
    print(f"    Fetching homepage: {url}")
    html, final_url, fetch_error = _fetch_html(url, timeout=timeout, retries=retries)

    if not html:
        return ExtractionResult(
            url=url,
            context_text="",
            pages_crawled=[],
            error=fetch_error or "Could not fetch homepage HTML",
        )

    homepage_text = _extract_visible_text(html)
    if homepage_text:
        all_text_blocks.append(f"[Source: {final_url}]\n{homepage_text}")
        pages_crawled.append(final_url)

    # Step 2: Find and crawl relevant subpages
    subpages = _find_relevant_subpages(html, final_url, max_pages=max_pages, keywords=keywords)
    print(f"    Found {len(subpages)} relevant subpages to crawl")

    for subpage_url in subpages:
        if len(all_text_blocks) >= max_pages:
            break

        print(f"    Fetching subpage: {subpage_url}")
        time.sleep(0.5)  # Polite crawl delay

        sub_html, sub_final, _ = _fetch_html(subpage_url, timeout=timeout, retries=1)
        if sub_html:
            sub_text = _extract_visible_text(sub_html)
            if sub_text:
                all_text_blocks.append(f"[Source: {sub_final}]\n{sub_text}")
                pages_crawled.append(sub_final)

    # Step 3: Combine all text, trimmed to budget
    combined = "\n\n---\n\n".join(all_text_blocks)
    if len(combined) > MAX_COMBINED_CHARS:
        combined = combined[:MAX_COMBINED_CHARS] + "\n\n[... truncated for token budget ...]"

    return ExtractionResult(
        url=url,
        context_text=combined,
        pages_crawled=pages_crawled,
    )


def batch_extract(records: list[dict], timeout: int = 15,
                   retries: int = 3, max_pages: int = 5,
                   keywords: list[str] = None, max_workers: int = 1,
                   use_playwright: bool = False) -> list[dict]:
    """
    Run web extraction across all records with a valid URL.
    Updates each record in-place with extracted text and metadata.

    Network crawls run in a thread pool (max_workers); each record is
    extracted independently so one failure never affects the batch.

    Args:
        records: List of canonical records (after URL validation).
        timeout: Per-request timeout in seconds.
        retries: Retry attempts per request.
        max_pages: Maximum pages per practice.
        keywords: Subpage-relevance keywords (defaults to generic set).
        max_workers: Concurrent extraction workers (1 = sequential).
        use_playwright: If True, use headless Chromium instead of requests.

    Returns:
        The same records list with extraction fields added.
    """
    to_crawl = []
    for record in records:
        url = record.get("website_url", "")
        url_valid = record.get("_url_valid", False)
        # In playwright mode, attempt every record that has a URL — the requests-based
        # URL validator rejects bot-blocking sites that Playwright can reach just fine.
        has_url = bool(url)
        if not has_url or (not url_valid and not use_playwright):
            record["_context_text"] = ""
            record["_pages_crawled"] = []
            if not record.get("source_confidence"):
                record["source_confidence"] = "limited"
        else:
            to_crawl.append(record)

    _playwright_available = False
    if use_playwright:
        try:
            try:
                from playwright_extractor import crawl_with_playwright
            except ImportError:
                from extraction.playwright_extractor import crawl_with_playwright
            # Probe: verify the browser binary is reachable before committing to the pool
            from playwright.sync_api import sync_playwright
            with sync_playwright() as _pw:
                _b = _pw.chromium.launch(headless=True)
                _b.close()
            _playwright_available = True
            print("  [Playwright] Headless browser available — using Playwright for extraction")
        except Exception as _pw_err:
            print(f"  [Playwright] Browser not available ({_pw_err!s:.120}), falling back to requests")

    def _extract(record):
        try:
            if _playwright_available:
                result = crawl_with_playwright(
                    url=record.get("website_url", ""),
                    max_pages=max_pages,
                    keywords=keywords,
                    timeout_ms=timeout * 1000,
                )
                # If playwright returned empty, fall back to requests for this record
                if not result.success:
                    print(f"    [Playwright→requests fallback] {record.get('practice_name', '')}: {result.error}")
                    result = extract_practice_text(
                        url=record.get("website_url", ""), timeout=timeout,
                        retries=retries, max_pages=max_pages, keywords=keywords,
                    )
            else:
                result = extract_practice_text(
                    url=record.get("website_url", ""), timeout=timeout,
                    retries=retries, max_pages=max_pages, keywords=keywords,
                )
            return record, result, None
        except Exception as e:  # isolate per-record failure
            return record, None, str(e)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = [executor.submit(_extract, r) for r in to_crawl]
        for future in as_completed(futures):
            record, result, error = future.result()
            if error is not None or result is None:
                record["_context_text"] = ""
                record["_pages_crawled"] = []
                record["source_confidence"] = record.get("source_confidence") or "limited"
                print(f"    [FAIL] Extraction error: {record.get('practice_name', 'Unknown')}: {error}")
                continue

            record["_context_text"] = result.context_text
            record["_pages_crawled"] = result.pages_crawled

            if result.success:
                # Set source_confidence based on extraction richness.
                # Do not override "limited"/"failed" set upstream by url_validator.
                existing_conf = record.get("source_confidence")
                if existing_conf not in ("limited", "failed"):
                    pages_crawled = len(result.pages_crawled)
                    text_len = len(result.context_text)
                    if pages_crawled >= 2 and text_len > 3000:
                        record["source_confidence"] = "complete"
                    else:
                        record["source_confidence"] = "partial"
                print(f"    [OK] {record.get('practice_name', 'Unknown')}: "
                      f"{len(result.context_text)} chars from {len(result.pages_crawled)} pages")
            else:
                record["source_confidence"] = record.get("source_confidence") or "limited"
                print(f"    [FAIL] Extraction failed: {result.error}")

    return records
