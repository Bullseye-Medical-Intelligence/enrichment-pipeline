"""
download_site.py — download the full bullseyemedical.ai static site from Hostinger
into site/ so the site can be version-controlled and edited in the repo.

Run once to bootstrap the local mirror, then commit site/ to git.
After that, edit files in site/ and run upload_site.py to push changes.

Usage:
    python download_site.py

Reads credentials from pipeline-api/.env or environment variables:
    HOSTINGER_SFTP_HOST, HOSTINGER_SFTP_USER, HOSTINGER_SFTP_PASSWORD

The Hostinger FTP account is chrooted to public_html, so FTP root == web root.
Files are saved as site/<remote-path> (e.g. FTP index.html → site/index.html).
"""

from __future__ import annotations

import ftplib
import io
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

# File extensions to download (covers all static site assets)
_KEEP_EXTENSIONS = {
    ".html", ".htm", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".ico", ".txt", ".xml", ".pdf", ".woff", ".woff2",
    ".ttf", ".eot", ".json", ".map",
}

# File names without extensions to include
_KEEP_NAMES = {".htaccess", "robots.txt"}

# Remote directory names to skip entirely
_SKIP_DIRS = {"cgi-bin", "tmp", "logs", "mail", ".well-known", "cpanel", ".cpanel"}

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


def _list_all(ftp: ftplib.FTP, remote_dir: str) -> list[str]:
    """Recursively list all downloadable files under remote_dir."""
    results = []
    try:
        entries = list(ftp.mlsd(remote_dir if remote_dir else None))
    except ftplib.error_perm:
        return results
    for name, facts in entries:
        if name in (".", ".."):
            continue
        path = f"{remote_dir}/{name}" if remote_dir else name
        ftype = facts.get("type", "")
        if ftype == "dir":
            if name.lower() in _SKIP_DIRS:
                continue
            results.extend(_list_all(ftp, path))
        else:
            ext = Path(name).suffix.lower()
            if ext in _KEEP_EXTENSIONS or name in _KEEP_NAMES:
                results.append(path)
    return results


def _download_bytes(ftp: ftplib.FTP, remote_path: str) -> bytes:
    """Download one remote file and return its raw bytes."""
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {remote_path}", buf.write)
    return buf.getvalue()


def main() -> None:
    SITE_DIR.mkdir(exist_ok=True)

    print(f"Connecting to {HOST}:21 as {USER} …")
    ftp = _connect()
    print(f"Connected. ({ftp.getwelcome()[:60]})")
    print("\nFTP root listing:")
    for entry in sorted(ftp.nlst()):
        print(f"  {entry}")
    print("\nScanning for site files …")

    files = _list_all(ftp, "")
    print(f"Found {len(files)} files.\n")

    downloaded = 0
    errors = 0

    for remote_path in sorted(files):
        local_path = SITE_DIR / remote_path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = _download_bytes(ftp, remote_path)
            local_path.write_bytes(data)
            size = len(data)
            print(f"  ✓  {remote_path}  ({size:,} bytes)")
            downloaded += 1
        except Exception as exc:
            print(f"  ✗  {remote_path}  ERROR: {exc}")
            errors += 1

    ftp.quit()
    print(f"\nDone. {downloaded} downloaded, {errors} errors.")
    print(f"Files saved to: {SITE_DIR}/")
    if downloaded:
        print("\nNext steps:")
        print("  git add site/")
        print("  git commit -m 'chore: bootstrap site mirror from Hostinger'")
        print("  git push")


if __name__ == "__main__":
    main()
