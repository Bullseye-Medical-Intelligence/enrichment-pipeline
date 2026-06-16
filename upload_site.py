"""
upload_site.py — upload updated static site files to Hostinger via SFTP.

Usage:
    python upload_site.py [file1.html file2.html ...]

If no files are given it uploads everything in the `site/` directory.
Reads HOSTINGER_SFTP_HOST / USER / PASSWORD / PORT from .env (pipeline-api/.env).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Load .env from pipeline-api/ if running from repo root
_env_path = Path(__file__).parent / "pipeline-api" / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

HOST = os.environ.get("HOSTINGER_SFTP_HOST", "")
PORT = int(os.environ.get("HOSTINGER_SFTP_PORT", "22"))
USER = os.environ.get("HOSTINGER_SFTP_USER", "")
PASSWORD = os.environ.get("HOSTINGER_SFTP_PASSWORD", "")
REMOTE_ROOT = "/public_html"   # adjust if your site root differs

if not all([HOST, USER, PASSWORD]):
    sys.exit("Missing HOSTINGER_SFTP_HOST / USER / PASSWORD in pipeline-api/.env")

try:
    import paramiko
except ImportError:
    sys.exit("Run: pip install paramiko")


def _connect() -> tuple:
    """Return (transport, sftp) connected to Hostinger."""
    transport = paramiko.Transport((HOST, PORT))
    transport.connect(username=USER, password=PASSWORD)
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp


def _upload(sftp, local_path: Path, remote_path: str) -> None:
    """Upload one file, creating remote directories as needed."""
    remote_dir = str(Path(remote_path).parent)
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        sftp.mkdir(remote_dir)
    sftp.put(str(local_path), remote_path)
    print(f"  ✓  {local_path.name}  →  {remote_path}")


def main() -> None:
    files = [Path(f) for f in sys.argv[1:]] if sys.argv[1:] else list(Path("site").glob("**/*.html"))

    if not files:
        sys.exit("No files to upload. Pass filenames or put HTML files in a site/ directory.")

    print(f"Connecting to {HOST}:{PORT} as {USER} …")
    transport, sftp = _connect()

    try:
        for local in files:
            if not local.exists():
                print(f"  ✗  {local} not found, skipping")
                continue
            # Derive remote path: site/foo/bar.html → /public_html/foo/bar.html
            # or flat file → /public_html/bar.html
            parts = local.parts
            if "site" in parts:
                rel = Path(*parts[parts.index("site") + 1:])
            else:
                rel = local.name
            remote = f"{REMOTE_ROOT}/{rel}"
            _upload(sftp, local, remote)
    finally:
        sftp.close()
        transport.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
