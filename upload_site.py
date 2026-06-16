"""
upload_site.py — upload static site files to Hostinger via FTP.

Usage:
    python upload_site.py [file1.html file2.html ...]

If no files are given it uploads everything in the site/ directory.
Reads credentials from pipeline-api/.env (HOSTINGER_SFTP_HOST / USER / PASSWORD).
Uses plain FTP on port 21 (standard Hostinger shared hosting).
"""

from __future__ import annotations

import ftplib
import os
import sys
from pathlib import Path

# Load .env from pipeline-api/ when running from repo root
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
REMOTE_ROOT = "public_html"  # relative to FTP home on Hostinger

if not all([HOST, USER, PASSWORD]):
    sys.exit("Missing HOSTINGER_SFTP_HOST / USER / PASSWORD in pipeline-api/.env")


def _connect() -> ftplib.FTP:
    """Return an authenticated FTP connection to Hostinger."""
    ftp = ftplib.FTP()
    ftp.connect(HOST, 21, timeout=30)
    ftp.login(USER, PASSWORD)
    ftp.set_pasv(True)
    return ftp


def _ensure_dir(ftp: ftplib.FTP, remote_dir: str) -> None:
    """Create remote directory path if it doesn't already exist."""
    parts = remote_dir.strip("/").split("/")
    current = ""
    for part in parts:
        current = f"{current}/{part}" if current else part
        try:
            ftp.mkd(current)
        except ftplib.error_perm:
            pass  # already exists


def _upload(ftp: ftplib.FTP, local_path: Path, remote_path: str) -> None:
    """Upload one file, creating remote directories as needed."""
    remote_dir = str(Path(remote_path).parent).replace("\\", "/")
    _ensure_dir(ftp, remote_dir)
    with open(local_path, "rb") as f:
        ftp.storbinary(f"STOR {remote_path}", f)
    print(f"  ✓  {local_path.name}  →  {remote_path}")


def main() -> None:
    files = [Path(f) for f in sys.argv[1:]] if sys.argv[1:] else list(Path("site").glob("**/*.html"))

    if not files:
        sys.exit("No files to upload. Pass filenames or put HTML files in a site/ directory.")

    print(f"Connecting to {HOST}:21 as {USER} …")
    ftp = _connect()
    print(f"Connected. Server: {ftp.getwelcome()[:60]}")

    try:
        for local in files:
            if not local.exists():
                print(f"  ✗  {local} not found, skipping")
                continue
            # Derive remote path: site/foo/bar.html → public_html/foo/bar.html
            parts = local.parts
            if "site" in parts:
                rel = "/".join(parts[parts.index("site") + 1:])
            else:
                rel = local.name
            remote = f"{REMOTE_ROOT}/{rel}"
            _upload(ftp, local, remote)
    finally:
        ftp.quit()

    print("\nDone.")


if __name__ == "__main__":
    main()
