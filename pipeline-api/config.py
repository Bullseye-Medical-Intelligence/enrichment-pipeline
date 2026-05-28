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

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
PYTHON_EXECUTABLE: str = os.environ.get("PYTHON_EXECUTABLE", "python3")
MAX_CSV_SIZE_MB: int = int(os.environ.get("MAX_CSV_SIZE_MB", "50"))
MAX_CSV_ROWS: int = int(os.environ.get("MAX_CSV_ROWS", "10000"))
MAX_CONCURRENT_RUNS: int = int(os.environ.get("MAX_CONCURRENT_RUNS", "3"))
HOST: str = os.environ.get("HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PORT", "8000"))

# Project configs and ICP profiles live alongside the runs directory by default.
# Each can be overridden independently for non-standard layouts.
_OUTPUT_BASE: Path = OUTPUT_RUNS_PATH.parent
PROJECTS_PATH: Path = Path(os.environ.get("PROJECTS_PATH", str(_OUTPUT_BASE / "projects")))
ICP_PROFILES_PATH: Path = Path(
    os.environ.get("ICP_PROFILES_PATH", str(_OUTPUT_BASE / "icp_profiles"))
)

# ---------------------------------------------------------------------------
# Derived constants
# ---------------------------------------------------------------------------

MAX_CSV_SIZE_BYTES: int = MAX_CSV_SIZE_MB * 1024 * 1024
MAX_RUNS_RETURNED: int = 50
PIPELINE_VERSION: str = "v1.0"
PIPELINE_SCRIPT: str = "pipeline.py"
STATUS_FILENAME: str = "status.json"

# ---------------------------------------------------------------------------
# Projects and ICP profiles
# ---------------------------------------------------------------------------

PROJECT_CONFIG_FILENAME: str = "project_config.json"
PROJECT_CONFIG_SNAPSHOT_FILENAME: str = "project_config_snapshot.json"
ICP_SNAPSHOT_FILENAME: str = "icp_snapshot.json"

# Fields an operator-created project_config.json must contain to run a pipeline.
REQUIRED_PROJECT_FIELDS: tuple[str, ...] = (
    "project_id",
    "client_name",
    "target_specialty",
    "target_geography",
    "icp_profile_id",
)

# Fields an ICP profile JSON file must contain to be usable.
REQUIRED_ICP_FIELDS: tuple[str, ...] = ("icp_id", "name", "version", "signals")

# Generic defaults applied to a new project. No specialty-specific values here:
# exclusion rules are practice-structure rules and crawl keywords are generic
# site sections, not condition/specialty terms.
DEFAULT_BULLSEYE_MIN_SCORE: int = 75
DEFAULT_MAX_PAGES_PER_PRACTICE: int = 5
DEFAULT_REQUEST_TIMEOUT_SECONDS: int = 60
DEFAULT_REQUEST_RETRIES: int = 3
DEFAULT_IO_CONCURRENCY: int = 6
DEFAULT_EXCLUSION_RULES: tuple[str, ...] = (
    "wrong_specialty",
    "outside_geography",
    "no_web_presence",
    "hospital_owned",
    "health_system_affiliated",
)
DEFAULT_SUBPAGE_KEYWORDS: tuple[str, ...] = (
    "services",
    "providers",
    "about",
    "team",
    "staff",
    "physicians",
    "contact",
)

VALID_SOURCE_TYPES: frozenset[str] = frozenset({"outscraper", "manual"})

OUTSCRAPER_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {"name", "phone"}
)
OUTSCRAPER_URL_COLUMNS: frozenset[str] = frozenset({"site", "website"})
MANUAL_REQUIRED_COLUMNS: frozenset[str] = frozenset({"practice_name"})

REQUIRED_COLUMNS_BY_SOURCE: dict[str, frozenset[str]] = {
    "outscraper": OUTSCRAPER_REQUIRED_COLUMNS,
    "manual": MANUAL_REQUIRED_COLUMNS,
}

VALID_RUN_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "complete", "failed"}
)

# ---------------------------------------------------------------------------
# UI / session auth
# ---------------------------------------------------------------------------

# Single-user mode (UI_USERS overrides these when set)
UI_USERNAME: str = os.environ.get("UI_USERNAME", "")
UI_PASSWORD: str = os.environ.get("UI_PASSWORD", "")

# Multi-user: "user1:pass1,user2:pass2"
UI_USERS_RAW: str = os.environ.get("UI_USERS", "")

# Secret key for signing session cookies — required for production
SESSION_SECRET_KEY: str = os.environ.get("SESSION_SECRET_KEY", "")

# Session lifetime in hours
SESSION_MAX_AGE_HOURS: int = int(os.environ.get("SESSION_MAX_AGE_HOURS", "8"))


def get_valid_users() -> dict[str, str]:
    """Return {username: password} from UI_USERS or UI_USERNAME/UI_PASSWORD."""
    if UI_USERS_RAW:
        users = {}
        for pair in UI_USERS_RAW.split(","):
            pair = pair.strip()
            if ":" in pair:
                u, p = pair.split(":", 1)
                users[u.strip()] = p.strip()
        return users
    if UI_USERNAME and UI_PASSWORD:
        return {UI_USERNAME: UI_PASSWORD}
    return {}
