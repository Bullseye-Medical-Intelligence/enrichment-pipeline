"""
config.py
Environment variable loading and application-level constants.
All configurable values live here. No magic numbers or strings elsewhere.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Required — startup fails if these are absent
# ---------------------------------------------------------------------------

PIPELINE_API_KEY: str = os.environ.get("PIPELINE_API_KEY", "")
PIPELINE_REPO_PATH: Path = Path(os.environ.get("PIPELINE_REPO_PATH", ""))
OUTPUT_RUNS_PATH: Path = Path(os.environ.get("OUTPUT_RUNS_PATH", ""))

# ---------------------------------------------------------------------------
# Optional with defaults
# ---------------------------------------------------------------------------

PYTHON_EXECUTABLE: str = os.environ.get("PYTHON_EXECUTABLE", "python3")
MAX_CSV_SIZE_MB: int = int(os.environ.get("MAX_CSV_SIZE_MB", "50"))
MAX_CSV_ROWS: int = int(os.environ.get("MAX_CSV_ROWS", "10000"))
HOST: str = os.environ.get("HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PORT", "8000"))

# ---------------------------------------------------------------------------
# Derived constants
# ---------------------------------------------------------------------------

MAX_CSV_SIZE_BYTES: int = MAX_CSV_SIZE_MB * 1024 * 1024
MAX_RUNS_RETURNED: int = 50
PIPELINE_VERSION: str = "v1.0"
PIPELINE_SCRIPT: str = "pipeline.py"
STATUS_FILENAME: str = "status.json"

VALID_SOURCE_TYPES: frozenset[str] = frozenset({"outscraper", "manual"})

OUTSCRAPER_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {"name", "full_address", "phone", "site", "type"}
)
MANUAL_REQUIRED_COLUMNS: frozenset[str] = frozenset({"practice_name"})

REQUIRED_COLUMNS_BY_SOURCE: dict[str, frozenset[str]] = {
    "outscraper": OUTSCRAPER_REQUIRED_COLUMNS,
    "manual": MANUAL_REQUIRED_COLUMNS,
}

VALID_RUN_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "complete", "failed"}
)
