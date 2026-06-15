"""
discovery — Market Radar / Discovery package.

Public entry point: run_discovery()

  result = run_discovery(csv_bytes, registry_path, output_dir)
  print(result.counts)      # {"NEW": 5, "CHANGED": 2, ...}
  print(result.output_dir)  # Path where the four output files were written

The source registry at *registry_path* is never modified.  The run writes
an updated_registry_preview.json to *output_dir* showing what the registry
would look like; the caller decides when (and whether) to commit it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from discovery.outscraper_discovery_adapter import parse_csv, extract_fields
from discovery.registry import load_registry
from discovery.matcher import build_indexes
from discovery.classifier import classify, ALL_CLASSIFICATIONS
from discovery.writer import write_results


@dataclass
class DiscoveryResult:
    """Outcome of a single discovery run."""

    run_id: str
    records: list[dict]
    counts: dict[str, int]
    output_dir: Path
    started_at: str
    finished_at: str

    @property
    def new(self) -> list[dict]:
        from discovery.classifier import NEW
        return [r for r in self.records if r["classification"] == NEW]

    @property
    def changed(self) -> list[dict]:
        from discovery.classifier import CHANGED
        return [r for r in self.records if r["classification"] == CHANGED]

    @property
    def known(self) -> list[dict]:
        from discovery.classifier import KNOWN
        return [r for r in self.records if r["classification"] == KNOWN]

    @property
    def possible_duplicates(self) -> list[dict]:
        from discovery.classifier import POSSIBLE_DUPLICATE
        return [r for r in self.records if r["classification"] == POSSIBLE_DUPLICATE]

    @property
    def insufficient_data(self) -> list[dict]:
        from discovery.classifier import INSUFFICIENT_DATA
        return [r for r in self.records if r["classification"] == INSUFFICIENT_DATA]


def run_discovery(
    csv_bytes: bytes,
    registry_path: Path,
    output_dir: Path,
    run_id: Optional[str] = None,
) -> DiscoveryResult:
    """
    Compare an Outscraper CSV against the master practice registry.

    Parameters
    ----------
    csv_bytes:
        Raw bytes of the uploaded Outscraper CSV.
    registry_path:
        Path to master_practice_registry.json.  File need not exist; an empty
        registry is used when absent.
    output_dir:
        Directory where discovery_results.json / .csv / run_log / preview are written.
    run_id:
        Optional caller-supplied identifier.  A UUID hex is generated when absent.

    Returns
    -------
    DiscoveryResult with classified records and per-classification counts.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = run_id or ("disc_" + uuid.uuid4().hex[:12])

    registry = load_registry(registry_path)
    entries = registry.get("entries") or {}
    indexes = build_indexes(entries)

    raw_rows = parse_csv(csv_bytes)

    classified: list[dict] = []
    row_fields_by_idx: dict[int, dict] = {}
    seen_in_upload: dict = {}

    for idx, row in enumerate(raw_rows):
        fields = extract_fields(row)
        row_fields_by_idx[idx] = fields
        result = classify(idx, fields, indexes, entries, seen_in_upload)
        classified.append(result)

    finished_at = datetime.now(timezone.utc).isoformat()
    counts = {c: 0 for c in ALL_CLASSIFICATIONS}
    for r in classified:
        counts[r["classification"]] += 1

    write_results(
        records=classified,
        row_fields_by_idx=row_fields_by_idx,
        registry=registry,
        output_dir=output_dir,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
    )

    return DiscoveryResult(
        run_id=run_id,
        records=classified,
        counts=counts,
        output_dir=output_dir,
        started_at=started_at,
        finished_at=finished_at,
    )
