"""
preflight.py
System health checks: validates configuration and filesystem state.
Pure functions -- no side effects, no external network calls, no imports
from the pipeline repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple


class CheckResult(NamedTuple):
    """Outcome of a single preflight check."""

    check: str
    label: str
    status: str   # "ok" | "warn" | "error"
    message: str


def _check_anthropic_key(anthropic_api_key: str) -> CheckResult:
    """Verify ANTHROPIC_API_KEY is set (required for signal extraction)."""
    if anthropic_api_key:
        return CheckResult("anthropic_api_key", "Anthropic API Key", "ok", "Set")
    return CheckResult(
        "anthropic_api_key", "Anthropic API Key", "error",
        "Not set -- ANTHROPIC_API_KEY is required for signal extraction (Step 4)",
    )


def _check_pipeline_repo(pipeline_repo_path: Path, pipeline_script: str) -> CheckResult:
    """Verify the pipeline repo directory and entry-point script exist."""
    if not pipeline_repo_path or str(pipeline_repo_path) in ("", "."):
        return CheckResult(
            "pipeline_repo", "Pipeline Repo", "error",
            "PIPELINE_REPO_PATH is not configured",
        )
    if not pipeline_repo_path.is_dir():
        return CheckResult(
            "pipeline_repo", "Pipeline Repo", "error",
            f"Directory not found: {pipeline_repo_path}",
        )
    script = pipeline_repo_path / pipeline_script
    if not script.exists():
        return CheckResult(
            "pipeline_repo", "Pipeline Repo", "error",
            f"{pipeline_script} not found in {pipeline_repo_path}",
        )
    return CheckResult(
        "pipeline_repo", "Pipeline Repo", "ok", str(pipeline_repo_path)
    )


def _check_output_dir(output_runs_path: Path) -> CheckResult:
    """Verify the output/runs directory exists and is writable."""
    if not output_runs_path or str(output_runs_path) in ("", "."):
        return CheckResult(
            "output_dir", "Output Directory", "error",
            "OUTPUT_RUNS_PATH is not configured",
        )
    if not output_runs_path.exists():
        try:
            output_runs_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return CheckResult(
                "output_dir", "Output Directory", "error",
                f"Cannot create output directory: {exc}",
            )
    probe = output_runs_path / ".preflight_probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            "output_dir", "Output Directory", "error",
            f"Output directory is not writable: {exc}",
        )
    return CheckResult(
        "output_dir", "Output Directory", "ok", str(output_runs_path)
    )


def _check_icp_profiles(icp_profiles_path: Path) -> CheckResult:
    """Verify at least one ICP profile JSON is present."""
    if not icp_profiles_path.exists():
        return CheckResult(
            "icp_profiles", "ICP Profiles", "warn",
            "Profile directory not found -- create a profile before starting a run",
        )
    profiles = [p for p in icp_profiles_path.glob("*.json") if p.is_file()]
    if not profiles:
        return CheckResult(
            "icp_profiles", "ICP Profiles", "warn",
            "No .json profiles found -- create a profile before starting a run",
        )
    return CheckResult(
        "icp_profiles", "ICP Profiles", "ok",
        f"{len(profiles)} profile(s) loaded",
    )


def _check_projects(projects_path: Path) -> CheckResult:
    """Verify at least one project is configured."""
    if not projects_path.exists():
        return CheckResult(
            "projects", "Projects", "warn",
            "No projects configured -- create a project before starting a run",
        )
    configured = [
        d for d in projects_path.iterdir()
        if d.is_dir() and (d / "project_config.json").exists()
    ]
    if not configured:
        return CheckResult(
            "projects", "Projects", "warn",
            "No projects configured -- create a project before starting a run",
        )
    return CheckResult(
        "projects", "Projects", "ok",
        f"{len(configured)} project(s) configured",
    )


def _check_session_key(session_secret_key: str) -> CheckResult:
    """Verify SESSION_SECRET_KEY is set for production cookie security."""
    if session_secret_key:
        return CheckResult("session_key", "Session Secret Key", "ok", "Set")
    return CheckResult(
        "session_key", "Session Secret Key", "warn",
        "SESSION_SECRET_KEY not set -- session cookies are less secure without it",
    )


def run_checks(
    anthropic_api_key: str,
    pipeline_repo_path: Path,
    pipeline_script: str,
    output_runs_path: Path,
    icp_profiles_path: Path,
    projects_path: Path,
    session_secret_key: str,
) -> dict:
    """Run all preflight checks and return a summary dict.

    Returns {"status": "ok"|"warn"|"error", "checks": [...]}.
    Overall status is the worst individual check status.
    """
    checks = [
        _check_anthropic_key(anthropic_api_key),
        _check_pipeline_repo(pipeline_repo_path, pipeline_script),
        _check_output_dir(output_runs_path),
        _check_icp_profiles(icp_profiles_path),
        _check_projects(projects_path),
        _check_session_key(session_secret_key),
    ]

    if any(c.status == "error" for c in checks):
        overall = "error"
    elif any(c.status == "warn" for c in checks):
        overall = "warn"
    else:
        overall = "ok"

    return {
        "status": overall,
        "checks": [c._asdict() for c in checks],
    }
