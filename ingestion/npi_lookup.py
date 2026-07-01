"""
npi_lookup.py
NPPES NPI registry enrichment for pipeline records.

Queries the public NPPES API (no auth) at ingestion time to populate provider
taxonomy data. Supports a generic taxonomy exclusion gate: run_config can
specify `taxonomy_exclusion_rules` (list of {taxonomy_code, rule_name}) so
that any client can pre-filter records whose NPI taxonomy matches a configured
code before spending crawl or LLM budget on them.

Run at Step 1b: after CSV ingest and address normalization, before the
structural pre-filter and crawl (so _npi_taxonomy_exclusions is available to
check_structural_exclusions before any crawl budget is spent).
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

logger = logging.getLogger(__name__)

NPPES_API_URL = "https://npiregistry.cms.hhs.gov/api/"
NPPES_API_VERSION = "2.1"

# Confidence tiers
CONFIDENCE_CONFIDENT = "confident"
CONFIDENCE_AMBIGUOUS = "ambiguous"
CONFIDENCE_NONE = "none"

# Empty NPI block — written to every record when no match is made
_EMPTY_NPI_FIELDS: dict = {
    "npi_number": None,
    "npi_match_confidence": CONFIDENCE_NONE,
    "npi_entity_type": None,
    "provider_taxonomy_codes": [],
    "_npi_taxonomy_exclusions": [],
    "npi_provider_count": None,
    "npi_practice_name": None,
}

# Polite inter-request pause; NPPES has informal (undocumented) rate limits
_REQUEST_DELAY_SECONDS = 0.15

# Max concurrent NPI workers — capped conservatively regardless of io_concurrency
_MAX_NPI_WORKERS = 4

# Noise tokens stripped before name comparison
_NAME_NOISE = frozenset({
    "of", "the", "and", "for", "at", "in",
    "pc", "llc", "pa", "inc", "ltd", "pllc", "lp",
    "assoc", "associates", "group", "center", "centre",
    "practice", "clinic", "health", "care", "medical",
    "medicine", "services", "solutions",
})


def _normalize_phone(phone: str) -> str:
    """Return 10-digit string from a phone value, or '' if not parseable."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    # Strip leading country code 1 from 11-digit strings
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def _name_tokens(name: str) -> frozenset[str]:
    """Return lowercase word tokens, noise-filtered."""
    if not name:
        return frozenset()
    return frozenset(re.findall(r"[a-z0-9]+", name.lower())) - _NAME_NOISE


def _names_agree(record_name: str, npi_name: str) -> bool:
    """Return True when the two names share enough significant tokens.

    Handles abbreviations, word-order differences, and DBA names by counting
    token overlap on the significant (noise-stripped) token sets. A single shared
    token is accepted only when one name reduces to a single significant token
    (e.g. "Kaiser" vs "Kaiser Permanente"); names with two or more significant
    tokens must share at least two, so a lone generic or city word cannot attach a
    wrong NPI.
    """
    rec = _name_tokens(record_name)
    npi = _name_tokens(npi_name)
    if not rec or not npi:
        return False
    common = len(rec & npi)
    # A single shared token is only enough when one name reduces to a single
    # significant token (e.g. "Kaiser" vs "Kaiser Permanente"). Two-plus-token
    # names must share at least two, so one common generic or city word (e.g.
    # "Park" in "Park Dermatology" vs "Park Endocrinology") cannot alone attach a
    # wrong NPI, which would otherwise drive a wrong taxonomy exclusion.
    threshold = 1 if min(len(rec), len(npi)) <= 1 else 2
    return common >= threshold


def _extract_taxonomy_codes(result_obj: dict) -> list[str]:
    """Return all taxonomy codes from a single NPPES result object."""
    codes: list[str] = []
    for taxonomy in result_obj.get("taxonomies") or []:
        code = (taxonomy.get("code") or "").strip()
        if code:
            codes.append(code)
    return codes


def _npi_org_name(result_obj: dict) -> str:
    """Extract the best display name from an NPPES result object."""
    name = (result_obj.get("organization_name") or "").strip()
    if not name:
        basic = result_obj.get("basic") or {}
        name = (basic.get("organization_name") or "").strip()
    if not name:
        basic = result_obj.get("basic") or {}
        first = (basic.get("first_name") or "").strip()
        last = (basic.get("last_name") or "").strip()
        name = f"{first} {last}".strip()
    return name


def _npi_location_phone(result_obj: dict) -> str:
    """Extract the practice-location phone from an NPPES result object."""
    for addr in result_obj.get("addresses") or []:
        if addr.get("address_purpose") == "LOCATION":
            return _normalize_phone(addr.get("telephone_number") or "")
    return ""


def _query_nppes(params: dict, timeout: int = 8) -> Optional[dict]:
    """Execute one NPPES API request. Returns parsed JSON or None on any error."""
    query = {"version": NPPES_API_VERSION, "limit": "5"}
    query.update(params)
    url = NPPES_API_URL + "?" + urllib.parse.urlencode(query)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BEMI-Pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("NPPES query failed (%s): %s", url, exc)
        return None


def _taxonomy_exclusions(codes: list[str], taxonomy_rules: list[dict]) -> list[str]:
    """Return rule names from taxonomy_rules whose taxonomy_code appears in codes."""
    return [
        r["rule_name"] for r in taxonomy_rules
        if r.get("taxonomy_code") and r.get("rule_name") and r["taxonomy_code"] in codes
    ]


def _match_record(record: dict, taxonomy_rules: list[dict] | None = None) -> dict:
    """Look up a single record in NPPES and return NPI field values.

    taxonomy_rules: list of {"taxonomy_code": str, "rule_name": str} from
    run_config["taxonomy_exclusion_rules"]. When a matched NPI carries one of
    these taxonomy codes, the corresponding rule_name is added to
    _npi_taxonomy_exclusions so check_structural_exclusions can pre-filter
    the record before any crawl spend.

    Fast path: when npi_optional is populated (Outscraper CSV often carries the
    NPI), query NPPES directly by number — no address-match ambiguity, no
    quota spent on candidate scoring.

    Normal path: query by ZIP + practice name, then confirm with name/phone
    agreement per the §3 confidence rule from the spec.
    """
    taxonomy_rules = taxonomy_rules or []
    npi_optional = (record.get("npi_optional") or "").strip()

    # ------------------------------------------------------------------
    # Fast path: NPI already known
    # ------------------------------------------------------------------
    if npi_optional:
        resp = _query_nppes({"number": npi_optional})
        results = (resp or {}).get("results") or []
        if results:
            r = results[0]
            codes = _extract_taxonomy_codes(r)
            return {
                "npi_number": npi_optional,
                "npi_match_confidence": CONFIDENCE_CONFIDENT,
                "npi_entity_type": ("organization"
                                    if r.get("enumeration_type") == "NPI-2"
                                    else "individual"),
                "provider_taxonomy_codes": codes,
                "_npi_taxonomy_exclusions": _taxonomy_exclusions(codes, taxonomy_rules),
                "npi_provider_count": len(results),
                "npi_practice_name": _npi_org_name(r) or None,
            }
        # NPI from CSV didn't resolve — fall through to address-match
        logger.debug("npi_optional %s did not resolve in NPPES", npi_optional)

    # ------------------------------------------------------------------
    # Normal path: address-match
    # ------------------------------------------------------------------
    address_zip = (record.get("address_zip") or "").strip()[:5]
    practice_name = (record.get("practice_name") or "").strip()
    phone = _normalize_phone(record.get("phone") or "")

    if not address_zip or not practice_name:
        return dict(_EMPTY_NPI_FIELDS)

    # Try organization NPI first (most practices are type-2 org entities)
    resp = _query_nppes({
        "postal_code": address_zip,
        "organization_name": practice_name[:60],
        "enumeration_type": "NPI-2",
    })
    candidates = (resp or {}).get("results") or []

    # Fallback: search without type filter (solo practitioners use type-1 NPIs)
    if not candidates:
        resp2 = _query_nppes({
            "postal_code": address_zip,
            "organization_name": practice_name[:60],
        })
        candidates = (resp2 or {}).get("results") or []

    if not candidates:
        return dict(_EMPTY_NPI_FIELDS)

    # Score each ZIP-matched candidate with name + phone agreement
    for candidate in candidates:
        npi_name = _npi_org_name(candidate)
        npi_phone = _npi_location_phone(candidate)
        name_ok = _names_agree(practice_name, npi_name)
        phone_ok = bool(phone and npi_phone and phone == npi_phone)

        if name_ok or phone_ok:
            codes = _extract_taxonomy_codes(candidate)
            return {
                "npi_number": candidate.get("number"),
                "npi_match_confidence": CONFIDENCE_CONFIDENT,
                "npi_entity_type": ("organization"
                                    if candidate.get("enumeration_type") == "NPI-2"
                                    else "individual"),
                "provider_taxonomy_codes": codes,
                "_npi_taxonomy_exclusions": _taxonomy_exclusions(codes, taxonomy_rules),
                "npi_provider_count": len(candidates),
                "npi_practice_name": npi_name or None,
            }

    # ZIP matched but neither name nor phone agreed → ambiguous
    return {**_EMPTY_NPI_FIELDS, "npi_match_confidence": CONFIDENCE_AMBIGUOUS}


def enrich_records(records: list[dict], run_config: dict) -> list[dict]:
    """Populate NPI fields on all records via the NPPES public API.

    Parallel execution bounded by _MAX_NPI_WORKERS (cap irrespective of
    io_concurrency — NPPES is a public service, not our infrastructure).
    Per-request sleep throttles each worker thread.

    Failures are non-fatal: a record that errors gets the empty NPI block.

    Args:
        records: Canonical pipeline records (mutated in-place).
        run_config: Loaded run_config dict (unused currently; accepted for
            future per-client override knobs).

    Returns:
        Same list with NPI fields populated on each record.
    """
    if not records:
        return records

    taxonomy_rules = run_config.get("taxonomy_exclusion_rules") or []

    def _enrich_one(record: dict) -> None:
        try:
            npi_fields = _match_record(record, taxonomy_rules)
        except Exception as exc:
            logger.warning("NPI lookup error for %s: %s", record.get("id"), exc)
            npi_fields = dict(_EMPTY_NPI_FIELDS)
        record.update(npi_fields)
        time.sleep(_REQUEST_DELAY_SECONDS)

    workers = min(_MAX_NPI_WORKERS, len(records))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_enrich_one, r): r for r in records}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                rec = futures[future]
                logger.warning("NPI future error for %s: %s", rec.get("id"), exc)
                rec.update(dict(_EMPTY_NPI_FIELDS))

    confident = sum(
        1 for r in records if r.get("npi_match_confidence") == CONFIDENCE_CONFIDENT
    )
    ambiguous = sum(
        1 for r in records if r.get("npi_match_confidence") == CONFIDENCE_AMBIGUOUS
    )
    taxonomy_hits = sum(1 for r in records if r.get("_npi_taxonomy_exclusions"))

    print(
        f"  [NPI] {confident}/{len(records)} confident matches, "
        f"{ambiguous} ambiguous, "
        f"{len(records) - confident - ambiguous} no match"
        + (f" — {taxonomy_hits} taxonomy exclusion hit(s)" if taxonomy_hits else "")
    )
    return records
