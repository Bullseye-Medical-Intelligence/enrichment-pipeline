"""
update_site_intel.py — download every HTML page from Hostinger, apply three
changes (Intelligence nav link, Intelligence footer column, OG/SEO meta tags),
and re-upload.

Dry-run by default.  Pass --upload to write changes to the live site.

Reads credentials from pipeline-api/.env.

Usage:
    python update_site_intel.py            # dry-run (inspect only)
    python update_site_intel.py --upload   # live upload
"""

from __future__ import annotations

import ftplib
import io
import os
import sys
from pathlib import Path

# ── BeautifulSoup4 guard ─────────────────────────────────────
try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit(
        "BeautifulSoup4 is required but not installed.\n"
        "Install it with:  pip install beautifulsoup4"
    )

# ── Load .env ────────────────────────────────────────────────
_env_path = Path(__file__).parent / "pipeline-api" / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

HOST = os.environ.get("HOSTINGER_SFTP_HOST", "")
USER = os.environ.get("HOSTINGER_SFTP_USER", "")
PASSWORD = os.environ.get("HOSTINGER_SFTP_PASSWORD", "")
REMOTE_ROOT = ""  # FTP home is already the web root on Hostinger

if not all([HOST, USER, PASSWORD]):
    sys.exit("Missing HOSTINGER_SFTP_HOST / USER / PASSWORD in pipeline-api/.env")

# ── Per-page meta overrides ───────────────────────────────────
# Hand-edit this dict to override title/description/og_type per page.
# Key = FTP path as downloaded (e.g. "index.html", "targeting-tax.html",
#       "intelligence/my-article.html").
# Absent fields fall back to: title from existing <title> tag, generic description.
PAGE_META: dict[str, dict[str, str]] = {
    "index.html": {
        "title": "Bullseye Medical Intelligence — Physician Targeting Research",
        "description": (
            "We research, score, and prioritize physician practices so your reps "
            "know who to call, why to call them, and what to say when they get there."
        ),
        "og_type": "website",
        "canonical_url": "https://www.bullseyemedical.ai/",
    },
    "targeting-tax.html": {
        "title": "The Targeting Tax — Bullseye Medical Intelligence",
        "description": (
            "Your reps spend 40% of their week on research, not selling. "
            "Bullseye eliminates the targeting tax with pre-scored physician intelligence."
        ),
        "og_type": "article",
        "canonical_url": "https://www.bullseyemedical.ai/targeting-tax",
    },
    "intelligence/index.html": {
        "title": "Field Notes — Bullseye Medical Intelligence",
        "description": (
            "Market intelligence essays, practice targeting insights, and "
            "go-to-market analysis for medical device and specialty therapeutics teams."
        ),
        "og_type": "website",
        "canonical_url": "https://www.bullseyemedical.ai/intelligence/",
    },
}

# Fallback values used when a page is not in PAGE_META
_GENERIC_DESCRIPTION = (
    "Bullseye Medical Intelligence — physician targeting research for "
    "medical device and specialty therapeutics teams."
)
_OG_IMAGE = "https://www.bullseyemedical.ai/assets/og-image.png"
_SITE_NAME = "Bullseye Medical Intelligence"


# ── Meta helpers ──────────────────────────────────────────────

def _canonical_url_for_path(ftp_path: str) -> str:
    """Derive a canonical URL from an FTP path by stripping trailing index.html."""
    base = "https://www.bullseyemedical.ai/"
    if ftp_path == "index.html":
        return base
    path = ftp_path
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]  # keep trailing slash
    return base + path


def _og_type_for_path(ftp_path: str) -> str:
    """Return 'article' for intelligence pages, 'website' otherwise."""
    return "article" if ftp_path.startswith("intelligence/") else "website"


def _meta_for_page(ftp_path: str, soup: BeautifulSoup) -> dict[str, str]:
    """Build the full meta dict for a page, merging PAGE_META overrides."""
    override = PAGE_META.get(ftp_path, {})

    # Title: override -> existing <title> tag -> site name
    title = override.get("title", "")
    if not title:
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else _SITE_NAME

    description = override.get("description", _GENERIC_DESCRIPTION)
    og_type = override.get("og_type", _og_type_for_path(ftp_path))
    canonical_url = override.get("canonical_url", _canonical_url_for_path(ftp_path))

    return {
        "title": title,
        "description": description,
        "og_type": og_type,
        "canonical_url": canonical_url,
    }


# ── Soup helpers ──────────────────────────────────────────────

def _upsert_meta(soup: BeautifulSoup, name: str | None, prop: str | None,
                 value: str) -> None:
    """Insert or update a <meta> tag in <head>.  Matches by name= or property=."""
    head = soup.find("head")
    if head is None:
        return
    existing = None
    if name:
        existing = head.find("meta", attrs={"name": name})
    elif prop:
        existing = head.find("meta", attrs={"property": prop})
    if existing:
        existing["content"] = value
    else:
        tag = soup.new_tag("meta")
        if name:
            tag["name"] = name
        if prop:
            tag["property"] = prop
        tag["content"] = value
        head.append(tag)


def _upsert_canonical(soup: BeautifulSoup, url: str) -> None:
    """Insert or update <link rel='canonical'> in <head>."""
    head = soup.find("head")
    if head is None:
        return
    existing = head.find("link", attrs={"rel": "canonical"})
    if existing:
        existing["href"] = url
    else:
        tag = soup.new_tag("link", rel="canonical", href=url)
        head.append(tag)


def _apply_meta_tags(soup: BeautifulSoup, page_meta: dict[str, str]) -> int:
    """Upsert all OG/SEO meta tags.  Returns the count of tags upserted."""
    title = page_meta["title"]
    description = page_meta["description"]
    og_type = page_meta["og_type"]
    canonical_url = page_meta["canonical_url"]

    # (name, property, value)
    meta_ops = [
        ("description", None, description),
        (None, "og:title", title),
        (None, "og:description", description),
        (None, "og:image", _OG_IMAGE),
        (None, "og:url", canonical_url),
        (None, "og:type", og_type),
        (None, "og:site_name", _SITE_NAME),
        ("twitter:card", None, "summary_large_image"),
        ("twitter:title", None, title),
        ("twitter:description", None, description),
        ("twitter:image", None, _OG_IMAGE),
    ]

    for name, prop, value in meta_ops:
        _upsert_meta(soup, name, prop, value)

    _upsert_canonical(soup, canonical_url)

    return len(meta_ops) + 1  # +1 for canonical link


# ── Nav change ────────────────────────────────────────────────

def _apply_nav(soup: BeautifulSoup, ftp_path: str) -> str | None:
    """Add Intelligence nav link.

    Returns:
        change description string if a change was made,
        "skip" if already present (idempotent),
        None if <ul class='nav-links'> was not found.
    """
    nav_ul = soup.find("ul", class_="nav-links")
    if nav_ul is None:
        return None

    # Idempotency: any <a href="/intelligence/"> already in the ul?
    for a_tag in nav_ul.find_all("a"):
        if a_tag.get("href", "") == "/intelligence/":
            return "skip"

    li = soup.new_tag("li")
    a = soup.new_tag("a", href="/intelligence/")
    a["class"] = "nav-link"
    a.string = "Intelligence"
    li.append(a)
    nav_ul.append(li)
    return "added Intelligence to nav"


# ── Footer change ─────────────────────────────────────────────

def _apply_footer(soup: BeautifulSoup, ftp_path: str) -> str | None:
    """Add Intelligence footer column.

    Returns:
        change description string if a change was made,
        "skip" if already present (idempotent),
        None if no footer-links container was found.
    """
    # Look for <div class="footer-links"> first
    footer_links = soup.find("div", class_="footer-links")
    if footer_links is None:
        # Fallback: find a div that contains at least one .footer-col child
        for div in soup.find_all("div"):
            if div.find(class_="footer-col"):
                footer_links = div
                break

    if footer_links is None:
        return None

    # Idempotency: any element already containing "Intelligence" text?
    if footer_links.find(string=lambda s: s and "Intelligence" in s):
        return "skip"

    col_html = (
        '<div class="footer-col">'
        '<span class="footer-col-title">Intelligence</span>'
        '<a href="/intelligence/">Field Notes</a>'
        "</div>"
    )
    col_soup = BeautifulSoup(col_html, "html.parser")
    footer_links.append(col_soup)
    return "added Intelligence to footer"


# ── Main update function ──────────────────────────────────────

def _update_html(html: str, ftp_path: str) -> tuple[str, list[str]]:
    """Apply all three changes to one HTML string.

    Returns (new_html, list_of_change_descriptions).
    """
    soup = BeautifulSoup(html, "html.parser")
    changes: list[str] = []

    # 1. Nav link
    nav_result = _apply_nav(soup, ftp_path)
    if nav_result is None:
        print(f"    [WARN] No <ul class='nav-links'> found — skipping nav change")
    elif nav_result == "skip":
        pass  # already present, no log noise
    else:
        changes.append(nav_result)

    # 2. Footer column
    footer_result = _apply_footer(soup, ftp_path)
    if footer_result is None:
        print(f"    [WARN] No footer-links div found — skipping footer change")
    elif footer_result == "skip":
        pass  # already present, no log noise
    else:
        changes.append(footer_result)

    # 3. OG/SEO meta tags (always upserted)
    page_meta = _meta_for_page(ftp_path, soup)
    n_meta = _apply_meta_tags(soup, page_meta)
    changes.append(f"upserted {n_meta} OG/SEO meta tags")

    new_html = soup.encode(formatter="minimal").decode("utf-8")
    return new_html, changes


# ── FTP helpers ──────────────────────────────────────────────

def _connect() -> ftplib.FTP:
    """Open and return an authenticated FTP connection."""
    ftp = ftplib.FTP()
    ftp.connect(HOST, 21, timeout=30)
    ftp.login(USER, PASSWORD)
    ftp.set_pasv(True)
    return ftp


def _list_html(ftp: ftplib.FTP, remote_dir: str) -> list[str]:
    """Recursively list all .html files under remote_dir (empty string = FTP root)."""
    results: list[str] = []
    try:
        entries = list(ftp.mlsd(remote_dir if remote_dir else None))
    except ftplib.error_perm:
        return results
    for name, facts in entries:
        if name in (".", ".."):
            continue
        path = f"{remote_dir}/{name}" if remote_dir else name
        if facts.get("type") == "dir":
            results.extend(_list_html(ftp, path))
        elif name.lower().endswith(".html") or name.lower().endswith(".htm"):
            results.append(path)
    return results


def _download(ftp: ftplib.FTP, remote_path: str) -> str:
    """Download a remote file and return its contents as a string."""
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {remote_path}", buf.write)
    return buf.getvalue().decode("utf-8", errors="replace")


def _upload_str(ftp: ftplib.FTP, content: str, remote_path: str) -> None:
    """Upload a string as UTF-8 to a remote FTP path."""
    buf = io.BytesIO(content.encode("utf-8"))
    ftp.storbinary(f"STOR {remote_path}", buf)


# ── Main ─────────────────────────────────────────────────────

def main(upload: bool = False) -> None:
    """Download all HTML, apply three changes, and optionally upload."""
    mode_label = "LIVE UPLOAD" if upload else "DRY RUN"
    print(f"Connecting to {HOST}:21  [{mode_label}] ...")
    ftp = _connect()
    print("Connected.  Scanning for HTML files ...\n")

    html_files = _list_html(ftp, REMOTE_ROOT)
    if not html_files:
        print("No HTML files found.")
        ftp.quit()
        return

    updated = 0
    skipped = 0

    for remote_path in sorted(html_files):
        print(f"  {remote_path}")
        try:
            html = _download(ftp, remote_path)
            new_html, changes = _update_html(html, remote_path)

            for c in changes:
                print(f"    -> {c}")

            if upload:
                _upload_str(ftp, new_html, remote_path)
                print("    [uploaded]")
            else:
                print("    [DRY RUN -- not uploaded]")
            updated += 1

        except Exception as exc:
            print(f"    ERROR: {exc}")
            skipped += 1

    ftp.quit()
    action = "uploaded" if upload else "would update"
    print(f"\nDone.  {updated} file(s) {action}, {skipped} error(s).")


if __name__ == "__main__":
    _upload_flag = "--upload" in sys.argv
    main(upload=_upload_flag)
