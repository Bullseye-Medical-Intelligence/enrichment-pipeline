"""
config.py
Environment variable loading and application-level constants.
All configurable values live here. No magic numbers or strings elsewhere.
"""

import os
import sys
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
# Default to the exact Python running this server (sys.executable) so the
# spawned pipeline subprocess inherits the same venv — including Playwright and
# its Chromium browser. A bare "python3" can resolve to a different interpreter
# with no Playwright installed, which silently breaks browser re-crawl. An
# explicit PYTHON_EXECUTABLE in .env still overrides this.
PYTHON_EXECUTABLE: str = os.environ.get("PYTHON_EXECUTABLE") or sys.executable
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
# API build version — bump MINOR for new capabilities, PATCH for fixes.
BUILD_VERSION: str = "1.1.0"
BUILD_DATE: str = "2026-06-10"
PIPELINE_SCRIPT: str = "pipeline.py"
STATUS_FILENAME: str = "status.json"

# ---------------------------------------------------------------------------
# Projects and ICP profiles
# ---------------------------------------------------------------------------

PROJECT_CONFIG_FILENAME: str = "project_config.json"
PROJECT_CONFIG_SNAPSHOT_FILENAME: str = "project_config_snapshot.json"
ICP_SNAPSHOT_FILENAME: str = "icp_snapshot.json"
SUPPRESSION_LIST_FILENAME: str = "existing_customers.csv"

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
# Bullseye threshold matches the pipeline's enrichment/constants.DEFAULT_BULLSEYE_MIN_SCORE
# (90) so a UI-created project scores identically to a CLI run — one source of truth.
DEFAULT_BULLSEYE_MIN_SCORE: int = 90
DEFAULT_MAX_PAGES_PER_PRACTICE: int = 5
DEFAULT_REQUEST_TIMEOUT_SECONDS: int = 60
DEFAULT_REQUEST_RETRIES: int = 3
DEFAULT_IO_CONCURRENCY: int = 6
DEFAULT_LLM_CONCURRENCY: int = 3
DEFAULT_NEAR_MISS_BAND: int = 0
DEFAULT_EXCLUSION_RULES: tuple[str, ...] = (
    "wrong_specialty",
    "outside_geography",
    "no_web_presence",
    "hospital_owned",
    "health_system_affiliated",
)

# All exclusion rule names recognised by the pipeline engine.
# Kept in sync with enrichment/exclusion_checker.py::ALL_KNOWN_EXCLUSION_RULES.
# Defined here to avoid importing enrichment internals into pipeline-api.
ALL_KNOWN_EXCLUSION_RULE_NAMES: frozenset[str] = frozenset({
    # Hard rules (always active)
    "wrong_specialty",
    "outside_geography",
    "practice_closed",
    "academic_medical_center",
    # Configurable rules (active when listed in active_exclusion_rules)
    "hospital_owned",
    "health_system_affiliated",
    "no_web_presence",
    "competitor_conflict",
    "no_relevant_service_line",
})
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
OUTSCRAPER_URL_COLUMNS: frozenset[str] = frozenset({
    "site", "website", "website_url", "url", "web", "web_url", "website_address",
})
MANUAL_REQUIRED_COLUMNS: frozenset[str] = frozenset({"practice_name"})

REQUIRED_COLUMNS_BY_SOURCE: dict[str, frozenset[str]] = {
    "outscraper": OUTSCRAPER_REQUIRED_COLUMNS,
    "manual": MANUAL_REQUIRED_COLUMNS,
}

VALID_RUN_STATUSES: frozenset[str] = frozenset(
    # "ingested": roster loaded (normalized + structural exclusions) but not yet
    # enriched — the operator triggers enrichment as a second step.
    {"pending", "ingested", "running", "complete", "failed"}
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

# ---------------------------------------------------------------------------
# Brief publishing (Hostinger SFTP)
# ---------------------------------------------------------------------------

HOSTINGER_SFTP_HOST: str = os.environ.get("HOSTINGER_SFTP_HOST", "")
HOSTINGER_SFTP_PORT: int = int(os.environ.get("HOSTINGER_SFTP_PORT", "22"))
HOSTINGER_FTP_PORT: int = int(os.environ.get("HOSTINGER_FTP_PORT", "21"))
HOSTINGER_SFTP_USER: str = os.environ.get("HOSTINGER_SFTP_USER", "")
HOSTINGER_SFTP_PASSWORD: str = os.environ.get("HOSTINGER_SFTP_PASSWORD", "")
HOSTINGER_BRIEFS_REMOTE_ROOT: str = os.environ.get("HOSTINGER_BRIEFS_REMOTE_ROOT", "")
BRIEFS_PUBLIC_BASE_URL: str = os.environ.get(
    "BRIEFS_PUBLIC_BASE_URL", "https://briefs.bullseyemedical.ai"
)
# Plain FTP transmits credentials and content in cleartext — it is an explicit
# opt-in, never an automatic fallback. Default: fail closed on SFTP errors.
HOSTINGER_ALLOW_FTP_FALLBACK: bool = (
    os.environ.get("HOSTINGER_ALLOW_FTP_FALLBACK", "").lower() in ("1", "true", "yes")
)
# Optional pinned SFTP host key (base64 public key, e.g. the field after the
# key type in a known_hosts entry). When set, the server key is verified on
# every connect; on mismatch the upload aborts.
HOSTINGER_SFTP_HOST_KEY: str = os.environ.get("HOSTINGER_SFTP_HOST_KEY", "")
