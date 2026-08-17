"""
audit_anchors.py — Evidence Anchor Audit CLI (report-only).

Mechanically confirms that each requested "yes" signal's evidence_text still
appears verbatim (normalized whitespace + case, same rule as the verification
pass's anchor-check) in the record's archived Evidence Vault pages. Zero LLM,
zero network — pure local string matching, so it is cheap to re-run after any
re-crawl or re-extract.

stdin (the API builds this work-list; tier/override eligibility lives there):
    {"records": [{"record_id": "T-1", "signal_ids": ["S-01", ...]}, ...]}

stdout:
    {"results": [{
        "record_id": "...",
        "status": "all_anchored" | "has_failures" | "not_auditable",
        "signals": [{"signal_id", "signal_label", "classification":
                     "anchored" | "not_anchored" | "no_evidence_text",
                     "page": "page-01.txt", "page_url": "..."}]
    }]}

Matching runs against each archived page file IN FULL (not the rehydrated,
budget-capped combined context), so a quote on a later page can never
false-fail on truncation, and the report can name the page that anchors it.

Report-only: writes nothing, mutates no record, never touches the
`verification` object. Called by the API via subprocess (same pattern as
check_links.py); never run by operators directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from enrichment.verifier import _anchor_check
from output.evidence_writer import evidence_dir_for_record, read_record_evidence_index

CLASS_ANCHORED = "anchored"
CLASS_NOT_ANCHORED = "not_anchored"
CLASS_NO_EVIDENCE = "no_evidence_text"

STATUS_ALL_ANCHORED = "all_anchored"
STATUS_HAS_FAILURES = "has_failures"
STATUS_NOT_AUDITABLE = "not_auditable"


def _load_records_by_id(run_dir: Path) -> dict[str, dict]:
    """Map record id -> record from the run's enriched_targets.json."""
    targets_path = run_dir / "enriched_targets.json"
    if not targets_path.exists():
        sys.exit(f"enriched_targets.json not found in {run_dir}")
    payload = json.loads(targets_path.read_text(encoding="utf-8"))
    records = payload.get("records", payload) if isinstance(payload, dict) else payload
    by_id: dict[str, dict] = {}
    for rec in records:
        rid = str(rec.get("id") or rec.get("record_id") or "").strip()
        if rid:
            by_id[rid] = rec
    return by_id


def _load_vault_pages(run_dir: Path, record_id: str) -> list[dict]:
    """Return [{"file", "url", "text"}] for a record's archived pages, [] when none."""
    index = read_record_evidence_index(run_dir, record_id)
    if not index:
        return []
    try:
        record_dir = evidence_dir_for_record(run_dir, record_id)
    except ValueError:
        return []
    pages = []
    for entry in index:
        # Serve only the basename — same traversal guard as the evidence viewer.
        filename = Path(entry.get("file", "")).name
        page_path = record_dir / filename
        if not page_path.exists():
            continue
        try:
            text = page_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.strip():
            pages.append({"file": filename, "url": entry.get("url", ""), "text": text})
    return pages


def _audit_signal(signal: dict, pages: list[dict]) -> dict:
    """Classify one signal's evidence against the record's vault pages."""
    result = {
        "signal_id": signal.get("signal_id", ""),
        "signal_label": signal.get("signal_label") or signal.get("signal_id", ""),
        "classification": CLASS_NOT_ANCHORED,
        "page": "",
        "page_url": "",
    }
    evidence = (signal.get("evidence_text") or "").strip()
    if not evidence:
        result["classification"] = CLASS_NO_EVIDENCE
        return result
    for page in pages:
        if _anchor_check(evidence, page["text"]):
            result["classification"] = CLASS_ANCHORED
            result["page"] = page["file"]
            result["page_url"] = page["url"]
            return result
    return result


def run_anchor_audit(run_dir: Path, worklist: list[dict]) -> list[dict]:
    """Audit each work-list record's requested signals against its vault snapshot."""
    records_by_id = _load_records_by_id(run_dir)
    results = []
    for item in worklist:
        record_id = str(item.get("record_id") or "").strip()
        signal_ids = set(item.get("signal_ids") or [])
        record = records_by_id.get(record_id)
        if record is None:
            # The run changed between work-list build and audit; report honestly.
            results.append({"record_id": record_id,
                            "status": STATUS_NOT_AUDITABLE, "signals": []})
            continue

        pages = _load_vault_pages(run_dir, record_id)
        if not pages:
            # Pre-vault run or lost snapshot: never a silent pass.
            results.append({"record_id": record_id,
                            "status": STATUS_NOT_AUDITABLE, "signals": []})
            continue

        signal_results = [
            _audit_signal(sig, pages)
            for sig in (record.get("signals") or [])
            if sig.get("signal_id") in signal_ids
        ]
        failed = any(s["classification"] != CLASS_ANCHORED for s in signal_results)
        results.append({
            "record_id": record_id,
            "status": STATUS_HAS_FAILURES if failed else STATUS_ALL_ANCHORED,
            "signals": signal_results,
        })
    return results


def main() -> None:
    """CLI entry point: work-list on stdin, per-record classifications on stdout."""
    parser = argparse.ArgumentParser(
        description="Anchor-audit evidence quotes against the Evidence Vault (report-only)"
    )
    parser.add_argument("--run-dir", required=True, help="Path to the run directory")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        sys.exit(f"Run directory not found: {run_dir}")

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        sys.exit(f"stdin is not valid JSON: {exc}")
    worklist = payload.get("records") or []

    results = run_anchor_audit(run_dir, worklist)
    print(json.dumps({"results": results}))


if __name__ == "__main__":
    main()
