"""
log_writer.py
Writes run_log.json — run metadata, record counts, errors, and warnings.
Every pipeline run produces this file. No real client data in logs.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from output.atomic_write import atomic_write


def write_run_log(run_id: str, records: list[dict], errors: list[dict],
                   warnings: list[str], input_file: str, input_source_type: str,
                   records_input: int, pipeline_version: str = "v1.0",
                   output_dir: str = "./output",
                   llm_usage: dict | None = None) -> str:
    """
    Write run_log.json summarizing the pipeline run.

    Args:
        run_id: Unique run identifier.
        records: Final record list (used to count outcomes).
        errors: List of per-record error dicts from the pipeline.
        warnings: List of warning strings.
        input_file: Name/path of the input file.
        input_source_type: "outscraper", "manual", etc.
        records_input: Total records read from input (before dedup).
        pipeline_version: Pipeline version string.
        output_dir: Directory to write into.
        llm_usage: Optional run-level token totals (llm_input_tokens,
            llm_output_tokens, llm_call_count). Omitted from the log when
            None (e.g. ingest-only runs) so pre-capture and no-LLM runs
            are distinguishable from a zero-token run.

    Returns:
        Absolute path to the written log file.
    """
    output_path = Path(output_dir) / "run_log.json"

    # Count outcomes from final records
    records_output = len(records)
    records_excluded = sum(1 for r in records if r.get("exclusion_status") == "EXCLUDED")
    records_needs_review = sum(
        1 for r in records if r.get("enrichment_status") == "needs_review"
    )
    records_failed = sum(
        1 for r in records if r.get("enrichment_status") == "failed"
    )
    records_insufficient_context = sum(
        1 for r in records if r.get("source_confidence") in ("limited", "failed")
    )

    # Get model info from first successfully enriched record
    primary_model = "unknown"
    prompt_version = "unknown"
    verification_model = os.environ.get("OPENAI_MODEL", "unknown")

    for r in records:
        if r.get("llm_model_used"):
            primary_model = r["llm_model_used"]
        if r.get("llm_prompt_version"):
            prompt_version = r["llm_prompt_version"]
        if primary_model != "unknown" and prompt_version != "unknown":
            break

    # Sanitize errors — strip any PII or full API responses
    safe_errors = []
    for err in errors:
        safe_errors.append({
            "record_id": err.get("record_id", "unknown"),
            "step": err.get("step", "unknown"),
            "error": str(err.get("error", ""))[:200],
            "resolution": err.get("resolution", ""),
        })

    log = {
        "run_id": run_id,
        "run_timestamp": datetime.utcnow().isoformat() + "Z",
        "pipeline_version": pipeline_version,
        "input_file": str(input_file),
        "input_source_type": input_source_type,
        "records_input": records_input,
        "records_output": records_output,
        "records_excluded": records_excluded,
        "records_needs_review": records_needs_review,
        "records_failed": records_failed,
        "records_insufficient_context": records_insufficient_context,
        "records_skipped": max(0, records_input - records_output),
        "llm_primary_model": primary_model,
        "llm_verification_model": verification_model,
        "prompt_version": prompt_version,
        "errors": safe_errors,
        "warnings": warnings,
    }
    if llm_usage is not None:
        log["llm_input_tokens"] = int(llm_usage.get("llm_input_tokens", 0))
        log["llm_output_tokens"] = int(llm_usage.get("llm_output_tokens", 0))
        log["llm_call_count"] = int(llm_usage.get("llm_call_count", 0))

    atomic_write(output_path, lambda f: json.dump(log, f, indent=2, ensure_ascii=False))

    print(f"[log_writer] Run log -> {output_path}")
    print(
        f"[log_writer] Summary: {records_output} output | "
        f"{records_excluded} excluded | "
        f"{records_needs_review} needs_review | "
        f"{records_failed} failed"
    )
    return str(output_path.resolve())
