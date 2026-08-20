"""
exports.py
Filtered CSV export logic for completed runs.
Reads enriched_targets.json + reviews.json, applies review overlay, and
streams a filtered CSV. Never writes to disk — returns BytesIO.
"""

import csv
import io
import json
import logging
from pathlib import Path
from typing import Callable

import record_adapter
import reviews

logger = logging.getLogger(__name__)

# Rep-facing columns derived from nested pipeline output (objects and lists are
# not picked up by the scalar-field column scan, so each is flattened here).
# `providers` rolls the practice's providers up into ONE cell — consolidation
# made the practice location the row, and a provider must never reappear as an
# extra row. `location_label` renders the Pass 2 group membership as
# "Location 3 of 6" so a multi-location group reads as a group.
_BRIEF_COLUMNS = ["why_contact", "providers_flat", "provider_count", "location_label"]

# Flattened evidence columns, so a CRM import carries the PROOF and not just the
# verdict. Signals are a list, and the row builder blanks every list-valued
# field, so before this the entire evidence layer — the verbatim quote, the page
# it came from, the day it was captured — reached a client only as rendered HTML.
# Three is a deliberate cap: enough to justify the tier in a CRM record detail
# view, few enough that the header stays importable.
EVIDENCE_COLUMN_SLOTS = 3

# Cap on a quote cell. Salesforce and HubSpot text fields truncate silently on
# import, which would corrupt a verbatim quote without saying so — better to cut
# it here, visibly, on a word boundary with an ellipsis.
MAX_QUOTE_CHARS = 250

_EVIDENCE_COLUMNS = [
    f"signal_{n}_{suffix}"
    for n in range(1, EVIDENCE_COLUMN_SLOTS + 1)
    for suffix in ("id", "claim", "quote", "source_url", "captured")
]

# Internal matching artifacts and raw group keys: useful to an operator
# reconciling a merge, meaningless in a client deliverable.
_CONSOLIDATION_INTERNAL_COLUMNS = {
    "address_street_normalized",
    "address_unit_normalized",
    "group_id",
    "location_index",
    "location_count",
    # How the display name was chosen ("placeholder", "domain", "npi_organization").
    # An audit trail for an operator reconciling a merge; a client reading
    # "placeholder" beside a practice name learns only that we guessed.
    "practice_name_source",
}

# Internal-only columns stripped from every client-facing CSV export.
# Numeric scores: qualitative tier + confidence_band are sufficient for CRM import.
# Pipeline internals: llm_model_used, llm_prompt_version, raw_input are not
# meaningful to the sales team and should never appear in client deliverables.
_HIDDEN_SCORE_COLUMNS = {
    "bullseye_score",
    "fit_signal_score",
    "confidence_score",
    "fit_confidence_status",
    "llm_model_used",
    "llm_prompt_version",
    "raw_input",
}

# Review-overlay columns appended to every OPERATOR export row.
_REVIEW_COLUMNS = [
    "displayed_tier",
    "qc_status",
    "analyst_note",
    "override_tier",
    "override_reason",
    "reviewed_by",
    "reviewed_at",
]

# Client CSVs carry only the final tier from the review overlay — QC status,
# analyst notes, override rationale, and reviewer identity are internal and must
# never ship in a client deliverable.
_CLIENT_REVIEW_COLUMNS = ["displayed_tier"]

# Record-level scalar fields removed from CLIENT-facing CSVs on top of the score
# columns above: free-text internal notes must never reach a client file.
_CLIENT_HIDDEN_COLUMNS = {"internal_notes"} | _CONSOLIDATION_INTERNAL_COLUMNS


def _consolidation_cells(record: dict) -> dict:
    """Flatten a practice location's providers and group membership into cells.

    One row per practice location is the contract, so every provider the merge
    absorbed is rendered into a single rolled-up cell. Formatting only — the
    engine decided who merged and which group a location belongs to.
    """
    providers = record.get("providers") or []
    parts = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        name = (provider.get("name") or "").strip()
        if not name:
            continue
        credentials = ", ".join(provider.get("credentials") or [])
        parts.append(f"{name}, {credentials}" if credentials else name)
    # Fall back to the crawl-derived names when a run predates consolidation.
    if not parts:
        parts = [str(n) for n in (record.get("provider_names") or []) if str(n).strip()]

    location_count = record.get("location_count") or 0
    location_index = record.get("location_index") or 0
    label = ""
    if record.get("group_id") and location_count > 1 and location_index:
        label = f"Location {location_index} of {location_count}"

    return {
        "providers_flat": " | ".join(parts),
        "provider_count": record.get("provider_count") or len(parts),
        "location_label": label,
    }


def _escape_csv_cell(value):
    """Neutralize spreadsheet formula injection in a cell value.

    A cell beginning with =, +, -, @, tab, or CR is executed as a formula when
    the CSV is opened in Excel or Google Sheets. Practice names and LLM-derived
    fields originate from untrusted sources (Outscraper exports, crawled sites),
    so any such string cell is prefixed with a single quote to force text.
    Non-string values pass through unchanged.
    """
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _territory_sorted(records: list[dict]) -> list[dict]:
    """Order rows for territory grouping: state, then city, then ZIP.

    This is GROUPING, not routing. It puts a rep's accounts for one town on
    consecutive rows so a day can be planned by reading down the sheet. It makes
    no claim about proximity or drive order, and it must not be described as
    route optimization: the location data is ZIP/city/state with no coordinates,
    so two adjacent rows may be a mile apart or an hour apart and nothing here
    can tell the difference.

    Practice name breaks ties so the order is total and an export is byte-stable
    across runs. Rows missing a state sort last rather than interleaving.
    """
    def key(record: dict):
        state = (record.get("address_state") or "").strip().upper()
        city = (record.get("address_city") or "").strip().upper()
        zip_code = (record.get("address_zip") or "").strip()
        return (not state, state, city, zip_code,
                (record.get("practice_name") or "").strip().upper())

    return sorted(records, key=key)


def _truncate_quote(text: str) -> str:
    """Trim a verbatim quote to MAX_QUOTE_CHARS on a word boundary.

    Truncation is marked with an ellipsis so a reader can tell a shortened quote
    from a complete one — an unmarked cut would misrepresent the practice as
    having said only that much.
    """
    text = " ".join((text or "").split())
    if len(text) <= MAX_QUOTE_CHARS:
        return text
    cut = text[:MAX_QUOTE_CHARS]
    spaced = cut.rsplit(" ", 1)[0]
    return (spaced or cut).rstrip(",;:.") + "…"


def evidence_cells(record: dict) -> dict:
    """Flatten a record's top confirmed signals into evidence columns.

    Admits only DIRECTLY observed signals — `signal_state == "yes"` carrying both
    a quote and an http(s) source. An inferred signal is excluded by design: it
    was reasoned from a proxy and has no verbatim text to quote, so filling a
    quote cell for one would invent evidence that was never on the page. That is
    the same standard the engine's own evidence gate applies.

    Ordering is by descending signal weight, then signal_id, so the columns are
    stable across runs and two exports of one record never disagree.

    `captured` is the record's `date_enriched` — the day the page was crawled and
    the signal extracted. Signals carry no timestamp of their own, so this is the
    real capture date rather than a per-signal one invented to fill the column.
    """
    confirmed = [
        sig for sig in (record.get("signals") or [])
        if sig.get("signal_state") == "yes"
        and not sig.get("state_inferred")
        and (sig.get("evidence_text") or "").strip()
        and str(sig.get("source_url") or "").strip().lower().startswith(("http://", "https://"))
        and (sig.get("signal_label") or sig.get("signal_id"))
    ]
    confirmed.sort(
        key=lambda s: (-(s.get("positive_weight") or 0), str(s.get("signal_id") or "")))

    captured = record.get("date_enriched") or ""
    cells = {column: "" for column in _EVIDENCE_COLUMNS}
    for slot, sig in enumerate(confirmed[:EVIDENCE_COLUMN_SLOTS], start=1):
        cells[f"signal_{slot}_id"] = sig.get("signal_id") or ""
        cells[f"signal_{slot}_claim"] = sig.get("signal_label") or sig.get("label") or ""
        cells[f"signal_{slot}_quote"] = _truncate_quote(sig.get("evidence_text") or "")
        cells[f"signal_{slot}_source_url"] = sig.get("source_url") or ""
        cells[f"signal_{slot}_captured"] = captured
    return cells


def is_approved(rec: dict, rev: dict) -> bool:
    """Return True when a record passes the approved-export gate.

    Gate rules (all must hold):
    - qc_status == "approved"
    - effective displayed_tier != "excluded"
    - without an analyst override_tier, the pipeline tier is exportable:
      not "EXCLUDED", not "Needs Verification", and not "Manual Review"
      (unconfirmed / no-evidence accounts ship only after an analyst confirms
      them with an override)
    """
    if rev.get("qc_status") != "approved":
        return False
    if record_adapter.displayed_tier(rec, rev).lower() == "excluded":
        return False
    if not rev.get("override_tier"):
        if rec.get("exclusion_status") == "EXCLUDED":
            return False
        # Read through displayed_tier, not raw target_tier, so the legacy tier
        # rename resolves and an analyst override is honoured consistently with
        # every other export gate.
        if record_adapter.displayed_tier(rec, rev) in ("Needs Verification", "Manual Review"):
            return False
    return True


def build_approved_csv(run_id, run_directory, records=None, all_reviews=None,
                       client_facing=False) -> io.BytesIO:
    """Return a BytesIO CSV of approved records (all tiers)."""
    return _build_csv(run_id, run_directory, is_approved, records, all_reviews,
                      client_facing=client_facing)


def build_bullseye_csv(run_id, run_directory, records=None, all_reviews=None,
                       client_facing=False) -> io.BytesIO:
    """Return a BytesIO CSV of approved Bullseye-tier records only."""
    def _bullseye(rec: dict, rev: dict) -> bool:
        return is_approved(rec, rev) and record_adapter.displayed_tier(rec, rev).lower() == "bullseye"

    return _build_csv(run_id, run_directory, _bullseye, records, all_reviews,
                      client_facing=client_facing)


def build_contender_csv(run_id, run_directory, records=None, all_reviews=None,
                        client_facing=False) -> io.BytesIO:
    """Return a BytesIO CSV of Contender-tier records, shipped unless rejected.

    Only Bullseye blocks client-package readiness; Contenders are reviewed by the
    external sales team and ship by default, dropped only when an analyst sets
    qc_status == "rejected". This matches the Sales Handoff, which also drops only
    rejected records. (displayed_tier == "contender" already excludes Excluded /
    Needs Verification / Manual Review records.)
    """
    def _contender(rec: dict, rev: dict) -> bool:
        return (record_adapter.displayed_tier(rec, rev).lower() == "contender"
                and rev.get("qc_status") != "rejected")

    return _build_csv(run_id, run_directory, _contender, records, all_reviews,
                      client_facing=client_facing)


def build_excluded_csv(run_id, run_directory, records=None, all_reviews=None,
                       client_facing=False) -> io.BytesIO:
    """Return a BytesIO CSV of records whose effective tier is Excluded."""
    def _excluded(rec: dict, rev: dict) -> bool:
        return record_adapter.displayed_tier(rec, rev).lower() == "excluded"

    return _build_csv(run_id, run_directory, _excluded, records, all_reviews,
                      client_facing=client_facing)


def build_retry_csv(run_id: str, run_directory: Path) -> io.BytesIO:
    """Return a BytesIO manual-format CSV of records that failed to crawl.

    Includes records with source_confidence 'limited' or 'failed' — i.e.
    the pipeline could not extract meaningful web content. The CSV is in the
    Bullseye manual format so it can be uploaded as a new run for re-crawling.
    """
    results_path = run_directory / "enriched_targets.json"
    if not results_path.exists():
        return io.BytesIO()
    with open(results_path, "r", encoding="utf-8") as f:
        records = record_adapter.normalize_records_payload(json.load(f))

    crawl_failed = [
        r for r in records
        if r.get("source_confidence") in ("limited", "failed")
    ]
    if not crawl_failed:
        return io.BytesIO()

    fieldnames = ["practice_name", "website_url", "phone",
                  "address_city", "address_state", "address_zip", "specialty"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for rec in crawl_failed:
        writer.writerow({
            "practice_name": rec.get("practice_name", ""),
            "website_url": record_adapter.normalize_homepage_url(rec.get("website_url", "")),
            "phone": rec.get("phone", ""),
            "address_city": rec.get("address_city", ""),
            "address_state": rec.get("address_state", ""),
            "address_zip": rec.get("address_zip", ""),
            "specialty": rec.get("specialty", ""),
        })
    return io.BytesIO(buf.getvalue().encode("utf-8"))


def _build_csv(
    run_id: str,
    run_directory: Path,
    filter_fn: Callable[[dict, dict], bool],
    records: list[dict] | None = None,
    all_reviews: dict | None = None,
    client_facing: bool = False,
) -> io.BytesIO:
    """Load, merge, filter records and return a UTF-8 encoded BytesIO CSV.

    filter_fn receives (record, review) and returns True to include the row.
    Callers that already hold the records/reviews (e.g. the client-package
    builder) may pass them in to avoid re-reading the same files.
    """
    if records is None:
        results_path = run_directory / "enriched_targets.json"
        if not results_path.exists():
            return io.BytesIO()
        with open(results_path, "r", encoding="utf-8") as f:
            records = record_adapter.normalize_records_payload(json.load(f))

    if not records:
        return io.BytesIO()

    if all_reviews is None:
        all_reviews = reviews.get_reviews(run_id, run_directory)

    records = _territory_sorted(records)

    # Derive column order from first record (scalar fields only). Numeric scores
    # are always hidden; confidence_band rides along as a normal scalar so the
    # client sees the band, not the number. Client CSVs additionally drop internal
    # free-text columns and carry only the final tier from the review overlay.
    # Underscore-prefixed provenance is never a column — this defends against any
    # internal field leaking into a CSV even if it reaches enriched_targets.json.
    hidden = _HIDDEN_SCORE_COLUMNS | (_CLIENT_HIDDEN_COLUMNS if client_facing else set())
    first = records[0]
    record_columns = [
        k for k, v in first.items()
        if not isinstance(v, (dict, list))
        and k not in hidden
        and not k.startswith("_")
    ]
    review_columns = _CLIENT_REVIEW_COLUMNS if client_facing else _REVIEW_COLUMNS
    # De-duplicated, order preserved. _BRIEF_COLUMNS names fields the scalar scan
    # may also have picked up (provider_count exists on a consolidated record and
    # is derived for a non-consolidated one), and a repeated fieldname makes
    # DictWriter emit the same column twice.
    all_columns = list(dict.fromkeys(
        record_columns + _BRIEF_COLUMNS + _EVIDENCE_COLUMNS + review_columns))

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_columns, extrasaction="ignore")
    writer.writeheader()

    for rec in records:
        rid = record_adapter.get_record_id(rec)
        review = all_reviews.get(rid, reviews.default_review())

        if not filter_fn(rec, review):
            continue

        # Apply signal overlay so overridden signal states are reflected in any
        # signal-derived scalar fields. Signals (a list) are excluded from CSV
        # columns; is_override lives inside the list and never appears as a header.
        merged = reviews.apply_signal_overrides(rec, review)
        row = {k: (v if not isinstance(v, (dict, list)) else "") for k, v in merged.items()}
        row["why_contact"] = (merged.get("call_brief") or {}).get("why_contact", "")
        row.update(_consolidation_cells(merged))
        # Built from the OVERLAID signals, so an analyst's correction reaches the
        # CRM columns exactly as it reaches the handoff — a CSV that disagreed
        # with the HTML about what was confirmed would be worse than no columns.
        row.update(evidence_cells(merged))
        row.update({
            "displayed_tier": record_adapter.displayed_tier(rec, review),
            "qc_status": review.get("qc_status", "pending"),
            "analyst_note": review.get("analyst_note") or "",
            "override_tier": review.get("override_tier") or "",
            "override_reason": review.get("override_reason") or "",
            "reviewed_by": review.get("reviewed_by") or "",
            "reviewed_at": review.get("reviewed_at") or "",
        })
        writer.writerow({k: _escape_csv_cell(v) for k, v in row.items()})

    return io.BytesIO(buf.getvalue().encode("utf-8"))
