"""
writer.py
Write discovery run output files.

Output files (all written to *output_dir*):
  discovery_results.json        — full classified record list
  discovery_results.csv         — flat table for spreadsheet review
  discovery_run_log.json        — run summary / counts
  updated_registry_preview.json — what the registry would look like after
                                   incorporating NEW and CHANGED records;
                                   never overwrites the source registry

The source registry is never modified by this module.
"""

import csv
import io
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from discovery.classifier import (
    NEW, CHANGED, KNOWN, POSSIBLE_DUPLICATE, INSUFFICIENT_DATA,
    ALL_CLASSIFICATIONS,
)
from discovery.registry import REGISTRY_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_by_classification(records: list[dict]) -> dict[str, int]:
    counts = {c: 0 for c in ALL_CLASSIFICATIONS}
    for r in records:
        counts[r["classification"]] = counts.get(r["classification"], 0) + 1
    return counts


def _changed_fields_summary(changed_fields: list[dict]) -> str:
    return "; ".join(f"{c['label']}: {c['old']!r} → {c['new']!r}" for c in changed_fields)


# ---------------------------------------------------------------------------
# Individual writers
# ---------------------------------------------------------------------------

def _write_results_json(
    records: list[dict],
    row_fields_by_idx: dict[int, dict],
    counts: dict[str, int],
    run_id: str,
    output_dir: Path,
) -> None:
    out = {
        "run_id": run_id,
        "classification_counts": counts,
        "records": [],
    }
    for rec in records:
        fields = row_fields_by_idx.get(rec["row_idx"], {})
        out["records"].append({
            "row_idx": rec["row_idx"],
            "classification": rec["classification"],
            "match_basis": rec["match_basis"],
            "entry_id": rec["entry_id"],
            "changed_fields": rec["changed_fields"],
            "duplicate_of_row_idx": rec["duplicate_of_row_idx"],
            "practice_name": fields.get("practice_name", ""),
            "website_url": fields.get("website_url", ""),
            "website_domain": fields.get("website_domain", ""),
            "phone": fields.get("phone", ""),
            "phone_digits": fields.get("phone_digits", ""),
            "address_full": fields.get("address_full", ""),
            "address_city": fields.get("address_city", ""),
            "address_state": fields.get("address_state", ""),
            "address_zip": fields.get("address_zip", ""),
            "google_place_id": fields.get("google_place_id", ""),
            "google_category": fields.get("google_category", ""),
            "npi": fields.get("npi", ""),
        })
    _write_json(output_dir / "discovery_results.json", out)


def _write_results_csv(
    records: list[dict],
    row_fields_by_idx: dict[int, dict],
    output_dir: Path,
) -> None:
    fieldnames = [
        "row_idx", "classification", "practice_name", "address_city",
        "address_state", "website_url", "phone", "google_place_id",
        "match_basis", "entry_id", "duplicate_of_row_idx", "changed_fields_summary",
    ]
    rows = []
    for rec in records:
        fields = row_fields_by_idx.get(rec["row_idx"], {})
        rows.append({
            "row_idx": rec["row_idx"],
            "classification": rec["classification"],
            "practice_name": fields.get("practice_name", ""),
            "address_city": fields.get("address_city", ""),
            "address_state": fields.get("address_state", ""),
            "website_url": fields.get("website_url", ""),
            "phone": fields.get("phone", ""),
            "google_place_id": fields.get("google_place_id", ""),
            "match_basis": rec["match_basis"] or "",
            "entry_id": rec["entry_id"] or "",
            "duplicate_of_row_idx": (
                "" if rec["duplicate_of_row_idx"] is None
                else str(rec["duplicate_of_row_idx"])
            ),
            "changed_fields_summary": _changed_fields_summary(rec.get("changed_fields") or []),
        })
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    (output_dir / "discovery_results.csv").write_text(buf.getvalue(), encoding="utf-8")


def _write_run_log(
    counts: dict[str, int],
    registry_count_before: int,
    preview_count: int,
    run_id: str,
    input_row_count: int,
    started_at: str,
    finished_at: str,
    output_dir: Path,
) -> None:
    _write_json(output_dir / "discovery_run_log.json", {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "input_row_count": input_row_count,
        "classification_counts": counts,
        "registry_entry_count_before": registry_count_before,
        "registry_entry_count_after_preview": preview_count,
    })


def _build_preview_registry(
    records: list[dict],
    row_fields_by_idx: dict[int, dict],
    registry: dict,
    run_id: str,
) -> dict:
    """
    Build a preview of what the registry would look like after this run.

    Only NEW and CHANGED records affect the preview — KNOWN / POSSIBLE_DUPLICATE /
    INSUFFICIENT_DATA rows produce no registry changes.
    """
    import copy
    preview = copy.deepcopy(registry)
    entries = preview.setdefault("entries", {})
    now = datetime.now(timezone.utc).isoformat()

    for rec in records:
        fields = row_fields_by_idx.get(rec["row_idx"], {})
        if rec["classification"] == NEW:
            new_id = uuid.uuid4().hex
            entries[new_id] = {
                "entry_id": new_id,
                "google_place_id": fields.get("google_place_id", ""),
                "website_domain": fields.get("website_domain", ""),
                "phone_digits": fields.get("phone_digits", ""),
                "name_normalized": fields.get("name_normalized", ""),
                "address_normalized": fields.get("address_normalized", ""),
                "practice_name": fields.get("practice_name", ""),
                "website_url": fields.get("website_url", ""),
                "phone": fields.get("phone", ""),
                "address_city": fields.get("address_city", ""),
                "address_state": fields.get("address_state", ""),
                "address_zip": fields.get("address_zip", ""),
                "google_category": fields.get("google_category", ""),
                "npi": fields.get("npi", ""),
                "first_seen_run_id": run_id,
                "first_seen_at": now,
                "last_seen_run_id": run_id,
                "last_seen_at": now,
                "last_tier": "",
                "last_score": 0,
                "runs_seen": [run_id],
                "change_log": [],
            }
        elif rec["classification"] == CHANGED and rec["entry_id"]:
            entry = entries.get(rec["entry_id"])
            if entry is None:
                continue
            for ch in rec.get("changed_fields") or []:
                ch_log = {**ch, "detected_at": now, "detected_in_run_id": run_id}
                entry.setdefault("change_log", []).append(ch_log)
            for field, key in (
                ("website_domain", "website_domain"),
                ("phone_digits", "phone_digits"),
                ("practice_name", "practice_name"),
                ("name_normalized", "name_normalized"),
                ("address_normalized", "address_normalized"),
                ("google_category", "google_category"),
            ):
                if fields.get(field):
                    entry[key] = fields[field]
            entry["last_seen_run_id"] = run_id
            entry["last_seen_at"] = now
            if run_id not in entry.get("runs_seen", []):
                entry.setdefault("runs_seen", []).append(run_id)

    preview["is_preview"] = True
    preview["preview_run_id"] = run_id
    preview["entry_count"] = len(entries)
    preview["updated_at"] = now
    return preview


def _write_json(path: Path, data: dict) -> None:
    """Atomically write JSON to *path*."""
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def write_results(
    records: list[dict],
    row_fields_by_idx: dict[int, dict],
    registry: dict,
    output_dir: Path,
    run_id: str,
    started_at: str,
    finished_at: str,
) -> None:
    """
    Write all four discovery output files to *output_dir*.

    Parameters
    ----------
    records:
        List of classification dicts from classifier.classify().
    row_fields_by_idx:
        Mapping of row_idx → extracted field dict (for display and preview).
    registry:
        The loaded registry (used for the preview; not mutated).
    output_dir:
        Target directory — created if absent.
    run_id:
        Stable identifier for this discovery run.
    started_at / finished_at:
        ISO-8601 timestamps bounding the run.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    registry_count_before = len((registry.get("entries") or {}))
    counts = _count_by_classification(records)

    _write_results_json(records, row_fields_by_idx, counts, run_id, output_dir)
    _write_results_csv(records, row_fields_by_idx, output_dir)

    preview = _build_preview_registry(records, row_fields_by_idx, registry, run_id)
    _write_json(output_dir / "updated_registry_preview.json", preview)

    _write_run_log(
        counts=counts,
        registry_count_before=registry_count_before,
        preview_count=len(preview.get("entries") or {}),
        run_id=run_id,
        input_row_count=len(records),
        started_at=started_at,
        finished_at=finished_at,
        output_dir=output_dir,
    )
