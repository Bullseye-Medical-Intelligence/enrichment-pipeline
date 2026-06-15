"""
discovery_cli.py

Thin subprocess entry point onto the discovery package, mirroring the
simulate_icp.py pattern. The pipeline-api spawns this so it never imports the
discovery engine in-process (the API and the engine live in separate components;
the API calls the engine only through a subprocess boundary).

Reads an Outscraper CSV, compares it against the master practice registry, and
writes the four discovery output files into --output-dir:
    discovery_results.json
    discovery_results.csv
    discovery_run_log.json
    updated_registry_preview.json

The source registry is never mutated — only the preview is written.

Usage:
    python discovery_cli.py \
        --input <input.csv> \
        --registry <master_practice_registry.json> \
        --output-dir <run_dir> \
        --run-id <RUN-...>

On success, prints a JSON summary to stdout and exits 0:
    {"run_id": "...", "status": "complete", "total_imported": N,
     "new_count": N, "changed_count": N, "known_count": N,
     "possible_duplicate_count": N, "insufficient_data_count": N}

On failure, prints {"error": "..."} to stdout and exits 1.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure the repo root is importable so `import discovery` resolves to the
# discovery package even when this script is invoked from another working dir.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from discovery import run_discovery
from discovery.classifier import (
    NEW, CHANGED, KNOWN, POSSIBLE_DUPLICATE, INSUFFICIENT_DATA,
)


def _summary_from_counts(run_id: str, counts: dict) -> dict:
    """Flatten the engine's per-classification counts into the API summary shape."""
    new = counts.get(NEW, 0)
    changed = counts.get(CHANGED, 0)
    known = counts.get(KNOWN, 0)
    dup = counts.get(POSSIBLE_DUPLICATE, 0)
    insufficient = counts.get(INSUFFICIENT_DATA, 0)
    return {
        "run_id": run_id,
        "status": "complete",
        "total_imported": new + changed + known + dup + insufficient,
        "new_count": new,
        "changed_count": changed,
        "known_count": known,
        "possible_duplicate_count": dup,
        "insufficient_data_count": insufficient,
    }


def main(argv: list[str] | None = None) -> int:
    """Parse args, run discovery, print a JSON summary. Returns the exit code."""
    parser = argparse.ArgumentParser(description="Run a discovery comparison.")
    parser.add_argument("--input", required=True, help="Path to the Outscraper CSV.")
    parser.add_argument("--registry", required=True, help="Path to master_practice_registry.json.")
    parser.add_argument("--output-dir", required=True, help="Directory for discovery output files.")
    parser.add_argument("--run-id", required=True, help="Caller-supplied run identifier.")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        print(json.dumps({"error": f"input CSV not found: {input_path}"}))
        return 1

    try:
        csv_bytes = input_path.read_bytes()
        result = run_discovery(
            csv_bytes=csv_bytes,
            registry_path=Path(args.registry),
            output_dir=Path(args.output_dir),
            run_id=args.run_id,
        )
    except Exception as exc:  # surface the reason to the parent process
        print(json.dumps({"error": f"discovery failed: {exc}"}))
        return 1

    print(json.dumps(_summary_from_counts(args.run_id, result.counts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
