"""
brief_publisher.py
Upload a generated HTML brief to Hostinger and return a public URL.
Also manages the per-run published_briefs.json record.

Tries SFTP (paramiko) first; if paramiko is not installed or the SFTP
connection is refused, falls back to plain FTP (ftplib, stdlib).

Public API:
  publish_brief(html_bytes, client_slug, brief_type, existing_storage_path=None) -> dict
  client_slug_from_name(client_name) -> str
  get_published_briefs(run_directory) -> dict
  save_published_brief(run_directory, brief_type, result) -> None
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import config

logger = logging.getLogger(__name__)

_BRIEFS_FILENAME = "published_briefs.json"


def publish_brief(
    html_bytes: bytes,
    client_slug: str,
    brief_type: str,
    existing_storage_path: str | None = None,
) -> dict:
    """Upload an HTML brief to Hostinger and return its public URL.

    When existing_storage_path is provided (a republish), the file is overwritten
    in place so the URL stays the same for anyone who already received it.

    Returns:
        {"public_url": str, "storage_path": str, "filename": str, "published_at": str}

    Raises:
        RuntimeError: if the upload fails or publishing is not configured.
    """
    if not config.HOSTINGER_SFTP_HOST:
        raise RuntimeError(
            "Brief publishing is not configured. Set HOSTINGER_SFTP_HOST in .env."
        )

    if existing_storage_path:
        remote_path = existing_storage_path
        root = config.HOSTINGER_BRIEFS_REMOTE_ROOT.rstrip("/")
        if existing_storage_path.startswith(root + "/"):
            relative_path = existing_storage_path[len(root) + 1:]
        else:
            relative_path = existing_storage_path.lstrip("/")
        filename = PurePosixPath(relative_path).name
    else:
        token = secrets.token_hex(4)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"{brief_type}-{token}.html"
        relative_path = f"{client_slug}/{today}/{filename}"
        remote_path = str(
            PurePosixPath(config.HOSTINGER_BRIEFS_REMOTE_ROOT.rstrip("/")) / relative_path
        )

    public_url = f"{config.BRIEFS_PUBLIC_BASE_URL.rstrip('/')}/{relative_path}"

    _upload(html_bytes, remote_path)

    published_at = datetime.now(timezone.utc).isoformat()
    logger.info("Published %s brief for %s: %s", brief_type, client_slug, public_url)
    return {
        "public_url": public_url,
        "storage_path": remote_path,
        "filename": filename,
        "published_at": published_at,
    }


def client_slug_from_name(client_name: str) -> str:
    """Convert a client name to a URL-safe slug, e.g. 'Right at Home' -> 'right-at-home'."""
    s = client_name.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s-]+", "-", s)
    return s.strip("-")[:40] or "client"


def get_published_briefs(run_directory: Path) -> dict:
    """Load published_briefs.json from run_directory; return {} if absent or corrupt."""
    path = run_directory / _BRIEFS_FILENAME
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_published_brief(run_directory: Path, brief_type: str, result: dict) -> None:
    """Atomically update published_briefs.json with a new or updated entry."""
    path = run_directory / _BRIEFS_FILENAME
    briefs = get_published_briefs(run_directory)
    briefs[brief_type] = result
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(briefs, f, indent=2)
    os.replace(tmp, path)


def _upload(data: bytes, remote_path: str) -> None:
    """Upload data to remote_path, trying SFTP first and falling back to FTP."""
    sftp_err = None
    try:
        _sftp_upload(data, remote_path)
        return
    except RuntimeError as exc:
        # paramiko not installed or SFTP connection refused — fall through to FTP
        sftp_err = exc

    logger.warning("SFTP unavailable (%s); retrying via FTP.", sftp_err)
    try:
        _ftp_upload(data, remote_path)
    except Exception as exc:
        raise RuntimeError(
            f"Both SFTP and FTP upload failed. "
            f"SFTP error: {sftp_err}. FTP error: {exc}"
        ) from exc


def _sftp_upload(data: bytes, remote_path: str) -> None:
    """Upload via SFTP (paramiko). Raises RuntimeError on any failure."""
    try:
        import paramiko
    except ImportError as exc:
        raise RuntimeError("paramiko not installed; falling back to FTP") from exc

    transport = paramiko.Transport((config.HOSTINGER_SFTP_HOST, config.HOSTINGER_SFTP_PORT))
    try:
        transport.connect(
            username=config.HOSTINGER_SFTP_USER,
            password=config.HOSTINGER_SFTP_PASSWORD,
        )
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            _sftp_makedirs(sftp, str(PurePosixPath(remote_path).parent))
            with sftp.open(remote_path, "wb") as fh:
                fh.write(data)
        finally:
            sftp.close()
    except paramiko.AuthenticationException as exc:
        raise RuntimeError(f"SFTP authentication failed: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"SFTP upload failed: {exc}") from exc
    finally:
        transport.close()


def _sftp_makedirs(sftp, remote_dir: str) -> None:
    """Recursively create remote_dir and all missing parents via SFTP."""
    parts = PurePosixPath(remote_dir).parts
    current = ""
    for part in parts:
        current = str(PurePosixPath(current) / part) if current else part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def _ftp_upload(data: bytes, remote_path: str) -> None:
    """Upload via plain FTP (stdlib ftplib). Creates parent directories as needed.

    Hostinger FTP is chrooted to the account home directory, so absolute paths
    like /home/u353003312/domains/... must be relativised against the FTP root
    before use — otherwise the path is doubled up and the file lands in the wrong
    place.
    """
    import ftplib
    import io

    with ftplib.FTP() as ftp:
        ftp.connect(config.HOSTINGER_SFTP_HOST, config.HOSTINGER_FTP_PORT)
        ftp.login(config.HOSTINGER_SFTP_USER, config.HOSTINGER_SFTP_PASSWORD)

        # If the configured root is a relative path (no leading /), use it directly.
        # Absolute paths need chroot detection.
        rel_path = _ftp_rel_path(remote_path, ftp)
        logger.info("FTP upload: configured=%r rel=%r", remote_path, rel_path)

        _ftp_makedirs(ftp, str(PurePosixPath(rel_path).parent))
        # cwd is now at the target directory — use filename only, not full path
        ftp.storbinary(f"STOR {PurePosixPath(rel_path).name}", io.BytesIO(data))


def _ftp_rel_path(remote_path: str, ftp) -> str:
    """Return remote_path relative to the FTP chroot root.

    If the path is already relative (no leading /), return it as-is —
    this is the recommended configuration when the FTP account is chrooted
    to a specific directory (e.g. public_html). Absolute paths are handled
    by probing the FTP root listing to detect the chroot offset.
    """
    if not remote_path.startswith("/"):
        return remote_path

    parts = list(PurePosixPath(remote_path).parts)
    if parts and parts[0] == "/":
        parts = parts[1:]

    try:
        root_entries = set(ftp.nlst())
    except Exception:
        root_entries = set()

    while len(parts) > 1 and parts[0] not in root_entries:
        parts = parts[1:]

    return str(PurePosixPath(*parts)) if parts else remote_path.lstrip("/")


def _ftp_makedirs(ftp, remote_dir: str) -> None:
    """Recursively create remote_dir (relative) and all missing parents via FTP."""
    import ftplib

    for part in PurePosixPath(remote_dir).parts:
        try:
            ftp.cwd(part)
            logger.info("FTP cwd(%r) ok — now at %r", part, ftp.pwd())
        except ftplib.error_perm as cwd_err:
            logger.info("FTP cwd(%r) failed (%s), trying mkd", part, cwd_err)
            try:
                ftp.mkd(part)
                ftp.cwd(part)
            except ftplib.error_perm as mkd_err:
                raise RuntimeError(
                    f"FTP makedirs failed at {part!r}: cwd={cwd_err!s} mkd={mkd_err!s}"
                ) from mkd_err
