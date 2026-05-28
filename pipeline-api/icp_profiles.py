"""
icp_profiles.py
ICP profile listing and loading from config.ICP_PROFILES_PATH.

ICP profiles are plain JSON files named {icp_id}.json that are dropped into the
profiles directory by an operator. There is no visual builder yet. Each profile
carries the signal checklist the pipeline enriches against (pipeline.py --icp).
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

# icp_id becomes a filename, so it is guarded against path traversal.
_ICP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def is_valid_icp_id(icp_id: str) -> bool:
    """Return True if icp_id is safe to use as a filename."""
    return bool(_ICP_ID_PATTERN.match(icp_id or ""))


def icp_profile_path(icp_id: str) -> Path:
    """Return the path to an ICP profile file, rejecting traversal attempts."""
    if not is_valid_icp_id(icp_id):
        raise ValueError(f"Invalid icp_id: {icp_id!r}")
    return config.ICP_PROFILES_PATH / f"{icp_id}.json"


def validate_profile(profile: dict) -> None:
    """Raise ValueError if an ICP profile is missing required fields."""
    missing = [f for f in config.REQUIRED_ICP_FIELDS if profile.get(f) in (None, "")]
    if missing:
        raise ValueError(f"ICP profile is missing required field(s): {missing}")
    signals = profile.get("signals")
    if not isinstance(signals, list) or not signals:
        raise ValueError("ICP profile 'signals' must be a non-empty list.")


def load_profile(icp_id: str) -> dict:
    """
    Read, parse, and validate a single ICP profile.

    Raises:
        ValueError if the id is invalid, the file is missing, the JSON is
        malformed, or required fields are absent.
    """
    if not is_valid_icp_id(icp_id):
        raise ValueError(f"Invalid icp_id '{icp_id}'.")
    path = icp_profile_path(icp_id)
    if not path.exists():
        raise ValueError(f"ICP profile '{icp_id}' not found in {config.ICP_PROFILES_PATH}.")
    try:
        with open(path, "r", encoding="utf-8") as f:
            profile = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"ICP profile '{icp_id}' is malformed JSON: {e}") from e
    validate_profile(profile)
    return profile


def list_profiles() -> list[dict]:
    """
    Return metadata for every loadable ICP profile, sorted by icp_id.

    Each entry: {icp_id, name, version, signal_count}. Malformed files are
    skipped so one bad file does not hide the rest.
    """
    base = config.ICP_PROFILES_PATH
    if not base.exists():
        return []
    profiles: list[dict] = []
    for entry in sorted(base.glob("*.json")):
        icp_id = entry.stem
        try:
            profile = load_profile(icp_id)
        except ValueError as e:
            logger.warning("Skipping unusable ICP profile '%s': %s", icp_id, e)
            continue
        profiles.append({
            "icp_id": profile["icp_id"],
            "name": profile["name"],
            "version": profile["version"],
            "signal_count": len(profile["signals"]),
        })
    return profiles


def read_snapshot(run_directory: Path) -> Optional[dict]:
    """Read a run's icp_snapshot.json, or None if absent/malformed."""
    path = run_directory / config.ICP_SNAPSHOT_FILENAME
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
