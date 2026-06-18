"""
evidence_writer.py
Evidence Vault: persist the per-page text the crawler actually saw, at crawl
time, so every signal claim stays verifiable after the live site changes.

Layout inside a run's output directory:

  evidence/<record_id>/
    index.json    [{url, file, fetched_at, sha256, chars, provenance}]
    page-01.txt   extracted text of one crawled page
    page-02.txt

Capture is per-record and overwrite-on-recapture: a re-crawl replaces the
record's evidence directory so the snapshot always matches the record the
operator sees. The index sha256 is the fingerprint of each page's text —
tamper-evident provenance for "we saw this, on this page, at this time".
"""

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from output.atomic_write import atomic_write

EVIDENCE_DIRNAME = "evidence"
INDEX_FILENAME = "index.json"
PAGE_FILENAME_TEMPLATE = "page-{:02d}.txt"

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")

# Mirror of extraction.web_extractor.MAX_COMBINED_CHARS — kept local to avoid an
# output -> extraction import. Caps reconstructed context so a post-run pass sees
# the same budget a live Step 4 saw.
MAX_COMBINED_CONTEXT_CHARS = 25000
_CONTEXT_BLOCK_SEPARATOR = "\n\n---\n\n"


def sanitize_record_id(record_id: str) -> str:
    """Reduce a record id to filesystem-safe characters (path-traversal guard)."""
    return _SAFE_ID_RE.sub("_", str(record_id or "").strip())[:80]


def evidence_dir_for_record(output_dir: Path, record_id: str) -> Path:
    """Return the evidence directory path for one record (not created)."""
    safe_id = sanitize_record_id(record_id)
    if not safe_id:
        raise ValueError("record_id is empty after sanitization")
    return Path(output_dir) / EVIDENCE_DIRNAME / safe_id


def write_record_evidence(
    output_dir: Path,
    record_id: str,
    pages: list[dict],
    provenance: str = "crawl",
) -> int:
    """Write a record's page snapshots + index; returns the page count written.

    `pages` is [{"url": ..., "text": ...}] from the extractor. An existing
    evidence directory for the record is replaced (newest capture wins).
    Pages with empty text are skipped. Returns 0 (and writes nothing) when no
    page has text.
    """
    usable = [p for p in pages if (p.get("text") or "").strip()]
    if not usable:
        return 0

    record_dir = evidence_dir_for_record(output_dir, record_id)
    if record_dir.exists():
        shutil.rmtree(record_dir)
    record_dir.mkdir(parents=True)

    fetched_at = datetime.now(timezone.utc).isoformat()
    index = []
    for n, page in enumerate(usable, start=1):
        text = page["text"]
        filename = PAGE_FILENAME_TEMPLATE.format(n)
        atomic_write(record_dir / filename, lambda f, _t=text: f.write(_t))
        index.append({
            "url": page.get("url", ""),
            "file": filename,
            "fetched_at": fetched_at,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "chars": len(text),
            "provenance": provenance,
        })

    atomic_write(
        record_dir / INDEX_FILENAME,
        lambda f: json.dump(index, f, indent=2, ensure_ascii=False),
    )
    return len(index)


def read_record_evidence_index(output_dir: Path, record_id: str) -> list[dict]:
    """Load a record's evidence index; returns [] when absent or malformed."""
    try:
        path = evidence_dir_for_record(output_dir, record_id) / INDEX_FILENAME
    except ValueError:
        return []
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def read_record_context_text(output_dir: Path, record_id: str) -> str:
    """Reconstruct a record's `_context_text` from its Evidence Vault snapshot.

    Internal pipeline fields are stripped before enriched_targets.json is
    written, so post-run passes (verification, re-extraction) cannot read
    `_context_text` back from the output. This rebuilds it from the archived
    page files in the same shape a live crawl produced: each page wrapped as
    "[Source: <url>]\\n<text>" and joined with the block separator, capped at
    the combined-context budget. Returns "" when the record has no snapshot.
    """
    try:
        record_dir = evidence_dir_for_record(output_dir, record_id)
    except ValueError:
        return ""
    index = read_record_evidence_index(output_dir, record_id)
    if not index:
        return ""

    blocks = []
    for entry in index:
        page_path = record_dir / Path(entry.get("file", "")).name
        if not page_path.exists():
            continue
        try:
            text = page_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.strip():
            continue
        blocks.append(f"[Source: {entry.get('url', '')}]\n{text}")

    if not blocks:
        return ""
    combined = _CONTEXT_BLOCK_SEPARATOR.join(blocks)
    if len(combined) > MAX_COMBINED_CONTEXT_CHARS:
        combined = combined[:MAX_COMBINED_CONTEXT_CHARS] + "\n\n[... truncated for token budget ...]"
    return combined
