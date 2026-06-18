"""
upload_site.py — upload static site files to Hostinger via FTP.

The Hostinger FTP account is chrooted to public_html, so FTP root == web root.
Files in site/ map directly: site/index.html → FTP root index.html.

Usage:
    python upload_site.py                          # upload ALL files in site/
    python upload_site.py site/index.html          # upload specific file(s)

Reads credentials from pipeline-api/.env or environment variables:
    HOSTINGER_SFTP_HOST, HOSTINGER_SFTP_USER, HOSTINGER_SFTP_PASSWORD
"""

from __future__ import annotations

import ftplib
import os
import sys
from pathlib import Path

# ── Load .env ─────────────────────────────────────────────────
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
SITE_DIR = Path(__file__).parent / "site"

# FTP home is already the web root (Hostinger chroots to public_html)
_REMOTE_ROOT = ""

if not all([HOST, USER, PASSWORD]):
    sys.exit(
        "Missing Hostinger credentials.\n"
        "Set HOSTINGER_SFTP_HOST, HOSTINGER_SFTP_USER, HOSTINGER_SFTP_PASSWORD\n"
        "in pipeline-api/.env or as environment variables."
    )


def _connect() -> ftplib.FTP:
    """Return an authenticated FTP connection to Hostinger."""
    ftp = ftplib.FTP()
    ftp.connect(HOST, 21, timeout=30)
    ftp.login(USER, PASSWORD)
    ftp.set_pasv(True)
    return ftp


def _ensure_dir(ftp: ftplib.FTP, remote_dir: str) -> None:
    """Create remote directory path if it does not already exist."""
    if not remote_dir or remote_dir == ".":
        return
    parts = remote_dir.strip("/").split("/")
    current = ""
    for part in parts:
        current = f"{current}/{part}" if current else part
        try:
            ftp.mkd(current)
        except ftplib.error_perm:
            pass  # already exists


def _remote_path(local: Path) -> str:
    """Derive the FTP destination path from a local site/ path."""
    try:
        rel = local.relative_to(SITE_DIR)
    except ValueError:
        rel = Path(local.name)
    return str(rel).replace("\\", "/")


def _upload(ftp: ftplib.FTP, local: Path) -> None:
    """Upload one file, creating remote directories as needed."""
    remote = _remote_path(local)
    remote_dir = str(Path(remote).parent).replace("\\", "/")
    _ensure_dir(ftp, remote_dir)
    with open(local, "rb") as f:
        ftp.storbinary(f"STOR {remote}", f)
    size = local.stat().st_size
    print(f"  ✓  {remote}  ({size:,} bytes)")


def _all_site_files() -> list[Path]:
    """Return all files under site/ in sorted order."""
    if not SITE_DIR.exists():
        sys.exit(
            f"site/ directory not found at {SITE_DIR}.\n"
            "Run download_site.py first to pull the current site from Hostinger."
        )
    return sorted(p for p in SITE_DIR.rglob("*") if p.is_file())


def main() -> None:
    if sys.argv[1:]:
        files = [Path(f) for f in sys.argv[1:]]
    else:
        files = _all_site_files()
        print(f"Uploading all {len(files)} files from site/ …\n")

    if not files:
        sys.exit("No files to upload.")

    print(f"Connecting to {HOST}:21 as {USER} …")
    ftp = _connect()
    print(f"Connected. ({ftp.getwelcome()[:60]})\n")

    uploaded = 0
    errors = 0
    try:
        for local in files:
            if not local.exists():
                print(f"  ✗  {local}  (not found, skipping)")
                errors += 1
                continue
            try:
                _upload(ftp, local)
                uploaded += 1
            except Exception as exc:
                print(f"  ✗  {local}  ERROR: {exc}")
                errors += 1
    finally:
        ftp.quit()

    print(f"\nDone. {uploaded} uploaded, {errors} errors.")


if __name__ == "__main__":
    main()
