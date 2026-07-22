"""
verify_run.py — CLI entry point for the post-run Needs Verification pass.

Usage:
    python verify_run.py --run-dir /output/runs/RUN-20260616-140000 --icp config/clients/obgyn_femasys/icp_checklist.json

Reads enriched_targets.json from the run directory, runs anchor-check + blind
GPT re-extraction on Needs Verification records, writes results back atomically.
Prints a JSON summary to stdout.

Reads credentials from pipeline-api/.env (OPENAI_API_KEY, OPENAI_MODEL).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Load .env from pipeline-api/ when running from repo root
_env_path = Path(__file__).parent / "pipeline-api" / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Post-run Needs Verification pass")
    parser.add_argument("--run-dir", required=True, help="Path to the run directory")
    parser.add_argument("--icp", required=True, help="Path to the ICP checklist JSON")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        sys.exit(f"Run directory not found: {run_dir}")

    icp_path = Path(args.icp)
    if not icp_path.exists():
        sys.exit(f"ICP file not found: {icp_path}")

    icp_data = json.loads(icp_path.read_text(encoding="utf-8"))
    icp_signals = icp_data.get("signals") or icp_data.get("icp_signals") or []

    from enrichment.verifier import run_verification_pass
    from output.atomic_write import ConcurrentRunChange

    print(f"Running verification pass on {run_dir.name}…")
    try:
        stats = run_verification_pass(run_dir, icp_signals)
    except ConcurrentRunChange as e:
        sys.exit(str(e))
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
