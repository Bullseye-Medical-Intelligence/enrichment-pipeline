"""
projects.py
Project configuration storage: create, read, update, list, and validate projects.

A project owns a single project_config.json that doubles as the pipeline's
run config (pipeline.py --config) and names the ICP profile to enrich against.
All project state lives on the filesystem under config.PROJECTS_PATH. No DB.
"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
import icp_profiles

logger = logging.getLogger(__name__)

# project_id becomes a directory name, so it is guarded against path traversal
# and constrained to filesystem-safe, lowercase characters.
_PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def is_valid_project_id(project_id: str) -> bool:
    """Return True if project_id is filesystem-safe (lowercase, digits, -, _)."""
    return bool(_PROJECT_ID_PATTERN.match(project_id or ""))


def project_dir(project_id: str) -> Path:
    """Return the directory for a project, rejecting traversal attempts."""
    if not is_valid_project_id(project_id):
        raise ValueError(f"Invalid project_id: {project_id!r}")
    return config.PROJECTS_PATH / project_id


def project_config_path(project_id: str) -> Path:
    """Return the path to a project's project_config.json."""
    return project_dir(project_id) / config.PROJECT_CONFIG_FILENAME


def suppression_list_path(project_id: str) -> Path:
    """Return the path to a project's customer suppression CSV (may not exist)."""
    return project_dir(project_id) / config.SUPPRESSION_LIST_FILENAME


def suppression_list_row_count(project_id: str) -> int:
    """Return the number of data rows in the suppression list, or 0 if absent."""
    path = suppression_list_path(project_id)
    if not path.exists():
        return 0
    try:
        import csv as _csv
        with open(path, newline="", encoding="utf-8-sig") as f:
            return max(0, sum(1 for _ in _csv.reader(f)) - 1)
    except Exception:
        return 0


def default_project_config() -> dict:
    """Return generic scoring/crawl defaults for a new project.

    No specialty- or client-specific values: callers supply those fields.
    """
    return {
        "client_website": "",
        "product_name": "",
        "target_geography": [],
        "active_exclusion_rules": list(config.DEFAULT_EXCLUSION_RULES),
        "bullseye_min_score": config.DEFAULT_BULLSEYE_MIN_SCORE,
        "verify_near_miss_band": config.DEFAULT_NEAR_MISS_BAND,
        "max_pages_per_practice": config.DEFAULT_MAX_PAGES_PER_PRACTICE,
        "request_timeout_seconds": config.DEFAULT_REQUEST_TIMEOUT_SECONDS,
        "request_retries": config.DEFAULT_REQUEST_RETRIES,
        "io_concurrency": config.DEFAULT_IO_CONCURRENCY,
        "llm_concurrency": config.DEFAULT_LLM_CONCURRENCY,
        "subpage_keywords": list(config.DEFAULT_SUBPAGE_KEYWORDS),
        "notes": "",
    }


def validate_project_config(data: dict) -> None:
    """Raise ValueError if a project config is invalid.

    Enforces required fields, the filesystem-safe project_id, list-typed fields,
    and integer ranges for the pipeline tuning parameters.
    """
    if not is_valid_project_id(data.get("project_id", "")):
        raise ValueError(
            "project_id is required and must be lowercase letters, digits, "
            "hyphens or underscores (1-64 characters, no spaces)."
        )
    for field in ("client_name", "target_specialty", "icp_profile_id"):
        if not str(data.get(field, "")).strip():
            raise ValueError(f"{field} is required and cannot be empty.")

    geo = data.get("target_geography")
    if not isinstance(geo, list):
        raise ValueError("target_geography must be a list.")
    bad_geo = [g for g in geo if not isinstance(g, str) or len(g) != 2 or not g.isupper()]
    if bad_geo:
        raise ValueError(
            f"target_geography contains invalid state code(s): {sorted(bad_geo)}. "
            "Each entry must be a 2-letter uppercase US state code."
        )

    rules = data.get("active_exclusion_rules")
    if not isinstance(rules, list):
        raise ValueError("active_exclusion_rules must be a list.")
    unknown_rules = sorted(set(rules) - config.ALL_KNOWN_EXCLUSION_RULE_NAMES)
    if unknown_rules:
        raise ValueError(
            f"active_exclusion_rules contains unrecognized rule name(s): {unknown_rules}. "
            f"Known rules: {sorted(config.ALL_KNOWN_EXCLUSION_RULE_NAMES)}."
        )

    keywords = data.get("subpage_keywords")
    if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
        raise ValueError("subpage_keywords must be a list of strings.")

    _validate_int(data, "bullseye_min_score", minimum=0, maximum=100)
    _validate_int(data, "verify_near_miss_band", minimum=0, optional=True)
    _validate_int(data, "max_pages_per_practice", minimum=1)
    _validate_int(data, "request_timeout_seconds", minimum=1)
    _validate_int(data, "request_retries", minimum=0)
    _validate_int(data, "io_concurrency", minimum=1)
    _validate_int(data, "llm_concurrency", minimum=1, optional=True)


def _validate_int(
    data: dict,
    field: str,
    minimum: int,
    maximum: Optional[int] = None,
    *,
    optional: bool = False,
) -> None:
    """Raise ValueError if a config field is not an int within [minimum, maximum].

    When optional=True the check is skipped if the field is absent from data.
    Use this for fields that have a default value and may be missing in configs
    created before the field was added.
    """
    if field not in data:
        if optional:
            return
        raise ValueError(f"{field} is required and must be an integer.")
    value = data[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer.")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}-{maximum}" if maximum is not None else f">= {minimum}"
        raise ValueError(f"{field} must be {bound}.")


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


def create_project(project_data: dict) -> dict:
    """
    Create a new project directory and write its project_config.json.

    Merges the supplied data over generic defaults, validates it, confirms the
    referenced ICP profile is loadable, and rejects duplicates.

    Raises:
        ValueError if the config is invalid, the project already exists, or the
        referenced ICP profile is missing/malformed.
    """
    cfg = {**default_project_config(), **{k: v for k, v in project_data.items() if v is not None}}
    cfg.setdefault("created_at", datetime.now(timezone.utc).isoformat())

    validate_project_config(cfg)

    path = project_config_path(cfg["project_id"])
    if path.exists():
        raise ValueError(f"Project '{cfg['project_id']}' already exists.")

    # Referential integrity: a project must point at a real, loadable ICP profile.
    icp_profiles.get_icp_profile(cfg["icp_profile_id"])

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_config(path, cfg)
    logger.info(
        "Created project '%s' (ICP '%s') by %s",
        cfg["project_id"], cfg["icp_profile_id"], cfg.get("created_by", "unknown"),
    )
    return cfg


def update_project(project_id: str, project_data: dict) -> dict:
    """
    Overwrite an existing project's config with merged, validated data.

    Raises:
        ValueError if the project does not exist, the merged config is invalid,
        or the referenced ICP profile is missing.
    """
    existing = get_project(project_id)
    if existing is None:
        raise ValueError(f"Project '{project_id}' does not exist.")

    cfg = {**existing, **{k: v for k, v in project_data.items() if v is not None}}
    cfg["project_id"] = project_id  # id is immutable; it is the folder name
    cfg["updated_at"] = datetime.now(timezone.utc).isoformat()

    validate_project_config(cfg)
    icp_profiles.get_icp_profile(cfg["icp_profile_id"])

    _write_config(project_config_path(project_id), cfg)
    logger.info("Updated project '%s'", project_id)
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


def _write_config(path: Path, cfg: dict) -> None:
    """Write a project_config.json atomically (temp file + os.replace).

    A crash mid-write must never leave a truncated config — it doubles as the
    pipeline's run config and a corrupt file would break every run for the project.
    """
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
