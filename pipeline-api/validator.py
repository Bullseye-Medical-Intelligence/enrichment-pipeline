"""
validator.py
Pre-flight validation for CSV uploads. All checks run before any run
directory is created or subprocess is spawned.
"""

import csv
import io
import logging

from fastapi import UploadFile

from config import (
    MAX_CSV_ROWS,
    MAX_CSV_SIZE_BYTES,
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
    from config import OUTSCRAPER_URL_COLUMNS
    required = REQUIRED_COLUMNS_BY_SOURCE[source_type]
    actual = set(fieldnames)
    missing = required - actual
    if missing:
        raise ValueError(
            f"CSV is missing required columns for source '{source_type}': "
            f"{sorted(missing)}"
        )
    if source_type == "outscraper" and not (OUTSCRAPER_URL_COLUMNS & actual):
        raise ValueError(
            "CSV is missing a website URL column for source 'outscraper'. "
            "Include either 'site' or 'website'."
        )
