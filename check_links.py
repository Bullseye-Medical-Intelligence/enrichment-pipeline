"""
check_links.py
Evidence link checker CLI — verifies that evidence source URLs still resolve
before a deliverable ships. Called by the API via subprocess (the API itself
makes no external HTTP calls); never invoked directly by operators.

Input (stdin JSON):  {"urls": ["https://...", ...]}
Output (stdout JSON): {"results": [{"url", "classification", "detail", "final_url"}]}

Classification:
  OK    — 2xx, or a redirect chain ending 2xx on the same domain (not a
          path-to-homepage collapse)
  FLAG  — redirect to a different domain, or a URL with a path redirecting
          to the bare homepage (the evidence page is probably gone)
  DEAD  — 4xx / 5xx / timeout / DNS failure / redirect loop

Politeness: max 2 concurrent requests, 500ms minimum spacing between requests
to the same domain, 10s timeout. These are small practice websites.
"""

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

REQUEST_TIMEOUT_SECONDS = 10
MAX_WORKERS = 2
SAME_DOMAIN_DELAY_SECONDS = 0.5
MAX_REDIRECT_HOPS = 5
GET_BYTE_CAP = 2048
_USER_AGENT = "Mozilla/5.0 (compatible; BEMI-LinkChecker/1.0)"

_domain_last_request: dict[str, float] = {}
_domain_lock = threading.Lock()


def _normalized_domain(url: str) -> str:
    """Lowercased host with any www. prefix dropped, for same-domain checks."""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _respect_domain_delay(url: str) -> None:
    """Sleep as needed so requests to one domain are >= 500ms apart."""
    domain = _normalized_domain(url)
    with _domain_lock:
        last = _domain_last_request.get(domain, 0.0)
        wait = SAME_DOMAIN_DELAY_SECONDS - (time.monotonic() - last)
        _domain_last_request[domain] = time.monotonic() + max(0.0, wait)
    if wait > 0:
        time.sleep(wait)


def _fetch_status(url: str) -> tuple[int, str]:
    """One polite request: HEAD first, GET (byte-capped) on 405/501.

    Returns (status_code, location_header). Redirects are NOT followed here —
    the caller walks the chain so every hop can be classified.
    """
    _respect_domain_delay(url)
    headers = {"User-Agent": _USER_AGENT}
    resp = requests.head(url, timeout=REQUEST_TIMEOUT_SECONDS,
                         allow_redirects=False, headers=headers)
    if resp.status_code in (405, 501):
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS,
                            allow_redirects=False, headers=headers, stream=True)
        try:
            next(resp.iter_content(GET_BYTE_CAP), None)
        finally:
            resp.close()
    return resp.status_code, resp.headers.get("Location", "")


def classify_chain(original_url: str, final_url: str, final_status: int) -> tuple[str, str]:
    """Classify a resolved redirect chain (pure logic, unit-testable).

    A redirect is OK only when it stays on the same domain AND does not
    collapse a specific evidence path to the bare homepage.
    """
    if not 200 <= final_status < 300:
        return "DEAD", f"final status {final_status}"
    if final_url == original_url:
        return "OK", ""
    if _normalized_domain(final_url) != _normalized_domain(original_url):
        return "FLAG", f"redirects to different domain ({_normalized_domain(final_url)})"
    original_path = urlparse(original_url).path.rstrip("/")
    final_path = urlparse(final_url).path.rstrip("/")
    if original_path and not final_path:
        return "FLAG", "page redirects to the bare homepage — evidence page likely gone"
    return "OK", ""


def check_url(url: str, fetch=_fetch_status) -> dict:
    """Resolve one URL (following redirects manually) and classify it."""
    current = url
    try:
        for _ in range(MAX_REDIRECT_HOPS + 1):
            status, location = fetch(current)
            if 300 <= status < 400 and location:
                from urllib.parse import urljoin
                current = urljoin(current, location)
                continue
            classification, detail = classify_chain(url, current, status)
            return {"url": url, "classification": classification,
                    "detail": detail, "final_url": current}
        return {"url": url, "classification": "DEAD",
                "detail": "redirect loop (too many hops)", "final_url": current}
    except requests.exceptions.Timeout:
        return {"url": url, "classification": "DEAD",
                "detail": "timeout", "final_url": current}
    except requests.exceptions.RequestException as e:
        return {"url": url, "classification": "DEAD",
                "detail": f"connection failed: {type(e).__name__}", "final_url": current}


def check_urls(urls: list[str]) -> list[dict]:
    """Check a list of unique URLs with bounded concurrency."""
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(check_url, u) for u in urls]
        for future in as_completed(futures):
            results.append(future.result())
    order = {u: i for i, u in enumerate(urls)}
    results.sort(key=lambda r: order.get(r["url"], 0))
    return results


def main() -> int:
    """Read {"urls": [...]} from stdin, write {"results": [...]} to stdout."""
    try:
        payload = json.load(sys.stdin)
        urls = [u for u in payload.get("urls", []) if isinstance(u, str) and u.strip()]
    except (json.JSONDecodeError, AttributeError) as e:
        print(json.dumps({"error": f"invalid input: {e}"}), file=sys.stdout)
        return 1
    seen = set()
    unique = [u for u in urls if not (u in seen or seen.add(u))]
    print(json.dumps({"results": check_urls(unique)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
