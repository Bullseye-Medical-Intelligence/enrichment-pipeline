"""
csv_writer.py
Writes enriched_targets.csv — flat export without nested signal detail.
Useful for quick review in Excel/Sheets.
"""

import csv
from pathlib import Path

from output.atomic_write import atomic_write


# Columns included in the flat CSV export (signals are excluded — too nested)
CSV_COLUMNS = [
    "id",
    "practice_name",
    "specialty",
    "npi_optional",
    "website_url",
    "phone",
    "address_city",
    "address_state",
    "address_zip",
    "metro_region_tag",
    "state_mandate_status",
    "bullseye_score",
    "fit_signal_score",
    "confidence_score",
    "confidence_band",
    "fit_confidence_status",
    "target_tier",
    "exclusion_status",
    "exclusion_reason",
    "source_confidence",
    "enrichment_status",
    "qc_status",
    "date_enriched",
    "enrichment_run_id",
    "source_pipeline_version",
    "raw_input_source",
    "llm_model_used",
    "llm_prompt_version",
    "internal_notes",
    # Sales angle as a pipe-joined string for flat export
    "sales_angle_flat",
    # Provider names as pipe-joined string
    "provider_names_flat",
]


def _flatten_record(record: dict) -> dict:
    """Flatten nested fields to strings for CSV output."""
    flat = {}
    for col in CSV_COLUMNS:
        if col == "sales_angle_flat":
            sales = record.get("sales_angle") or []
            flat[col] = " | ".join(str(p) for p in sales) if sales else ""
        elif col == "provider_names_flat":
            names = record.get("provider_names") or []
            flat[col] = " | ".join(str(n) for n in names) if names else ""
        else:
            val = record.get(col, "")
            if val is None:
                flat[col] = ""
            elif isinstance(val, list):
                flat[col] = " | ".join(str(v) for v in val)
            else:
                flat[col] = str(val)
    return flat


def write_csv(records: list[dict], output_dir: str = "./output",
               pipeline_version: str = "v1.0") -> str:
    """
    Write enriched records to enriched_targets.csv (flat, no signals nested).

    Args:
        records: List of finalized record dicts.
        output_dir: Directory to write into.
        pipeline_version: Pipeline version string to include in records.

    Returns:
        Absolute path to the written file.
    """
    output_path = Path(output_dir) / "enriched_targets.csv"

    rows = []
    for record in records:
        # Inject pipeline_version if not already present
        if not record.get("source_pipeline_version"):
            record = dict(record)
            record["source_pipeline_version"] = pipeline_version
        rows.append(_flatten_record(record))

    def _write(f):
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    atomic_write(output_path, _write, newline="")

    size_kb = output_path.stat().st_size / 1024
    print(f"[csv_writer] Wrote {len(records)} records -> {output_path} ({size_kb:.1f} KB)")
    return str(output_path.resolve())
