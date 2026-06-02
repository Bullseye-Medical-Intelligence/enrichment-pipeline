"""
validator.py
Pre-flight validation for CSV uploads. All checks run before any run
directory is created or subprocess is spawned.
"""

import csv
import difflib
import io
import logging

from fastapi import UploadFile

from config import (
    MAX_CSV_ROWS,
    MAX_CSV_SIZE_BYTES,
    OUTSCRAPER_URL_COLUMNS,
    REQUIRED_COLUMNS_BY_SOURCE,
    VALID_SOURCE_TYPES,
)

logger = logging.getLogger(__name__)


async def validate_csv_upload(
    file: UploadFile,
    source_type: str,
    project_id: str,
) -> tuple[bytes, int]:
    """
    Run all pre-flight checks on an incoming CSV upload.

    Project/ICP resolution is handled by the runner before this is called.
    This function validates only the CSV file itself.

    Checks (in order):
      1. source_type is valid
      2. project_id is non-empty
      3. File size is under MAX_CSV_SIZE_BYTES
      4. File decodes as UTF-8
      5. File parses as valid CSV
      6. CSV has at least one data row
      7. Row count is under MAX_CSV_ROWS
      8. Required columns are present for the given source_type

    Args:
        file: The uploaded file object.
        source_type: 'outscraper' or 'manual'.
        project_id: Non-empty project identifier string.

    Returns:
        (file_bytes, row_count) on success.

    Raises:
        ValueError with a descriptive message on any failed check.
    """
    _validate_source_type(source_type)
    _validate_project_id(project_id)

    content = await file.read()

    _validate_file_size(content)
    text = _decode_csv_bytes(content)
    rows, fieldnames = _parse_csv(text)
    _validate_row_count(rows)
    _validate_columns(fieldnames, source_type)

    return content, len(rows)


def _validate_source_type(source_type: str) -> None:
    """Raise ValueError if source_type is not a known value."""
    if source_type not in VALID_SOURCE_TYPES:
        raise ValueError(
            f"Invalid source_type '{source_type}'. "
            f"Must be one of: {sorted(VALID_SOURCE_TYPES)}"
        )


def _validate_project_id(project_id: str) -> None:
    """Raise ValueError if project_id is empty or whitespace-only."""
    if not project_id or not project_id.strip():
        raise ValueError("project_id is required and cannot be empty")


def _validate_file_size(content: bytes) -> None:
    """Raise ValueError if the file exceeds MAX_CSV_SIZE_BYTES."""
    if len(content) > MAX_CSV_SIZE_BYTES:
        size_mb = len(content) / (1024 * 1024)
        limit_mb = MAX_CSV_SIZE_BYTES // (1024 * 1024)
        raise ValueError(
            f"File is {size_mb:.1f} MB, which exceeds the {limit_mb} MB limit"
        )


def _decode_csv_bytes(content: bytes) -> str:
    """Decode bytes as UTF-8 (with BOM strip). Raise ValueError on failure."""
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(
            "File could not be decoded as UTF-8. "
            "Ensure the CSV is saved with UTF-8 encoding."
        )


def _parse_csv(text: str) -> tuple[list[dict], list[str]]:
    """
    Parse CSV text into rows and fieldnames.

    Returns:
        (rows, fieldnames) where rows is a list of dicts and
        fieldnames is the list of column headers.

    Raises:
        ValueError if the text is not valid CSV.
    """
    try:
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        fieldnames = [c.strip().lower() for c in (reader.fieldnames or [])]
        return rows, fieldnames
    except csv.Error as e:
        raise ValueError(f"File is not valid CSV: {e}")


def _validate_row_count(rows: list[dict]) -> None:
    """Raise ValueError if the CSV has no rows or exceeds MAX_CSV_ROWS."""
    if not rows:
        raise ValueError("CSV file contains no data rows")
    if len(rows) > MAX_CSV_ROWS:
        raise ValueError(
            f"CSV contains {len(rows):,} rows, "
            f"which exceeds the {MAX_CSV_ROWS:,} row limit"
        )


def _validate_columns(fieldnames: list[str], source_type: str) -> None:
    """Raise ValueError if required columns are missing for the source_type."""
    required = REQUIRED_COLUMNS_BY_SOURCE[source_type]
    actual = set(fieldnames)
    missing = required - actual
    if missing:
        raise ValueError(
            f"CSV is missing required columns for source '{source_type}': "
            f"{sorted(missing)}"
        )
    if source_type == "outscraper" and not (OUTSCRAPER_URL_COLUMNS & actual):
        accepted = ", ".join(sorted(OUTSCRAPER_URL_COLUMNS))
        raise ValueError(
            f"CSV is missing a website URL column for source 'outscraper'. "
            f"Accepted column names: {accepted}."
        )


def preflight_summary(content: bytes, source_type: str) -> dict:
    """
    Validate a CSV and return a human-facing import summary without mutating it.

    Runs the same hard checks as validate_csv_upload, then inspects the rows for
    likely duplicates and data-quality warnings to surface before a run starts.

    Raises:
        ValueError on any hard validation failure.
    """
    _validate_source_type(source_type)
    _validate_file_size(content)
    text = _decode_csv_bytes(content)
    rows, fieldnames = _parse_csv(text)
    _validate_row_count(rows)
    _validate_columns(fieldnames, source_type)
    return _analyze_rows(rows, fieldnames, source_type)


def _first_value(row: dict, keys: tuple[str, ...]) -> str:
    """Return the first non-empty value across the given column names."""
    for key in keys:
        value = (row.get(key) or "").strip()
        if value:
            return value
    return ""


_FUZZY_SIMILAR_THRESHOLD = 0.82
_FUZZY_MAX_ROWS = 500   # skip fuzzy pass on very large files to stay fast


def _analyze_rows(rows: list[dict], fieldnames: list[str], source_type: str) -> dict:
    """Summarize a parsed CSV: importable count, duplicates, and similar-name pairs."""
    name_cols = ("name", "practice_name")
    url_cols = tuple(OUTSCRAPER_URL_COLUMNS)
    state_cols = ("state", "address_state")

    seen_urls: dict[str, int] = {}          # url -> first_row number (1-based, header=1)
    seen_name_state: dict[str, int] = {}    # "name_lower|state" -> first_row
    names_by_state: dict[str, list[tuple[int, str, str]]] = {}  # state -> [(row, name, name_lower)]

    duplicate_records: list[dict] = []
    dropped_count = 0

    for i, raw in enumerate(rows, start=2):   # row 1 is the CSV header
        row = {k.lower(): v for k, v in raw.items() if k}
        name = _first_value(row, name_cols)
        if not name:
            dropped_count += 1
            continue
        url = _first_value(row, url_cols).lower().rstrip("/")
        state = _first_value(row, state_cols)
        state_display = state.upper() if state else ""
        name_key = f"{name.lower()}|{state.lower()}"

        first_row = None
        if url and url in seen_urls:
            first_row = seen_urls[url]
        elif name_key in seen_name_state:
            first_row = seen_name_state[name_key]

        if first_row is not None:
            duplicate_records.append({
                "name": name,
                "url": url,
                "state": state_display,
                "row": i,
                "first_row": first_row,
                "type": "exact",
                "similar_to": "",
            })
        else:
            if url:
                seen_urls[url] = i
            seen_name_state[name_key] = i
            bucket = names_by_state.setdefault(state.lower(), [])
            bucket.append((i, name, name.lower()))

    # Fuzzy similar-name pass — O(n²) per state bucket, skipped on large files.
    similar_records: list[dict] = []
    if len(rows) <= _FUZZY_MAX_ROWS:
        for state_key, bucket in names_by_state.items():
            state_display = state_key.upper() if state_key else ""
            for j in range(len(bucket)):
                row_j, name_j, lower_j = bucket[j]
                for k in range(j + 1, len(bucket)):
                    row_k, name_k, lower_k = bucket[k]
                    ratio = difflib.SequenceMatcher(None, lower_j, lower_k).ratio()
                    if _FUZZY_SIMILAR_THRESHOLD <= ratio < 1.0:
                        similar_records.append({
                            "name": name_k,
                            "url": "",
                            "state": state_display,
                            "row": row_k,
                            "first_row": row_j,
                            "type": "similar",
                            "similar_to": name_j,
                        })

    duplicate_count = len(duplicate_records)
    similar_count = len(similar_records)

    warnings: list[str] = []
    if dropped_count:
        warnings.append(
            f"{dropped_count} row(s) have no practice name and cannot be imported."
        )
    if duplicate_count:
        warnings.append(
            f"{duplicate_count} exact duplicate(s) detected "
            "(same website or same practice name + state). "
            "The pipeline will de-duplicate them automatically."
        )
    if similar_count:
        warnings.append(
            f"{similar_count} row(s) have similar names to another row in the same state — "
            "verify these are not the same clinic before importing."
        )
    if source_type == "outscraper" and "type" not in set(fieldnames):
        warnings.append(
            "No 'type' column found. Specialty is inferred from practice names "
            "where possible; unmatched practices are marked Unknown."
        )

    return {
        "row_count": len(rows),
        "importable": len(rows) - dropped_count,
        "duplicate_count": duplicate_count,
        "similar_count": similar_count,
        "dropped_count": dropped_count,
        "warnings": warnings,
        "duplicates": duplicate_records,
        "similar": similar_records,
    }
