"""
json_writer.py
Writes enriched_targets.json — the full schema output with all signal data.
This is the primary output file imported by the dashboard.
"""

import json
from datetime import datetime
from pathlib import Path

from output.atomic_write import atomic_write


def write_json(records: list[dict], output_dir: str = "./output",
                run_id: str = "") -> str:
    """
    Write enriched records to enriched_targets.json.

    Args:
        records: List of finalized, validated record dicts.
        output_dir: Directory to write into.
        run_id: Current pipeline run ID (included in filename metadata).

    Returns:
        Absolute path to the written file.
    """
    output_path = Path(output_dir) / "enriched_targets.json"

    output = {
        "run_id": run_id,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "record_count": len(records),
        "records": records,
    }

    atomic_write(
        output_path,
        lambda f: json.dump(output, f, indent=2, ensure_ascii=False, default=str),
    )

    size_kb = output_path.stat().st_size / 1024
    print(f"[json_writer] Wrote {len(records)} records -> {output_path} ({size_kb:.1f} KB)")
    return str(output_path.resolve())
