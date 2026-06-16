"""
inspect_site.py — read-only: lists /assets/ on Hostinger and dumps
dark-section hints from index.html and targeting-tax.html.

Run from the repo root:
    python inspect_site.py

Reads credentials from pipeline-api/.env
"""
import ftplib
import io
import os
from pathlib import Path

# load .env
_env = Path(__file__).parent / "pipeline-api" / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, _, v = _line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

HOST = os.environ.get("HOSTINGER_SFTP_HOST", "")
USER = os.environ.get("HOSTINGER_SFTP_USER", "")
PASSWORD = os.environ.get("HOSTINGER_SFTP_PASSWORD", "")
if not all([HOST, USER, PASSWORD]):
    raise SystemExit("Missing HOSTINGER_SFTP_HOST / USER / PASSWORD in pipeline-api/.env")

ftp = ftplib.FTP()
ftp.connect(HOST, 21, timeout=30)
ftp.login(USER, PASSWORD)
ftp.set_pasv(True)
print(f"Connected to {HOST}\n")

# ── /assets/ inventory ──────────────────────────────────────────
print("=== /assets/ contents ===")
try:
    for name, facts in ftp.mlsd("assets"):
        if name not in (".", ".."):
            print(f"  {name}  ({facts.get('size', '?')} bytes)")
except Exception as e:
    print(f"  ERROR listing /assets/: {e}")

# ── SVG file contents ───────────────────────────────────────────
for svg_path in [
    "assets/bullseye-mark.svg",
    "assets/bullseye-mark-ink.svg",
    "assets/bullseye-mark-paper.svg",
]:
    buf = io.BytesIO()
    try:
        ftp.retrbinary(f"RETR {svg_path}", buf.write)
        print(f"\n=== {svg_path} ===")
        print(buf.getvalue().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"\n=== {svg_path} NOT FOUND: {e} ===")

# ── Dark-section convention hunt ────────────────────────────────
KEYWORDS = ["#0a0a0a", "dark", "bg-", "background", "section", "footer", "class="]

for page in ["index.html", "targeting-tax.html"]:
    buf = io.BytesIO()
    try:
        ftp.retrbinary(f"RETR {page}", buf.write)
        lines = buf.getvalue().decode("utf-8", errors="replace").splitlines()
        print(f"\n\n=== {page} — dark/section/class hints (first 400 lines) ===")
        for i, line in enumerate(lines[:400], 1):
            lo = line.lower()
            if any(k in lo for k in KEYWORDS):
                print(f"  {i:4d}: {line.rstrip()}")
    except Exception as e:
        print(f"\n=== {page} ERROR: {e} ===")

ftp.quit()
print("\nDone.")
