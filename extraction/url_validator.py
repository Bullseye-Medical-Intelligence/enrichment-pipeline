"""
url_validator.py
Validates website URLs via HEAD requests.
Checks reachability, follows redirects, and categorizes failures.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests


# Default headers to mimic a real browser — reduces bot-blocking
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class UrlValidationResult:
    """Result of a URL validation check."""

    def __init__(self, url: str, is_valid: bool, final_url: str = "",
                 status_code: int = 0, error: str = ""):
        self.url = url
        self.is_valid = is_valid
        self.final_url = final_url or url
        self.status_code = status_code
        self.error = error

    def __repr__(self):
        return (
            f"UrlValidationResult(url={self.url!r}, is_valid={self.is_valid}, "
            f"status_code={self.status_code}, error={self.error!r})"
        )


def validate_url(url: str, timeout: int = 15, retries: int = 3) -> UrlValidationResult:
    """
    Validate a URL by making a HEAD request (with GET fallback).
    Follows redirects up to 5 hops.

    Args:
        url: The URL to validate.
        timeout: Request timeout in seconds.
        retries: Number of retry attempts on transient failure.

    Returns:
        UrlValidationResult with validation outcome.
    """
    if not url:
        return UrlValidationResult(url, False, error="Empty URL")

    # Basic URL sanity check
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return UrlValidationResult(url, False, error=f"Malformed URL: {url}")

    result = _attempt_validation(url, timeout=timeout, retries=retries)

    # Many smaller practice sites have broken HTTPS but respond on plain HTTP.
    # If the https:// attempt failed with a connection or SSL error, retry once
    # on http:// before giving up.
    if not result.is_valid and url.startswith("https://"):
        error_lower = result.error.lower()
        if "ssl" in error_lower or "connection" in error_lower:
            http_url = "http://" + url[len("https://"):]
            http_result = _attempt_validation(http_url, timeout=timeout, retries=1)
            if http_result.is_valid:
                return http_result

    return result


def _attempt_validation(url: str, timeout: int, retries: int) -> UrlValidationResult:
    """Internal: attempt HEAD then GET with retry logic."""
    last_error = ""
    for attempt in range(retries + 1):
        if attempt > 0:
            wait = 2 ** attempt  # 2s, 4s, 8s
            time.sleep(wait)

        try:
            # Try HEAD first (lighter)
            response = requests.head(
                url,
                headers=DEFAULT_HEADERS,
                timeout=timeout,
                allow_redirects=True,
            )
            # Some servers reject HEAD with 403/405/406; fall back to GET
            if response.status_code in (403, 405, 406):
                response = requests.get(
                    url,
                    headers=DEFAULT_HEADERS,
                    timeout=timeout,
                    allow_redirects=True,
                    stream=True,  # Don't download full body
                )
                response.close()

            final_url = response.url
            status = response.status_code

            # 2xx and 3xx (after following) = valid
            if 200 <= status < 400:
                return UrlValidationResult(
                    url=url,
                    is_valid=True,
                    final_url=final_url,
                    status_code=status,
                )
            # 4xx/5xx = technically reachable but broken
            elif 400 <= status < 600:
                last_error = f"HTTP {status}"
                # Don't retry on 4xx (client error), retry on 5xx
                if status < 500:
                    break

        except requests.exceptions.SSLError as e:
            last_error = f"SSL error: {str(e)[:100]}"
            break  # SSL errors won't be fixed by retry
        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error: {str(e)[:100]}"
        except requests.exceptions.Timeout:
            last_error = f"Timeout after {timeout}s"
        except requests.exceptions.TooManyRedirects:
            last_error = "Too many redirects"
            break
        except Exception as e:
            last_error = f"Unexpected error: {str(e)[:100]}"
            break

    return UrlValidationResult(
        url=url,
        is_valid=False,
        status_code=0,
        error=last_error or "Validation failed",
    )


def batch_validate_urls(records: list[dict], timeout: int = 15,
                         retries: int = 3, max_workers: int = 1) -> list[dict]:
    """
    Run URL validation across all records with website_url set.
    Updates each record in-place with validation results.

    Network calls run in a thread pool (max_workers); each record is
    validated independently so one failure never affects the batch.

    Args:
        records: List of canonical records.
        timeout: Per-request timeout in seconds.
        retries: Retry attempts per URL.
        max_workers: Concurrent validation workers (1 = sequential).

    Returns:
        The same records list with url_validation_* fields added.
    """
    validated = 0
    skipped = 0
    failed = 0

    to_check = []
    for record in records:
        url = record.get("website_url", "")
        if not url:
            record["_url_valid"] = False
            record["_url_final"] = ""
            record["_url_error"] = "No URL provided"
            record["source_confidence"] = "limited"
            skipped += 1
        else:
            to_check.append(record)

    def _validate(record):
        url = record.get("website_url", "")
        try:
            return record, validate_url(url, timeout=timeout, retries=retries), None
        except Exception as e:  # isolate per-record failure
            return record, None, str(e)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = [executor.submit(_validate, r) for r in to_check]
        for future in as_completed(futures):
            record, result, error = future.result()
            if error is not None or result is None:
                record["_url_valid"] = False
                record["_url_final"] = ""
                record["_url_error"] = error or "Validation error"
                record["source_confidence"] = "limited"
                failed += 1
                print(f"    [FAIL] FAILED: {record.get('website_url', '')}: {error}")
                continue

            record["_url_valid"] = result.is_valid
            record["_url_final"] = result.final_url
            record["_url_error"] = result.error
            if result.is_valid:
                record["website_url"] = result.final_url
                validated += 1
            else:
                record["source_confidence"] = "limited"
                failed += 1
                print(f"    [FAIL] FAILED: {result.error}")

    print(f"[url_validator] {validated} valid, {failed} failed, {skipped} skipped (no URL)")
    return records
