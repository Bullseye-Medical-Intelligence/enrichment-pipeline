"""
schema.py
Pydantic models for all request and response types.
Every external-facing data structure is defined here.
"""

from typing import Optional

from pydantic import BaseModel


class RunStatus(BaseModel):
    """Full content of a run's status.json file."""

    run_id: str
    project_id: str
    source_type: str
    input_filename: str
    status: str  # pending | running | complete | failed
    created_at: str
    completed_at: Optional[str] = None
    operator: str
    output_path: Optional[str] = None
    records_input: int = 0
    records_output: int = 0
    bullseye_count: int = 0
    watchlist_count: int = 0
    excluded_count: int = 0
    error_count: int = 0
    pipeline_version: str = "v1.0"
    error_summary: str = ""


class RunSummary(BaseModel):
    """Compact run representation returned by GET /runs."""

    run_id: str
    status: str
    source_type: str
    records_input: int
    bullseye_count: int
    watchlist_count: int
    excluded_count: int
    error_count: int
    created_at: str
    completed_at: Optional[str] = None


class RunListResponse(BaseModel):
    """Response body for GET /runs."""

    runs: list[RunSummary]
    total: int


class RunCreateResponse(BaseModel):
    """Response body for POST /runs."""

    run_id: str
    status: str


class ErrorResponse(BaseModel):
    """Standard error response body."""

    detail: str


class ValidationFailure(BaseModel):
    """400 response when pre-flight validation fails."""

    detail: str


VALID_OVERRIDE_TIERS: frozenset[str] = frozenset(
    {"Bullseye", "Strong", "Warm", "Cold", "Excluded"}
)
VALID_QC_STATUSES: frozenset[str] = frozenset({"pending", "approved", "rejected"})


class ReviewEdit(BaseModel):
    """Analyst review edit submitted from the results dashboard."""

    analyst_note: str = ""
    override_tier: Optional[str] = None
    override_reason: Optional[str] = None
    qc_status: str = "pending"
    reviewed_by: Optional[str] = None
