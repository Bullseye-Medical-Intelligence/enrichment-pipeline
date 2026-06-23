"""
icp_profiles.py
ICP profile listing and loading from config.ICP_PROFILES_PATH.

ICP profiles are plain JSON files named {icp_id}.json that are dropped into the
profiles directory by an operator. There is no visual builder yet. Each profile
carries the signal checklist the pipeline enriches against (pipeline.py --icp).
"""

import json
import logging
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

# Bundled seed profiles ship in the repo and are version-aware upserted into the
# runtime store (config.ICP_PROFILES_PATH). The same per-file logic runs at
# startup (sync_all_seed_profiles) and on demand before a run / re-crawl
# (sync_seed_profile), so a committed ICP change with a bumped version reaches the
# running app WITHOUT requiring a restart.
_SEEDS_DIR = Path(__file__).parent / "seeds" / "icp_profiles"

# Atomic-replace retry budget. On Windows, os.replace (MoveFileEx) cannot
# overwrite a read-only destination and raises PermissionError if the target is
# briefly locked by another process (antivirus / search indexer). We clear the
# read-only bit and retry with a short backoff before giving up.
_REPLACE_RETRIES = 5
_REPLACE_BACKOFF_SECONDS = 0.1

# icp_id becomes a filename, so it is guarded against path traversal.
_ICP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_REQUIRED_SIGNAL_FIELDS = ("signal_id", "signal_label", "prompt_instruction", "positive_weight")

# Legacy tier label aliases — same map used by the pipeline's exclusion_checker.
# Normalised before validation so existing ICP files with old names keep working.
_TIER_ALIASES: dict[str, str] = {"Watchlist": "Contender"}
_VALID_CAP_FLOOR_TIERS = ("Contender", "Needs Verification")
# Max length of an optional per-signal `column_label` (surfaces the signal as an
# at-a-glance dashboard column). Kept short so table headers stay compact.
_MAX_COLUMN_LABEL_LEN = 24


def _normalize_tier_value(value: str) -> str:
    """Return the canonical tier label for a possibly-legacy tier string."""
    return _TIER_ALIASES.get(value, value)


def _normalize_hypothesis(hypothesis: dict) -> dict:
    """Normalize hypothesis fields so all values are plain strings.

    The AI builder sometimes stores common_objections as a structured dict
    ({objection_1: {objection, response}, ...}). Convert it to a formatted
    text string so all display paths (profiles list, edit textarea, demo
    brief) work without per-template type checks.
    """
    if not isinstance(hypothesis, dict):
        return hypothesis
    co = hypothesis.get("common_objections")
    if isinstance(co, dict):
        parts = []
        for obj in co.values():
            if isinstance(obj, dict):
                q = obj.get("objection", "")
                r = obj.get("response", "")
                if q:
                    parts.append(f"Q: {q}\nA: {r}" if r else f"Q: {q}")
        hypothesis = {**hypothesis, "common_objections": "\n\n".join(parts)}
    return hypothesis


def is_valid_icp_id(icp_id: str) -> bool:
    """Return True if icp_id is safe to use as a filename."""
    return bool(_ICP_ID_PATTERN.match(icp_id or ""))


def icp_profile_path(icp_profile_id: str) -> Path:
    """Return the path to an ICP profile file, rejecting traversal attempts."""
    if not is_valid_icp_id(icp_profile_id):
        raise ValueError(f"Invalid icp_id: {icp_profile_id!r}")
    return config.ICP_PROFILES_PATH / f"{icp_profile_id}.json"


def _profile_version(path: Path) -> str:
    """Return the 'version' string of an ICP profile JSON, or '' if unreadable."""
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("version", ""))
    except Exception:
        return ""


def sync_seed_profile(icp_profile_id: str) -> bool:
    """Upsert one bundled seed into the runtime store if it changed; return True if copied.

    Version-aware and single-file: copies the seed for icp_profile_id into
    config.ICP_PROFILES_PATH when the destination is missing or the bundled seed's
    `version` differs from the installed copy's. This lets a committed ICP change
    (with a bumped version) take effect on the next run / re-crawl without waiting
    for an app restart. Operator edits that keep the same version are preserved
    (equal versions skip the copy); a profile saved under its own icp_id with no
    matching seed file is never touched.
    """
    if not is_valid_icp_id(icp_profile_id):
        return False
    seed = _SEEDS_DIR / f"{icp_profile_id}.json"
    if not seed.exists():
        return False
    dest = config.ICP_PROFILES_PATH / f"{icp_profile_id}.json"
    if dest.exists() and _profile_version(dest) == _profile_version(seed):
        return False
    config.ICP_PROFILES_PATH.mkdir(parents=True, exist_ok=True)
    shutil.copy2(seed, dest)
    logger.info("Synced ICP profile from seed: %s (version %s)", seed.name, _profile_version(seed))
    return True


def sync_all_seed_profiles() -> int:
    """Version-aware upsert of every bundled seed into the runtime store.

    Returns the number of profiles copied. Called at startup; the same per-file
    logic also runs on demand (sync_seed_profile) before a run or re-crawl.
    """
    if not _SEEDS_DIR.exists():
        return 0
    return sum(1 for seed in sorted(_SEEDS_DIR.glob("*.json")) if sync_seed_profile(seed.stem))


def validate_icp_profile(data: dict) -> None:
    """Raise ValueError if an ICP profile is missing required fields.

    Checks the top-level fields plus the shape of every signal.
    """
    for field in config.REQUIRED_ICP_FIELDS:
        if data.get(field) in (None, ""):
            raise ValueError(f"ICP profile is missing required field: {field}")
    signals = data.get("signals")
    if not isinstance(signals, list) or not signals:
        raise ValueError("ICP profile 'signals' must be a non-empty list.")
    signal_ids = {s.get("signal_id") for s in signals if isinstance(s, dict)}
    # Duplicate signal_ids silently shadow each other downstream — reject them
    # here so the API never accepts a profile the pipeline subprocess would.
    seen_ids: dict[str, int] = {}
    for i, signal in enumerate(signals):
        if isinstance(signal, dict):
            sid = signal.get("signal_id")
            if isinstance(sid, str) and sid:
                if sid in seen_ids:
                    raise ValueError(
                        f"ICP profile has duplicate signal_id '{sid}' "
                        f"(signals #{seen_ids[sid]} and #{i + 1})."
                    )
                seen_ids[sid] = i + 1
    for i, signal in enumerate(signals):
        if not isinstance(signal, dict):
            raise ValueError(f"ICP signal #{i + 1} must be an object.")
        for field in _REQUIRED_SIGNAL_FIELDS:
            if field not in signal:
                raise ValueError(f"ICP signal #{i + 1} is missing '{field}'.")
        # source_type is not implemented in the pipeline — reject it here so a
        # profile the subprocess would refuse never passes API validation.
        if "source_type" in signal:
            st = signal["source_type"]
            if st == "static_lookup":
                raise ValueError(
                    f"ICP signal #{i + 1} ({signal.get('signal_id', '?')}) has "
                    f"source_type='static_lookup', which is not implemented. "
                    f"Remove source_type before saving."
                )
            raise ValueError(
                f"ICP signal #{i + 1} ({signal.get('signal_id', '?')}) has "
                f"unsupported source_type='{st}'. No signal source_types are "
                f"currently implemented; remove the field."
            )
        if not signal.get("prompt_instruction"):
            raise ValueError(
                f"ICP signal #{i + 1} ({signal.get('signal_id', '?')}) "
                f"'prompt_instruction' must be a non-empty string."
            )
        if not isinstance(signal["positive_weight"], (int, float)) or isinstance(
            signal["positive_weight"], bool
        ):
            raise ValueError(f"ICP signal #{i + 1} 'positive_weight' must be numeric.")
        # Optional tiering fields — validate only when present.
        if "not_found_weight" in signal and (
            not isinstance(signal["not_found_weight"], (int, float))
            or isinstance(signal["not_found_weight"], bool)
        ):
            raise ValueError(f"ICP signal #{i + 1} 'not_found_weight' must be numeric.")
        if "no_weight" in signal and (
            not isinstance(signal["no_weight"], (int, float))
            or isinstance(signal["no_weight"], bool)
        ):
            raise ValueError(f"ICP signal #{i + 1} 'no_weight' must be numeric.")
        if "verification_required" in signal and not isinstance(
            signal["verification_required"], bool
        ):
            raise ValueError(
                f"ICP signal #{i + 1} 'verification_required' must be true or false."
            )
        if "required_for_bullseye" in signal and not isinstance(
            signal["required_for_bullseye"], bool
        ):
            raise ValueError(
                f"ICP signal #{i + 1} 'required_for_bullseye' must be true or false."
            )
        if "required_for_contender" in signal and not isinstance(
            signal["required_for_contender"], bool
        ):
            raise ValueError(
                f"ICP signal #{i + 1} 'required_for_contender' must be true or false."
            )
        if "cap_tier" in signal:
            canonical = _normalize_tier_value(signal["cap_tier"])
            if canonical not in _VALID_CAP_FLOOR_TIERS:
                raise ValueError(
                    f"ICP signal #{i + 1} 'cap_tier' must be 'Contender' or 'Needs Verification'."
                )
        if "floor_tier" in signal:
            canonical = _normalize_tier_value(signal["floor_tier"])
            if canonical not in _VALID_CAP_FLOOR_TIERS:
                raise ValueError(
                    f"ICP signal #{i + 1} 'floor_tier' must be 'Contender' or 'Needs Verification'."
                )
        if "exclude_if_yes" in signal and not isinstance(signal["exclude_if_yes"], bool):
            raise ValueError(
                f"ICP signal #{i + 1} 'exclude_if_yes' must be true or false."
            )
        if "reinforcer" in signal and not isinstance(signal["reinforcer"], bool):
            raise ValueError(
                f"ICP signal #{i + 1} 'reinforcer' must be true or false."
            )
        if "reinforces" in signal:
            if not isinstance(signal["reinforces"], str) or not signal["reinforces"]:
                raise ValueError(
                    f"ICP signal #{i + 1} 'reinforces' must be a non-empty signal_id string."
                )
            if signal["reinforces"] not in signal_ids:
                raise ValueError(
                    f"ICP signal #{i + 1} 'reinforces' references unknown signal_id "
                    f"'{signal['reinforces']}'."
                )
        if "inhibited_by" in signal:
            if not isinstance(signal["inhibited_by"], str) or not signal["inhibited_by"]:
                raise ValueError(
                    f"ICP signal #{i + 1} 'inhibited_by' must be a non-empty signal_id string."
                )
            if signal["inhibited_by"] not in signal_ids:
                raise ValueError(
                    f"ICP signal #{i + 1} 'inhibited_by' references unknown signal_id "
                    f"'{signal['inhibited_by']}'."
                )
        if "column_label" in signal:
            label = signal["column_label"]
            if not isinstance(label, str) or not label.strip():
                raise ValueError(
                    f"ICP signal #{i + 1} 'column_label' must be a non-empty string."
                )
            if len(label) > _MAX_COLUMN_LABEL_LEN:
                raise ValueError(
                    f"ICP signal #{i + 1} 'column_label' must be "
                    f"{_MAX_COLUMN_LABEL_LEN} characters or fewer."
                )
    # Profile-level optional fields.
    if "contact_strategy" in data and not isinstance(data.get("contact_strategy"), str):
        raise ValueError("ICP profile 'contact_strategy' must be a string.")


def get_icp_profile(icp_profile_id: str) -> dict:
    """
    Read, parse, and validate a single ICP profile.

    Raises:
        ValueError if the id is invalid, the file is missing, the JSON is
        malformed, or required fields are absent.
    """
    if not is_valid_icp_id(icp_profile_id):
        raise ValueError(f"Invalid icp_id '{icp_profile_id}'.")
    path = icp_profile_path(icp_profile_id)
    if not path.exists():
        raise ValueError(f"ICP profile '{icp_profile_id}' not found in {config.ICP_PROFILES_PATH}.")
    try:
        with open(path, "r", encoding="utf-8") as f:
            profile = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"ICP profile '{icp_profile_id}' is malformed JSON: {e}") from e
    # Normalize legacy tier aliases before validation so old files with e.g.
    # cap_tier: "Watchlist" are transparently upgraded without requiring a file edit.
    for sig in profile.get("signals") or []:
        for field in ("cap_tier", "floor_tier"):
            if field in sig:
                sig[field] = _normalize_tier_value(sig[field])
    # Demo-account tiers are operator-facing in the ICP brief; upgrade legacy
    # "Watchlist" labels so a profile saved before the rename renders the correct
    # tier name and styling instead of an undefined badge class.
    for acct in profile.get("demo_accounts") or []:
        if isinstance(acct, dict) and "tier" in acct:
            acct["tier"] = _normalize_tier_value(acct["tier"])
    if "hypothesis" in profile:
        profile["hypothesis"] = _normalize_hypothesis(profile["hypothesis"])
    validate_icp_profile(profile)
    return profile


def list_icp_profiles() -> list[dict]:
    """
    Return metadata for every loadable ICP profile, sorted by icp_id.

    Each entry: {icp_id, name, version, description, hypothesis, demo_accounts,
    source_urls, signal_count, signals}. Malformed files are skipped so one bad
    file does not hide the rest.
    """
    base = config.ICP_PROFILES_PATH
    if not base.exists():
        return []
    profiles: list[dict] = []
    for entry in sorted(base.glob("*.json")):
        icp_id = entry.stem
        try:
            profile = get_icp_profile(icp_id)
        except ValueError as e:
            logger.warning("Skipping unusable ICP profile '%s': %s", icp_id, e)
            continue
        profiles.append({
            "icp_id": profile["icp_id"],
            "name": profile["name"],
            "version": profile["version"],
            "description": profile.get("description", ""),
            "hypothesis": profile.get("hypothesis"),
            "demo_accounts": profile.get("demo_accounts", []),
            "source_urls": profile.get("source_urls", {}),
            "signal_count": len(profile["signals"]),
            "signals": profile["signals"],
            "default_specialty": profile.get("default_specialty", ""),
            "default_geography": profile.get("default_geography", []),
            "default_exclusion_rules": profile.get("default_exclusion_rules", []),
        })
    return profiles


def _replace_atomic(tmp: Path, dest: Path) -> None:
    """Rename tmp onto dest atomically, hardened for Windows.

    Unix rename overwrites any destination. Windows MoveFileEx refuses a
    read-only destination (a seeded profile copied by shutil.copy2 inherits the
    repo file's read-only bit) and raises PermissionError on a transient lock.
    Clear the read-only attribute and retry with a short backoff before failing.
    """
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if dest.exists():
                try:
                    os.chmod(dest, stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS)


def delete_icp_profile(icp_id: str) -> None:
    """Delete an ICP profile file from ICP_PROFILES_PATH.

    Raises ValueError on invalid icp_id or if the file does not exist.
    """
    path = icp_profile_path(icp_id)
    if not path.exists():
        raise ValueError(f"ICP profile '{icp_id}' not found.")
    path.unlink()


def save_icp_profile(data: dict, overwrite: bool = False) -> None:
    """
    Validate and write an ICP profile to ICP_PROFILES_PATH/{icp_id}.json.

    Uses atomic temp-file + os.replace() so a crash mid-write cannot produce a
    partial file. Raises ValueError on invalid data. A new profile must not clash
    with an existing id; pass overwrite=True (the Edit flow) to update one in place.
    """
    validate_icp_profile(data)
    icp_id = data["icp_id"]
    if not is_valid_icp_id(icp_id):
        raise ValueError(f"Invalid icp_id: {icp_id!r}")
    base = config.ICP_PROFILES_PATH
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{icp_id}.json"
    if path.exists() and not overwrite:
        raise ValueError(f"A profile with id '{icp_id}' already exists. Choose a different ID.")
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _replace_atomic(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


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
