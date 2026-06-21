"""
reviews.py
Analyst review persistence. Reads and writes reviews.json per run.

Critical rule: This module never WRITES enriched_targets.json. Pipeline output
is immutable; reviews are additive metadata only. It may READ enriched_targets.json
read-only in one place — to capture a signal's original state when an operator
first overrides it — but it never mutates it. Scores and tiers are owned by the
pipeline and are never recomputed here.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import record_adapter
from schema import VALID_OVERRIDE_TIERS, VALID_QC_STATUSES, ReviewEdit, SignalOverride

logger = logging.getLogger(__name__)

REVIEWS_FILENAME = "reviews.json"
ENRICHED_TARGETS_FILENAME = "enriched_targets.json"


def default_review() -> dict:
    """Return a default review entry for a record that has not been reviewed."""
    return {
        "analyst_note": "",
        "override_tier": None,
        "override_reason": None,
        "qc_status": "pending",
        "reviewed_by": None,
        "reviewed_at": None,
        "extra_sales_angles": [],
        "signal_overrides": {},
    }


def get_reviews(run_id: str, run_directory: Path) -> dict[str, dict]:
    """
    Read reviews.json for the given run.

    Returns:
        Dict mapping record_id → review entry.
        Returns empty dict if reviews.json does not exist yet.
    """
    reviews_path = run_directory / REVIEWS_FILENAME
    if not reviews_path.exists():
        return {}
    try:
        with open(reviews_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to read reviews.json for run %s: %s", run_id, e)
        return {}


def get_review(run_id: str, record_id: str, run_directory: Path) -> dict:
    """
    Return the review entry for a single record, or a default if not yet reviewed.
    """
    all_reviews = get_reviews(run_id, run_directory)
    return all_reviews.get(record_id, default_review())


def stamp_reenriched(run_id: str, record_id: str, run_directory: Path, kind: str) -> dict:
    """Append a re-enriched note to a record's review, preserving the analyst's decision.

    When a record's pipeline data is replaced in place (browser re-crawl or
    operator-provided content), the analyst's prior QC decision, override tier,
    and notes are kept untouched — but a dated line is appended to the analyst
    note so it is clear the underlying data changed under that decision.

    kind is a human label, e.g. "browser re-crawl" or "manual content".
    Returns the updated review entry.
    """
    all_reviews = get_reviews(run_id, run_directory)
    entry = dict(all_reviews.get(record_id) or default_review())

    now = datetime.now(timezone.utc).isoformat()
    stamp = f"Re-enriched on {datetime.now(timezone.utc).date().isoformat()} ({kind})."
    existing = (entry.get("analyst_note") or "").rstrip()
    entry["analyst_note"] = f"{existing}\n{stamp}".strip() if existing else stamp
    entry["reviewed_at"] = now

    all_reviews[record_id] = entry
    _atomic_write(run_directory / REVIEWS_FILENAME, all_reviews)
    return entry


def save_review(
    run_id: str,
    record_id: str,
    edit: ReviewEdit,
    username: str,
    run_directory: Path,
) -> dict:
    """
    Validate and persist a review edit atomically.

    Validation:
        - override_tier must be a known tier or null
        - override_reason is required when override_tier is set
        - qc_status must be a known status

    Args:
        run_id: Run identifier (for logging).
        record_id: Record being reviewed.
        edit: Incoming ReviewEdit from the client.
        username: Authenticated user saving the review.
        run_directory: Filesystem path to the run's output directory.

    Returns:
        The saved review entry dict.

    Raises:
        ValueError with a descriptive message on validation failure.
    """
    _validate_edit(edit)

    now = datetime.now(timezone.utc).isoformat()
    # Preserve existing extra_sales_angles when a standard review edit is saved
    # (the review form doesn't touch angles — they have their own endpoint).
    existing = get_reviews(run_id, run_directory).get(record_id, {})
    entry = {
        "analyst_note": edit.analyst_note.strip(),
        "override_tier": edit.override_tier,
        "override_reason": (edit.override_reason or "").strip() or None,
        "qc_status": edit.qc_status,
        "reviewed_by": username,
        "reviewed_at": now,
        "extra_sales_angles": existing.get("extra_sales_angles", []),
        # Signal overrides have their own endpoint; preserve them across a
        # standard tier/QC review save so a tier edit never wipes them.
        "signal_overrides": existing.get("signal_overrides", {}),
    }

    reviews_path = run_directory / REVIEWS_FILENAME
    all_reviews = get_reviews(run_id, run_directory)
    all_reviews[record_id] = entry

    _atomic_write(reviews_path, all_reviews)
    logger.info(
        "Review saved for run=%s record=%s by %s (qc=%s, override=%s)",
        run_id, record_id, username, edit.qc_status, edit.override_tier,
    )
    return entry


def bulk_approve(
    run_id: str,
    record_ids: list[str],
    username: str,
    run_directory: Path,
) -> int:
    """Approve a batch of records in a single atomic reviews.json write.

    Skips any record_id that is already approved. Records that have no existing
    review entry get a minimal approved entry with no override. Returns the
    count of newly approved records.
    """
    if not record_ids:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    all_reviews = get_reviews(run_id, run_directory)
    approved_count = 0

    for record_id in record_ids:
        existing = all_reviews.get(record_id, {})
        if existing.get("qc_status") == "approved":
            continue
        all_reviews[record_id] = {
            "analyst_note": existing.get("analyst_note", ""),
            "override_tier": existing.get("override_tier"),
            "override_reason": existing.get("override_reason"),
            "qc_status": "approved",
            "reviewed_by": username,
            "reviewed_at": now,
            "extra_sales_angles": existing.get("extra_sales_angles", []),
            "signal_overrides": existing.get("signal_overrides", {}),
        }
        approved_count += 1

    if approved_count:
        _atomic_write(run_directory / REVIEWS_FILENAME, all_reviews)
        logger.info(
            "Bulk approved %d record(s) in run=%s by %s",
            approved_count, run_id, username,
        )

    return approved_count


def get_signal_overrides(run_id: str, record_id: str, run_directory: Path) -> dict:
    """Return the signal_overrides map for one record, keyed by signal_id.

    Empty dict when the record has no overrides yet. Each value is the stored
    override entry (override_state, source_url, override_note, override_by,
    override_at, original_state).
    """
    review = get_review(run_id, record_id, run_directory)
    return review.get("signal_overrides") or {}


def save_signal_override(
    run_id: str,
    record_id: str,
    override: SignalOverride,
    run_directory: Path,
) -> dict:
    """Persist one signal override into the reviews.json overlay atomically.

    On the first override of a signal, captures that signal's original
    signal_state from enriched_targets.json into 'original_state' (read-only
    reference). On re-override, the original_state is preserved while state,
    source, note, and timestamp update. Never writes enriched_targets.json and
    never recomputes scores or tiers. Returns the updated review entry.
    """
    now = datetime.now(timezone.utc).isoformat()
    all_reviews = get_reviews(run_id, run_directory)
    entry = dict(all_reviews.get(record_id) or default_review())
    overrides = dict(entry.get("signal_overrides") or {})

    prior = overrides.get(override.signal_id)
    if prior and "original_state" in prior:
        original_state = prior["original_state"]
    else:
        original_state = _read_original_signal_state(
            record_id, override.signal_id, run_directory
        )

    overrides[override.signal_id] = {
        "signal_id": override.signal_id,
        "override_state": override.override_state,
        "source_url": override.source_url,
        "override_note": override.override_note,
        "override_by": override.override_by,
        "override_at": now,  # server-stamped, always authoritative
        "original_state": original_state,
    }
    entry["signal_overrides"] = overrides
    all_reviews[record_id] = entry

    _atomic_write(run_directory / REVIEWS_FILENAME, all_reviews)
    logger.info(
        "Signal override saved run=%s record=%s signal=%s state=%s by=%s",
        run_id, record_id, override.signal_id, override.override_state,
        override.override_by,
    )
    return entry


def apply_signal_overrides(record: dict, review: dict) -> dict:
    """Return a copy of `record` with operator signal overrides applied.

    For each overridden signal_id present on the record, replaces signal_state,
    evidence_text (the override_note, or "Operator-verified" when blank), and
    source_url, and marks the signal with is_override=True. Signals without an
    override pass through unchanged.

    Scores and tier (bullseye_score, fit_signal_score, confidence_score,
    target_tier) are never read or recomputed here — they flow through untouched
    from the pipeline record. A record whose review has no signal_overrides is
    returned unchanged (no regression to existing overlay behavior).
    """
    overrides = (review or {}).get("signal_overrides") or {}
    if not overrides:
        return record

    merged_signals = []
    for sig in record.get("signals", []):
        ov = overrides.get(sig.get("signal_id"))
        if not ov:
            merged_signals.append(sig)
            continue
        new_sig = dict(sig)
        new_sig["signal_state"] = ov.get("override_state", sig.get("signal_state"))
        new_sig["evidence_text"] = ov.get("override_note") or "Operator-verified"
        new_sig["source_url"] = ov.get("source_url", "")
        new_sig["is_override"] = True
        merged_signals.append(new_sig)

    return {**record, "signals": merged_signals}


def _read_original_signal_state(
    record_id: str, signal_id: str, run_directory: Path
) -> str:
    """Read a signal's current signal_state from enriched_targets.json (read-only).

    Used once, when a signal is first overridden, to snapshot its pipeline state.
    Returns "" when the file, record, or signal cannot be found. Never writes.
    """
    results_path = run_directory / ENRICHED_TARGETS_FILENAME
    if not results_path.exists():
        return ""
    try:
        with open(results_path, "r", encoding="utf-8") as f:
            records = record_adapter.normalize_records_payload(json.load(f))
    except (json.JSONDecodeError, OSError) as e:
        logger.error(
            "Failed to read %s while capturing original signal state: %s",
            results_path, e,
        )
        return ""

    for record in records:
        if record_adapter.get_record_id(record) == record_id:
            for sig in record.get("signals", []):
                if sig.get("signal_id") == signal_id:
                    return sig.get("signal_state", "")
            return ""
    return ""


def _validate_edit(edit: ReviewEdit) -> None:
    """Raise ValueError if the edit fails business validation."""
    if edit.override_tier is not None and edit.override_tier not in VALID_OVERRIDE_TIERS:
        raise ValueError(
            f"Invalid override_tier '{edit.override_tier}'. "
            f"Must be one of: {sorted(VALID_OVERRIDE_TIERS)}"
        )

    if edit.override_tier is not None and not (edit.override_reason or "").strip():
        raise ValueError(
            "override_reason is required when setting an override tier. "
            "Please describe why you are overriding the pipeline's classification."
        )

    if edit.qc_status not in VALID_QC_STATUSES:
        raise ValueError(
            f"Invalid qc_status '{edit.qc_status}'. "
            f"Must be one of: {sorted(VALID_QC_STATUSES)}"
        )


def _atomic_write(path: Path, data: dict) -> None:
    """Write data to path atomically: write temp file then rename."""
    directory = path.parent
    try:
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as e:
        raise OSError(f"Failed to write {path}: {e}") from e
