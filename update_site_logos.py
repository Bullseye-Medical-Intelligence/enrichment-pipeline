"""
update_site_logos.py — download every HTML page from Hostinger, update logo
references to the new bullseye ring mark, and re-upload.

Patterns replaced:
  1. Inline fan-geometry SVG (x1="18" y1="24.5" probe lines) → ring SVG inline
  2. Old <img> logo references (logo.svg, logo-light.svg, etc.) → /assets/bullseye-mark-ink.svg
  3. Missing or wrong favicon → /bullseye-favicon.svg
  4. Missing brand-name text that should read "Bullseye Medical Intelligence"

Run from the repo root on your local machine:
    python update_site_logos.py

Reads credentials from pipeline-api/.env.
"""

from __future__ import annotations

import ftplib
import io
import os
import re
import sys
from pathlib import Path

# ── Load .env ────────────────────────────────────────────────
_env_path = Path(__file__).parent / "pipeline-api" / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

HOST = os.environ.get("HOSTINGER_SFTP_HOST", "")
USER = os.environ.get("HOSTINGER_SFTP_USER", "")
PASSWORD = os.environ.get("HOSTINGER_SFTP_PASSWORD", "")
REMOTE_ROOT = ""  # FTP home is already the web root on Hostinger

if not all([HOST, USER, PASSWORD]):
    sys.exit("Missing HOSTINGER_SFTP_HOST / USER / PASSWORD in pipeline-api/.env")

# ── New bullseye ring SVG (inline, light-on-dark) ────────────
NEW_MARK_SVG = (
    '<svg class="logomark" width="30" height="30" viewBox="0 0 36 36" fill="none" '
    'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<circle cx="18" cy="18" r="15.5" stroke="currentColor" stroke-width="1.8"/>'
    '<circle cx="18" cy="18" r="8.5" stroke="currentColor" stroke-width="1.8"/>'
    '<circle cx="18" cy="18" r="3" fill="currentColor"/>'
    '<line x1="18" y1="1.5" x2="18" y2="8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
    '<line x1="18" y1="27.5" x2="18" y2="34.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
    '<line x1="1.5" y1="18" x2="8.5" y2="18" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
    '<line x1="27.5" y1="18" x2="34.5" y2="18" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
    '</svg>'
)

# ── Regex patterns for old logo marks ────────────────────────

# Old fan-geometry SVG block (multi-line): starts with <svg ... and contains the fan probe line
_FAN_SVG_RE = re.compile(
    r'<svg\b[^>]*>(?:(?!</svg>).)*x1="18"\s+y1="24\.5"(?:(?!</svg>).)*</svg>',
    re.DOTALL | re.IGNORECASE,
)

# Old <img> logo references
_IMG_LOGO_RE = re.compile(
    r'<img\b[^>]*src=["\'](?:[^"\']*/)?(logo[\w\-]*\.svg|bullseye[\w\-]*logo[\w\-]*\.svg)["\'][^>]*>',
    re.IGNORECASE,
)

# Old favicon link (wrong path or old svg name)
_FAVICON_OLD_RE = re.compile(
    r'<link\s[^>]*rel=["\']icon["\'][^>]*/?>',
    re.IGNORECASE,
)

FAVICON_NEW = '<link rel="icon" href="/bullseye-favicon.svg" type="image/svg+xml">'


def _update_html(html: str, filename: str) -> tuple[str, list[str]]:
    """Apply logo replacements to one HTML string. Returns (new_html, list_of_changes)."""
    changes = []
    original = html

    # 1. Replace inline fan-geometry SVG with ring SVG
    def _replace_fan(m):
        changes.append("replaced inline fan-geometry SVG with ring mark")
        return NEW_MARK_SVG
    html = _FAN_SVG_RE.sub(_replace_fan, html)

    # 2. Replace old <img> logo references pointing at logo*.svg files
    def _replace_img_logo(m):
        src_match = re.search(r'src=["\']([^"\']+)["\']', m.group(0), re.IGNORECASE)
        old_src = src_match.group(1) if src_match else "?"
        changes.append(f"replaced <img src=\"{old_src}\"> with /assets/bullseye-mark-ink.svg")
        # Preserve width/height/alt/style attrs if present
        attrs = ""
        for attr in ("width", "height", "alt", "style", "class"):
            am = re.search(rf'{attr}=["\']([^"\']*)["\']', m.group(0), re.IGNORECASE)
            if am:
                attrs += f' {attr}="{am.group(1)}"'
        return f'<img src="/assets/bullseye-mark-ink.svg"{attrs}>'
    html = _IMG_LOGO_RE.sub(_replace_img_logo, html)

    # 3. Update favicon
    if _FAVICON_OLD_RE.search(html):
        new_fav, n = _FAVICON_OLD_RE.subn(FAVICON_NEW, html)
        if n:
            html = new_fav
            changes.append("updated favicon link")
    elif "</head>" in html.lower():
        # Insert favicon before </head> if missing entirely
        html = re.sub(r'(</head>)', FAVICON_NEW + r'\n\1', html, count=1, flags=re.IGNORECASE)
        changes.append("inserted missing favicon link")

    if html == original:
        changes.append("(no changes needed)")

    return html, changes


# ── FTP helpers ──────────────────────────────────────────────

def _connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(HOST, 21, timeout=30)
    ftp.login(USER, PASSWORD)
    ftp.set_pasv(True)
    return ftp


def _list_html(ftp: ftplib.FTP, remote_dir: str) -> list[str]:
    """Recursively list all .html files under remote_dir (empty string = FTP root)."""
    results = []
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
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {remote_path}", buf.write)
    return buf.getvalue().decode("utf-8", errors="replace")


def _upload_str(ftp: ftplib.FTP, content: str, remote_path: str) -> None:
    buf = io.BytesIO(content.encode("utf-8"))
    ftp.storbinary(f"STOR {remote_path}", buf)


def _ensure_dir(ftp: ftplib.FTP, remote_dir: str) -> None:
    parts = remote_dir.strip("/").split("/")
    current = ""
    for part in parts:
        current = f"{current}/{part}" if current else part
        try:
            ftp.mkd(current)
        except ftplib.error_perm:
            pass


# ── Main ─────────────────────────────────────────────────────

def main() -> None:
    print(f"Connecting to {HOST}:21 …")
    ftp = _connect()
    print(f"Connected. Scanning {REMOTE_ROOT}/ for HTML files …\n")

    # Print the top-level directory so we can verify the structure
    print("  Root contents:", ftp.nlst())
    html_files = _list_html(ftp, REMOTE_ROOT)
    if not html_files:
        print("No HTML files found under", REMOTE_ROOT)
        ftp.quit()
        return

    updated = 0
    skipped = 0

    for remote_path in sorted(html_files):
        print(f"  {remote_path}")
        try:
            html = _download(ftp, remote_path)
            new_html, changes = _update_html(html, Path(remote_path).name)
            for c in changes:
                print(f"    → {c}")
            if "(no changes needed)" not in changes[0]:
                _upload_str(ftp, new_html, remote_path)
                updated += 1
            else:
                skipped += 1
        except Exception as exc:
            print(f"    ✗ ERROR: {exc}")

    ftp.quit()
    print(f"\nDone. {updated} file(s) updated, {skipped} already up to date.")


if __name__ == "__main__":
    main()
