"""
schema.py
Pydantic models for all request and response types.
Every external-facing data structure is defined here.
"""

from typing import Optional

from pydantic import BaseModel, field_validator


class RunStatus(BaseModel):
    """Full content of a run's status.json file.

    Project/ICP metadata fields are optional with defaults so status.json files
    written before the project layer existed still load.
    """

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
    needs_verification_count: int = 0
    contender_count: int = 0
    manual_review_count: int = 0
    excluded_count: int = 0
    error_count: int = 0
    pipeline_version: str = "v1.0"
    error_summary: str = ""
    # Project / ICP context (snapshotted at run creation)
    client_name: Optional[str] = None
    product_name: Optional[str] = None
    target_specialty: Optional[str] = None
    target_geography: list[str] = []
    icp_profile_id: Optional[str] = None
    icp_profile_name: Optional[str] = None
    icp_profile_version: Optional[str] = None
    archived: bool = False
    # LLM usage totals (copied from run_log.json on completion). None means
    # the run predates token capture — display "not captured", never zero.
    llm_input_tokens: Optional[int] = None
    llm_output_tokens: Optional[int] = None
    llm_call_count: Optional[int] = None
    # Run kind. "enrichment" for normal runs; discovery runs write "discovery"
    # in their own status.json shape. Default keeps pre-existing runs as enrichment.
    run_type: str = "enrichment"
    # Discovery → enrichment traceability (set when a run is created from a
    # discovery run's selected records). None for runs created from a raw upload.
    source_discovery_run_id: Optional[str] = None
    source_discovery_selection_count: Optional[int] = None
    source_discovery_selection_mode: Optional[str] = None
    # Explicit registry update (set by POST /enrichment-runs/{id}/update-registry).
    # None until an operator has pushed this run's records into the registry.
    registry_updated_at: Optional[str] = None
    registry_update_count: Optional[int] = None
    registry_update_log_path: Optional[str] = None


class RunSummary(BaseModel):
    """Compact run representation returned by GET /runs."""

    run_id: str
    status: str
    source_type: str
    records_input: int
    bullseye_count: int
    contender_count: int
    manual_review_count: int = 0
    excluded_count: int
    error_count: int
    created_at: str
    completed_at: Optional[str] = None
    project_id: Optional[str] = None
    client_name: Optional[str] = None
    icp_profile_id: Optional[str] = None
    error_summary: str = ""
    archived: bool = False


class RunListResponse(BaseModel):
    """Response body for GET /runs."""

    runs: list[RunSummary]
    total: int


class RunCreateResponse(BaseModel):
    """Response body for POST /runs."""

    run_id: str
    status: str


class DiscoveryRunSummary(BaseModel):
    """Summary of a discovery run, returned by the /discovery-runs endpoints.

    A discovery run compares an uploaded Outscraper CSV against the master
    practice registry; it performs no enrichment, scoring, or LLM work. Counts
    are per-classification (new / changed / known / possible duplicate /
    insufficient data). output_paths maps each artifact name to its filename
    inside the run directory.
    """

    run_id: str
    run_type: str = "discovery"
    status: str  # complete | failed
    source_type: str = "outscraper"
    created_at: str
    completed_at: Optional[str] = None
    operator: str = ""
    input_filename: str = ""
    total_imported: int = 0
    new_count: int = 0
    changed_count: int = 0
    known_count: int = 0
    possible_duplicate_count: int = 0
    insufficient_data_count: int = 0
    output_paths: dict[str, str] = {}
    error_summary: str = ""


class ErrorResponse(BaseModel):
    """Standard error response body."""

    detail: str


class ValidationFailure(BaseModel):
    """400 response when pre-flight validation fails."""

    detail: str


VALID_OVERRIDE_TIERS: frozenset[str] = frozenset(
    {"Bullseye", "Needs Verification", "Contender", "Excluded"}
)
VALID_QC_STATUSES: frozenset[str] = frozenset({"pending", "approved", "rejected"})


class ReviewEdit(BaseModel):
    """Analyst review edit submitted from the results dashboard."""

    analyst_note: str = ""
    override_tier: Optional[str] = None
    override_reason: Optional[str] = None
    qc_status: str = "pending"
    reviewed_by: Optional[str] = None
    extra_sales_angles: list[str] = []


# The three states a signal can hold, mirroring the pipeline's signal_state
# vocabulary. An operator override must resolve to one of these.
VALID_SIGNAL_STATES: frozenset[str] = frozenset({"yes", "no", "not_found"})


class SignalOverride(BaseModel):
    """Operator override of a single signal's state on one record.

    Persisted in the reviews.json overlay only — never written to
    enriched_targets.json. An override changes the displayed signal state and
    its evidence; it never recomputes scores or tiers (those stay owned by the
    pipeline). source_url is the operator-supplied evidence link and is required.
    """

    signal_id: str
    override_state: str  # yes | no | not_found
    source_url: str
    override_note: str = ""
    override_by: str = ""
    override_at: str = ""  # ISO 8601 UTC; stamped server-side at save time

    @field_validator("signal_id")
    @classmethod
    def _signal_id_non_empty(cls, v: str) -> str:
        """signal_id must name a signal on the record."""
        if not (v or "").strip():
            raise ValueError("signal_id is required and cannot be empty")
        return v.strip()

    @field_validator("override_state")
    @classmethod
    def _state_is_valid(cls, v: str) -> str:
        """override_state must be one of yes / no / not_found."""
        if v not in VALID_SIGNAL_STATES:
            raise ValueError(
                f"Invalid override_state '{v}'. "
                f"Must be one of: {sorted(VALID_SIGNAL_STATES)}"
            )
        return v

    @field_validator("source_url")
    @classmethod
    def _source_url_non_empty(cls, v: str) -> str:
        """An override must carry an operator-supplied evidence link."""
        if not (v or "").strip():
            raise ValueError("source_url is required and cannot be empty")
        return v.strip()
