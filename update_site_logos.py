"""
update_site_logos.py — download every HTML page from Hostinger, update logo
references to the new bullseye ring mark, and re-upload.

Patterns replaced:
  1. Fan-geometry SVG variant A (36×36, x1="18" y1="24.5" probe lines)
  2. Fan-geometry SVG variant B (28×28, convergence at cx=14 cy=17) — used on homepage
  3. Old <img> logo references (logo.svg, logo-light.svg, etc.)
  4. Missing or wrong favicon → /bullseye-favicon.svg

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

# ── Regex patterns for old logo marks ────────────────────────

# Fan variant A: 36×36, probe lines converge at y=24.5
_FAN_SVG_A_RE = re.compile(
    r'<svg\b[^>]*>(?:(?!</svg>).)*y1="24\.5"(?:(?!</svg>).)*</svg>',
    re.DOTALL | re.IGNORECASE,
)

# Fan variant B: 28×28, probe lines converge at cx=14 cy=17 (homepage / nav / footer)
_FAN_SVG_B_RE = re.compile(
    r'<svg\b[^>]*viewBox="0 0 28 28"[^>]*>(?:(?!</svg>).)*cy="17"(?:(?!</svg>).)*</svg>',
    re.DOTALL | re.IGNORECASE,
)

# Fan variant C: 30×30, probe lines converge at cx=15 cy=15 (intelligence pages)
_FAN_SVG_C_RE = re.compile(
    r'<svg\b[^>]*viewBox="0 0 30 30"[^>]*>(?:(?!</svg>).)*cy="15"(?:(?!</svg>).)*</svg>',
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


def _svg_is_white(svg_text: str) -> bool:
    """Return True if the SVG uses white strokes (dark-background context)."""
    return bool(re.search(r'stroke="#fff(?:fff)?"|stroke="white"', svg_text, re.IGNORECASE))


def _img_for_svg(svg_text: str, default_size: str = "28") -> str:
    """Build an <img> replacement for an old fan SVG, preserving size and adding correct filter."""
    w_m = re.search(r'\bwidth="(\d+)"', svg_text)
    h_m = re.search(r'\bheight="(\d+)"', svg_text)
    w = w_m.group(1) if w_m else default_size
    h = h_m.group(1) if h_m else default_size
    style_m = re.search(r'\bstyle="([^"]*)"', svg_text)
    extra_style = style_m.group(1).rstrip(";") + ";" if style_m else ""
    if _svg_is_white(svg_text):
        extra_style += "filter:brightness(0) invert(1);"
    style_attr = f' style="display:block;{extra_style}"' if extra_style else ' style="display:block;"'
    return f'<img src="/assets/bullseye-mark-ink.svg" width="{w}" height="{h}" alt=""{style_attr}>'


def _update_html(html: str, filename: str) -> tuple[str, list[str]]:
    """Apply logo replacements to one HTML string. Returns (new_html, list_of_changes)."""
    changes = []
    original = html

    # 1a. Fan variant A: 36×36 (y1="24.5" probe lines)
    def _replace_fan_a(m):
        changes.append("replaced 36×36 fan SVG with ring mark img")
        return _img_for_svg(m.group(0), "30")
    html = _FAN_SVG_A_RE.sub(_replace_fan_a, html)

    # 1b. Fan variant B: 28×28 (convergence at cy=17, used on homepage nav/footer/eyebrow)
    def _replace_fan_b(m):
        changes.append("replaced 28×28 fan SVG with ring mark img")
        return _img_for_svg(m.group(0), "28")
    html = _FAN_SVG_B_RE.sub(_replace_fan_b, html)

    # 1c. Fan variant C: 30×30 (convergence at cy=15, used on intelligence pages)
    def _replace_fan_c(m):
        changes.append("replaced 30×30 fan SVG with ring mark img")
        return _img_for_svg(m.group(0), "28")
    html = _FAN_SVG_C_RE.sub(_replace_fan_c, html)

    # 2. Replace old <img> logo references pointing at logo*.svg files
    def _replace_img_logo(m):
        src_match = re.search(r'src=["\']([^"\']+)["\']', m.group(0), re.IGNORECASE)
        old_src = src_match.group(1) if src_match else "?"
        changes.append(f"replaced <img src=\"{old_src}\"> with /assets/bullseye-mark-ink.svg")
        attrs = ""
        for attr in ("width", "height", "alt", "style", "class"):
            am = re.search(rf'{attr}=["\']([^"\']*)["\']', m.group(0), re.IGNORECASE)
            if am:
                attrs += f' {attr}="{am.group(1)}"'
        return f'<img src="/assets/bullseye-mark-ink.svg"{attrs}>'
    html = _IMG_LOGO_RE.sub(_replace_img_logo, html)

    # 2b. Fix CSS specificity bug on intelligence pages: .light .prose p overrides
    #     .answer p causing white-on-black boxes to show black text.
    old_answer_p = ".answer p{font-size:17px"
    new_answer_p = ".answer p,.light .prose .answer p{font-size:17px"
    if old_answer_p in html:
        html = html.replace(old_answer_p, new_answer_p, 1)
        changes.append("fixed .answer p CSS specificity bug")

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
