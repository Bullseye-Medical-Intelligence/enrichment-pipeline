"""
zip_lookup.py
Offline ZIP-code to (city, state) resolution.

Backed by a bundled dataset (us_zip_city_state.csv) so resolution is
deterministic and works fully offline — no network call, no API spend, no
hallucination. Used to fill city/state on ingested records when the source
list carries a ZIP but no place name.
"""

import csv
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent / "us_zip_city_state.csv"

# Lazily loaded {zip5: (city, state)} so the file is read once per process.
_ZIP_INDEX: dict[str, tuple[str, str]] | None = None


def _normalize_zip(zip_code: str) -> str:
    """Return the 5-digit ZIP5 form of a raw ZIP string, or '' if not usable."""
    digits = "".join(ch for ch in str(zip_code or "") if ch.isdigit())
    if len(digits) < 5:
        return ""
    return digits[:5]


def _load_index() -> dict[str, tuple[str, str]]:
    """Load (and cache) the bundled ZIP dataset into memory."""
    global _ZIP_INDEX
    if _ZIP_INDEX is None:
        index: dict[str, tuple[str, str]] = {}
        if _DATA_PATH.exists():
            with open(_DATA_PATH, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    z = (row.get("zip") or "").strip()
                    if z:
                        index[z] = (
                            (row.get("city") or "").strip(),
                            (row.get("state") or "").strip(),
                        )
        _ZIP_INDEX = index
    return _ZIP_INDEX


def infer_city_state(zip_code: str) -> tuple[str, str]:
    """Resolve a ZIP code to (city, state). Returns ('', '') when unknown."""
    z = _normalize_zip(zip_code)
    if not z:
        return ("", "")
    return _load_index().get(z, ("", ""))
