"""
projects.py
Project configuration storage: create, read, list, and validate projects.

A project owns a single project_config.json that doubles as the pipeline's
run config (pipeline.py --config) and names the ICP profile to enrich against.
All project state lives on the filesystem under config.PROJECTS_PATH. No DB.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
import icp_profiles

logger = logging.getLogger(__name__)

# project_id becomes a directory name, so it is guarded against path traversal.
_PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def is_valid_project_id(project_id: str) -> bool:
    """Return True if project_id is safe to use as a directory name."""
    return bool(_PROJECT_ID_PATTERN.match(project_id or ""))


def project_dir(project_id: str) -> Path:
    """Return the directory for a project, rejecting traversal attempts."""
    if not is_valid_project_id(project_id):
        raise ValueError(f"Invalid project_id: {project_id!r}")
    return config.PROJECTS_PATH / project_id


def project_config_path(project_id: str) -> Path:
    """Return the path to a project's project_config.json."""
    return project_dir(project_id) / config.PROJECT_CONFIG_FILENAME


def validate_config(cfg: dict) -> None:
    """Raise ValueError if a project config is missing any required field."""
    missing = [f for f in config.REQUIRED_PROJECT_FIELDS if not cfg.get(f)]
    if missing:
        raise ValueError(
            f"Project config is missing required field(s): {missing}"
        )


def get_project(project_id: str) -> Optional[dict]:
    """Read and return a project's config, or None if it does not exist."""
    if not is_valid_project_id(project_id):
        return None
    path = project_config_path(project_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to read project_config.json for %s: %s", project_id, e)
        return None


def list_projects() -> list[dict]:
    """Return all projects sorted by project_id, skipping malformed ones."""
    base = config.PROJECTS_PATH
    if not base.exists():
        return []
    projects: list[dict] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        cfg = get_project(entry.name)
        if cfg is not None:
            projects.append(cfg)
    projects.sort(key=lambda p: p.get("project_id", ""))
    return projects


def create_project(
    project_id: str,
    client_name: str,
    target_specialty: str,
    target_geography: list[str],
    icp_profile_id: str,
    created_by: str,
) -> dict:
    """
    Create a new project directory and write its project_config.json.

    Validates the project_id format, the referenced ICP profile, and the
    required fields before writing. Generic scoring/exclusion defaults are
    applied; no specialty-specific values are baked in.

    Raises:
        ValueError if the id is invalid, the project already exists, the ICP
        profile is missing/malformed, or a required field is empty.
    """
    if not is_valid_project_id(project_id):
        raise ValueError(
            f"Invalid project_id '{project_id}'. Use letters, digits, '-' or '_' "
            "(1-64 characters, no spaces)."
        )

    path = project_config_path(project_id)
    if path.exists():
        raise ValueError(f"Project '{project_id}' already exists.")

    # Referential integrity: a project must point at a real, loadable ICP profile.
    icp_profiles.load_profile(icp_profile_id)

    cfg = {
        "project_id": project_id,
        "client_name": client_name.strip(),
        "target_specialty": target_specialty.strip(),
        "target_geography": target_geography,
        "icp_profile_id": icp_profile_id,
        "bullseye_min_score": config.DEFAULT_BULLSEYE_MIN_SCORE,
        "active_exclusion_rules": list(config.DEFAULT_EXCLUSION_RULES),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": created_by,
    }
    validate_config(cfg)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    logger.info("Created project '%s' (ICP '%s') by %s", project_id, icp_profile_id, created_by)
    return cfg


def read_config_snapshot(run_directory: Path) -> Optional[dict]:
    """Read a run's project_config_snapshot.json, or None if absent/malformed."""
    path = run_directory / config.PROJECT_CONFIG_SNAPSHOT_FILENAME
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
