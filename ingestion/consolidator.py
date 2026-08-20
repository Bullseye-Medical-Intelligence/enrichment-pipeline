"""
consolidator.py
Practice-location consolidation at ingest, in two deliberately separate passes.

PASS 1 — merge. Provider rows that describe the same physical practice location
become one record. Candidates are blocked on (zip5 + street) so the comparison
is not O(n^2); within a block a differing unit is a hard veto, and otherwise a
weighted score decides merge / review / separate.

PASS 2 — link, never merge. The surviving locations are grouped by registrable
domain. A six-office group stays six billable location records and simply gains
group_id / group_name / location_index / location_count, so a deliverable can
say "Location 3 of 6" without ever reading as double-billing.

The two passes do not share a decision. Pass 1 answers "is this the same
location?"; Pass 2 answers "do these locations belong to one organization?".

Both run before any crawl or LLM spend, so the count the no-spend roster preview
reports is the consolidated count.

Merging is lossless: every input row survives in providers[] / source_row_ids[],
and the consolidation block records exactly why the rows became one. Identity is
deterministic — practice_id derives from normalized keys only, never from input
ordering, time, or randomness — so run comparison, suppression and rescore stay
stable across runs.
"""

from __future__ import annotations

import copy
from difflib import SequenceMatcher

from ingestion.practice_normalizer import identity_of, stable_hash

# ---------------------------------------------------------------------------
# Scoring — the weights are the contract. Do not implement a conjunctive
# "same address AND same phone" rule (it splits practices whose providers were
# scraped with different department numbers) and do not implement a bare OR
# (it merges unrelated practices sharing a building or a host).
# ---------------------------------------------------------------------------

SCORE_ADDRESS = 4          # identical street + zip5
SCORE_PHONE = 3            # identical normalized phone
SCORE_DOMAIN = 3           # identical registrable domain, not an aggregator
SCORE_NAME = 2             # practice-name similarity at or above the threshold

# Conflict penalty. Agreement alone cannot separate "no corroborating data"
# from "contradicting data": both land on the bare address score, which is why
# a medical office building full of unrelated practices produced thousands of
# identical mid-range pairs. One practice does not have two websites, so two
# real and different domains are positive evidence of two practices.
#
# There is deliberately NO matching penalty for differing phone numbers.
# Absence of a phone match is not evidence of difference — one practice
# legitimately publishes a main line, a scheduling line and a billing line, and
# penalising that would break the exact case consolidation exists to fix.
SCORE_DOMAIN_CONFLICT = -3

NAME_SIMILARITY_THRESHOLD = 0.85
MERGE_THRESHOLD = 6        # >= 6 merges
REVIEW_THRESHOLD = 4       # 4-5 goes to the review queue, never a silent split

# ---------------------------------------------------------------------------
# Two domain lists with DIFFERENT semantics. They are not one list with an
# exception, because they answer different questions:
#
#   NOISE     — proves nothing about ownership. A directory listing, a social
#               profile or a site-builder host is a scraping artifact, not the
#               practice's own web presence. Two records both showing
#               healthgrades.com tells you nothing at all.
#               -> contributes 0 in Pass 1; ignored in Pass 2.
#
#   UMBRELLA  — proves shared OWNERSHIP but not shared LOCATION. Two rows at an
#               identical street and ZIP on one health-system domain are very
#               likely the same clinic, because the address has already pinned
#               the location and the unit gate has already run.
#               -> valid merge evidence in Pass 1; ignored in Pass 2, because
#                  two hundred locations under one system domain are not a
#                  commercial group.
#
# Measured: treating umbrella domains as noise produced MORE locations
# (810 vs 769) and doubled the review queue — the signature of over-splitting
# real practices, not of better precision.
# ---------------------------------------------------------------------------

# NOISE — excluded from both passes.
NOISE_DOMAINS: frozenset[str] = frozenset({
    # Physician directories / review sites
    "healthgrades.com", "zocdoc.com", "vitals.com", "webmd.com", "wellness.com",
    "ratemds.com", "sharecare.com", "doximity.com", "npidb.org", "npino.com",
    "healthcare6.com", "findatopdoc.com", "caredash.com", "doctor.com",
    "md.com", "realself.com", "psychologytoday.com",
    # General listing / business directories
    "yelp.com", "yellowpages.com", "mapquest.com", "bbb.org", "manta.com",
    "chamberofcommerce.com", "dnb.com", "indeed.com", "glassdoor.com",
    # Social / platform hosts
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "google.com", "business.site", "youtube.com",
    # Generic site builders and domain parking (a shared host, not a practice)
    "wixsite.com", "wix.com", "squarespace.com", "weebly.com", "wordpress.com",
    "godaddysites.com", "blogspot.com", "webnode.com", "site123.me",
    "sedo.com", "sedoparking.com", "afternic.com", "hugedomains.com",
    "bodis.com", "parkingcrew.net", "above.com", "dan.com",
})

# UMBRELLA — shared ownership, not shared location. Health systems, academic
# medical centres, private-equity roll-ups and large multi-site groups. Counted
# as merge evidence in Pass 1 (where street + ZIP already pin the location) and
# ignored in Pass 2 (where they would link every location of a national system
# into one meaningless group).
#
# Not client-specific and not specialty-specific, so this does not conflict with
# the no-client-names-in-engine rule — these are infrastructure facts, the same
# category as a directory host. It is, however, unavoidably INCOMPLETE: there
# are thousands of systems and new ones appear through consolidation. Treat this
# as a starting set and extend per cartridge via `additional_umbrella_domains`.
# A structural guard (capping the size of a Pass 2 group) would cover the tail
# more reliably than any list can.
UMBRELLA_DOMAINS: frozenset[str] = frozenset({
    # National operators and large multi-state systems
    "hcahealthcare.com", "tenethealth.com", "commonspirit.org", "ascension.org",
    "providence.org", "trinity-health.org", "chsnet.com", "lifepointhealth.net",
    "steward.org", "prospectmedical.com", "upmc.com", "advocatehealth.com",
    # California and the Pacific Northwest
    "sutterhealth.org", "suttermedicalfoundation.org", "kp.org",
    "kaiserpermanente.org", "dignityhealth.org", "adventisthealth.org",
    "memorialcare.org", "scripps.org", "sharp.com", "cedars-sinai.org",
    "stanfordhealthcare.org", "ucsfhealth.org", "uclahealth.org",
    "ucihealth.org", "ucdavis.edu", "ucdavishealth.org", "ucsdhealth.org",
    "sansumclinic.org", "johnmuirhealth.com", "elcaminohealth.org",
    "ohsu.edu", "providence.org", "peacehealth.org", "multicare.org",
    "swedish.org", "virginiamason.org", "legacyhealth.org",
    # Midwest, South, Northeast
    "mayoclinic.org", "clevelandclinic.org", "hopkinsmedicine.org",
    "massgeneralbrigham.org", "nyulangone.org", "mountsinai.org",
    "northwell.edu", "pennmedicine.org", "jefferson.edu", "templehealth.org",
    "medstarhealth.org", "christianacare.org", "nm.org", "rush.edu",
    "uchicagomedicine.org", "henryford.com", "corewellhealth.org",
    "allina.com", "fairview.org", "healthpartners.com", "sanfordhealth.org",
    "essentiahealth.org", "ssmhealth.com", "mercy.net", "bjc.org",
    "bannerhealth.com", "intermountainhealthcare.org", "geisinger.org",
    "atriumhealth.org", "novanthealth.org", "wellstar.org", "piedmont.org",
    "emoryhealthcare.org", "inova.org", "sentara.com", "ochsner.org",
    "houstonmethodist.org", "memorialhermann.org", "bswhealth.com",
    "utsouthwestern.edu", "mdanderson.org", "adventhealth.com",
    "orlandohealth.com", "baptisthealth.net", "ynhhs.org",
    "hartfordhealthcare.org", "lifespan.org", "bmc.org", "bidmc.org",
    "tuftsmedicine.org", "dartmouth-hitchcock.org", "mainehealth.org",
})

# Credential tokens split out of a provider name so "Jane Smith, MD" yields a
# name and a credential rather than one opaque string.
CREDENTIAL_TOKENS: frozenset[str] = frozenset({
    "md", "do", "dds", "dmd", "od", "dpm", "dc", "np", "pa", "pa-c", "pac",
    "rn", "aprn", "fnp", "cnm", "phd", "psyd", "mbbs", "ms", "mph",
    "facog", "faap", "facs", "facc", "facp", "faan", "fase",
})


def _resolve_domain_list(settings: dict, replace_key: str, extend_key: str,
                         default: frozenset[str]) -> frozenset[str]:
    """Resolve one domain list: a cartridge may replace the default and/or extend it."""
    override = settings.get(replace_key)
    base = (frozenset(d.strip().lower() for d in override if d)
            if override is not None else default)
    extra = settings.get(extend_key) or []
    return base | frozenset(d.strip().lower() for d in extra if d)


def domain_policy(run_config: dict) -> tuple[frozenset[str], frozenset[str]]:
    """Return (noise_domains, umbrella_domains) for this run.

    Two lists, two meanings — see NOISE_DOMAINS / UMBRELLA_DOMAINS. Pass 1
    disqualifies only noise; Pass 2 and practice identity ignore both.
    """
    settings = (run_config or {}).get("consolidation") or {}
    noise = _resolve_domain_list(
        settings, "noise_domains", "additional_noise_domains", NOISE_DOMAINS)
    umbrella = _resolve_domain_list(
        settings, "umbrella_domains", "additional_umbrella_domains", UMBRELLA_DOMAINS)
    return noise, umbrella


def _is_enabled(run_config: dict) -> bool:
    """Consolidation is on unless a cartridge explicitly disables it."""
    settings = (run_config or {}).get("consolidation") or {}
    return bool(settings.get("enabled", True))


# ---------------------------------------------------------------------------
# Deterministic ordering helpers — nothing may depend on input row order
# ---------------------------------------------------------------------------

def _signature(record: dict, ident: dict) -> str:
    """Stable per-record sort key built only from normalized values."""
    return "|".join((
        ident["zip5"], ident["street"], ident["unit"], ident["phone"],
        ident["domain"], ident["name"], str(record.get("id") or ""),
    ))


def _name_similarity(a: str, b: str) -> float:
    """Similarity of two normalized practice names (0.0 when either is empty)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def units_conflict(left: dict, right: dict) -> bool:
    """True when both identities carry a unit and the units differ.

    This is the hard gate. Two practices in Suite 200 and Suite 400 of one
    building are different practices, and no score may override that.
    """
    return bool(left["unit"]) and bool(right["unit"]) and left["unit"] != right["unit"]


def score_pair(left: dict, right: dict, noise_domains: frozenset[str]) -> tuple[int, list[str]]:
    """Score one candidate pair; returns (score, matched_fields).

    Additive by design. A practice whose providers were scraped with different
    department phone numbers still reaches 7 on address + domain, where a
    conjunctive rule would have split it and shipped duplicates.
    """
    score = 0
    matched: list[str] = []

    if left["street"] and left["zip5"] \
            and left["street"] == right["street"] and left["zip5"] == right["zip5"]:
        score += SCORE_ADDRESS
        matched.append("address")

    if left["phone"] and left["phone"] == right["phone"]:
        score += SCORE_PHONE
        matched.append("phone")

    # Both domains must be real and non-aggregator before either agreement or
    # conflict means anything: a shared directory host proves nothing, and two
    # different directory listings are not two practices.
    left_domain, right_domain = left["domain"], right["domain"]
    comparable_domains = (
        left_domain and right_domain
        and left_domain not in noise_domains and right_domain not in noise_domains
    )
    if comparable_domains:
        if left_domain == right_domain:
            score += SCORE_DOMAIN
            matched.append("domain")
        else:
            score += SCORE_DOMAIN_CONFLICT
            matched.append("domain_conflict")

    if _name_similarity(left["name"], right["name"]) >= NAME_SIMILARITY_THRESHOLD:
        score += SCORE_NAME
        matched.append("name")

    return score, matched


# ---------------------------------------------------------------------------
# Pass 1 — merge provider rows into practice locations
# ---------------------------------------------------------------------------

def _block_key(ident: dict):
    """Blocking key: (zip5, street). None when the record cannot be blocked.

    A record missing either half is never compared to anything and stays its own
    location. That is the conservative direction — an unblocked record is
    reported in the summary rather than silently guessed at.
    """
    if ident["zip5"] and ident["street"]:
        return (ident["zip5"], ident["street"])
    return None


class _UnitAwareUnionFind:
    """Union-find whose clusters may never accumulate two different units.

    The pairwise gate stops a differing-unit pair from ever becoming a merge
    edge, but transitivity could still join Suite 200 to Suite 400 through a
    unit-less record. The cluster-level check closes that path.
    """

    def __init__(self, units: list[str]):
        self._parent = list(range(len(units)))
        self._units = [{u} if u else set() for u in units]

    def find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def union(self, i: int, j: int) -> bool:
        """Merge two clusters; returns False (and changes nothing) on unit conflict."""
        root_i, root_j = self.find(i), self.find(j)
        if root_i == root_j:
            return True
        combined = self._units[root_i] | self._units[root_j]
        if len(combined) > 1:
            return False
        low, high = (root_i, root_j) if root_i < root_j else (root_j, root_i)
        self._parent[high] = low
        self._units[low] = combined
        return True


def _merge_practice_locations(records: list[dict], noise_domains: frozenset[str]) -> dict:
    """Run Pass 1. Returns clustering results without mutating the input."""
    identities = [identity_of(r) for r in records]
    signatures = [_signature(r, i) for r, i in zip(records, identities)]

    blocks: dict[tuple, list[int]] = {}
    unblocked = 0
    for idx, ident in enumerate(identities):
        key = _block_key(ident)
        if key is None:
            unblocked += 1
            continue
        blocks.setdefault(key, []).append(idx)

    merge_edges: list[tuple[int, list[str], int, int]] = []
    review_edges: list[tuple[int, list[str], int, int]] = []

    for members in blocks.values():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda i: signatures[i])
        for a_pos in range(len(ordered)):
            for b_pos in range(a_pos + 1, len(ordered)):
                i, j = ordered[a_pos], ordered[b_pos]
                if units_conflict(identities[i], identities[j]):
                    continue                      # hard stop, never scored
                score, matched = score_pair(identities[i], identities[j], noise_domains)
                if score >= MERGE_THRESHOLD:
                    merge_edges.append((score, matched, i, j))
                elif score >= REVIEW_THRESHOLD:
                    review_edges.append((score, matched, i, j))

    # Deterministic union order: strongest first, then by normalized signature.
    merge_edges.sort(key=lambda e: (-e[0], signatures[e[2]], signatures[e[3]]))

    union_find = _UnitAwareUnionFind([ident["unit"] for ident in identities])
    applied: list[tuple[int, list[str], int, int]] = []
    for score, matched, i, j in merge_edges:
        if union_find.union(i, j):
            applied.append((score, matched, i, j))
        else:
            review_edges.append((score, matched, i, j))   # blocked by the unit gate

    clusters: dict[int, list[int]] = {}
    for idx in range(len(records)):
        clusters.setdefault(union_find.find(idx), []).append(idx)

    return {
        "identities": identities,
        "signatures": signatures,
        "clusters": clusters,
        "applied_edges": applied,
        "review_edges": review_edges,
        "unblocked": unblocked,
    }


# ---------------------------------------------------------------------------
# Lossless merge of one cluster
# ---------------------------------------------------------------------------

def _split_credentials(raw_name: str) -> tuple[str, list[str]]:
    """Split "Jane Smith, MD, FACOG" into ("Jane Smith", ["MD", "FACOG"])."""
    parts = [p.strip() for p in (raw_name or "").split(",") if p.strip()]
    if not parts:
        return "", []
    name = parts[0]
    credentials = [p for p in parts[1:]
                   if p.lower().replace(".", "").replace(" ", "") in CREDENTIAL_TOKENS]
    return name, credentials


def _providers_from_record(record: dict) -> list[dict]:
    """Build provider entries for one source row.

    The row is the provider unit, so its NPI and taxonomy attach to the row's
    first named provider. Additional names on the same row get entries without
    an NPI rather than borrowing one that is not theirs.
    """
    names = [n for n in (record.get("provider_names") or []) if str(n).strip()]
    if not names:
        names = [record.get("practice_name") or ""]
    npi = (record.get("npi_number") or record.get("npi_optional") or "") or ""
    taxonomy = list(record.get("provider_taxonomy_codes") or [])
    specialty = record.get("specialty") or ""
    source_id = str(record.get("id") or "")

    entries = []
    for position, raw_name in enumerate(names):
        name, credentials = _split_credentials(str(raw_name))
        if not name:
            continue
        entries.append({
            "name": name,
            "credentials": credentials,
            "npi": str(npi) if position == 0 else "",
            "taxonomy_codes": taxonomy if position == 0 else [],
            "specialty": specialty,
            "source_record_id": source_id,
        })
    return entries


# Generic organizational words used to prefer a practice name over a person's
# name when a cluster has no organization NPI to borrow from. Industry-generic
# English only — no specialty, client or brand terms (RULE 3).
_ORG_NAME_TOKENS: frozenset[str] = frozenset({
    "medical", "health", "healthcare", "clinic", "clinics", "center", "centre",
    "group", "associates", "partners", "institute", "foundation", "practice",
    "care", "services", "specialists", "physicians", "hospital", "offices",
    "pavilion", "campus", "network", "affiliates", "consultants",
})


def _looks_like_organization(name: str) -> bool:
    """True when a name reads as a practice rather than an individual."""
    tokens = {t.strip(".,").lower() for t in (name or "").split()}
    return bool(tokens & _ORG_NAME_TOKENS)


def _segment_domain_label(label: str) -> list[str]:
    """Split a concatenated domain label into recognisable word segments.

    "suttermedicalfoundation" -> ["sutter", "medical", "foundation"] by finding
    known organisational words inside it. A label with no recognisable word
    stays whole, which is what the legibility gate then judges.
    """
    segments: list[str] = []
    remaining = label
    while remaining:
        hit_at, hit_token = None, ""
        for token in _ORG_NAME_TOKENS:
            index = remaining.find(token)
            if index != -1 and (hit_at is None or index < hit_at
                                or (index == hit_at and len(token) > len(hit_token))):
                hit_at, hit_token = index, token
        if hit_at is None:
            segments.append(remaining)
            break
        if hit_at > 0:
            segments.append(remaining[:hit_at])
        segments.append(hit_token)
        remaining = remaining[hit_at + len(hit_token):]
    return [s for s in segments if s]


def _name_from_domain(domain: str) -> str:
    """Title-cased practice name derived from a registrable domain, or "".

    Only when the result is legible: at least two recognisable word segments, or
    a single segment of eight or more characters. "suttermedicalfoundation.org"
    qualifies; "smgdocs.com" does not, and an illegible label is worse than an
    honest placeholder.
    """
    label = (domain or "").split(".")[0].strip()
    if not label:
        return ""
    segments = _segment_domain_label(label)
    if len(segments) < 2 and len(label) < 8:
        return ""
    return " ".join(segment.title() for segment in segments)


def _resolve_practice_name(names: list[str], org_names: list[str], domain: str,
                           provider_count: int, street: str) -> tuple[str, str]:
    """Resolve a merged location's name. Returns (name, derivation_source).

    HARD CONSTRAINT: a multi-provider location is never labelled with a single
    individual's personal name. "Andres Sciolla" on a 56-provider location tells
    a rep it is a solo practice — wrong in a way that changes how they work the
    call, and careless in a client deliverable. A generic placeholder beats a
    confidently wrong person's name.

    Chain, first that succeeds:
      1. organisation name from NPI
      2. most frequent organisation-shaped name among the source rows
      3. domain-derived, if legible
      4. "{n} providers at {street}" placeholder
    """
    from_npi = _pick_most_common(org_names)
    if from_npi:
        return from_npi, "npi_organization"

    organizational = [n for n in names if n and _looks_like_organization(n)]
    if organizational:
        return _pick_most_common(organizational), "source_row_organization"

    # Only a single-provider location may carry a person's name.
    if provider_count <= 1:
        observed = _pick_most_common(names)
        if observed:
            return observed, "source_row"

    from_domain = _name_from_domain(domain)
    if from_domain:
        return from_domain, "domain_derived"

    where = street.strip() or "an unlisted address"
    return f"{provider_count} providers at {where}", "placeholder"


def _pick_most_common(values: list[str]) -> str:
    """Most frequent non-empty value; ties broken lexicographically (deterministic)."""
    counts: dict[str, int] = {}
    for value in values:
        cleaned = (value or "").strip()
        if cleaned:
            counts[cleaned] = counts.get(cleaned, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _base_index(members: list[int], records: list[dict], signatures: list[str]) -> int:
    """Choose the surviving base row deterministically.

    Prefers a row whose NPI resolved to an organization (it already describes the
    practice rather than one physician), then the most complete row, then the
    lexicographically smallest signature.
    """
    def sort_key(idx: int):
        record = records[idx]
        is_org = record.get("npi_entity_type") == "organization"
        completeness = sum(
            1 for field in ("website_url", "phone", "address_street",
                            "address_zip", "address_city", "specialty")
            if (record.get(field) or "")
        )
        return (0 if is_org else 1, -completeness, signatures[idx])

    return sorted(members, key=sort_key)[0]


def _merge_cluster(members: list[int], records: list[dict], identities: list[dict],
                   signatures: list[str], edges: list[tuple]) -> dict:
    """Build one practice-location record from a cluster of source rows."""
    ordered = sorted(members, key=lambda i: signatures[i])
    base_idx = _base_index(members, records, signatures)
    merged = copy.deepcopy(records[base_idx])

    if len(ordered) > 1:
        # Fill any field the base row left empty from its siblings, in a fixed
        # order, so a merge never loses data the input actually carried.
        for field in ("website_url", "phone", "address_street", "address_unit",
                      "address_city", "address_state", "address_zip",
                      "google_place_id", "npi_optional", "specialty",
                      "metro_region_tag", "state_mandate_status"):
            if not (merged.get(field) or ""):
                for idx in ordered:
                    value = records[idx].get(field) or ""
                    if value:
                        merged[field] = value
                        break

    providers: list[dict] = []
    seen_providers: set[tuple[str, str]] = set()
    for idx in ordered:
        for entry in _providers_from_record(records[idx]):
            key = (entry["name"].strip().lower(), entry["npi"])
            if key in seen_providers:
                continue
            seen_providers.add(key)
            providers.append(entry)
    providers.sort(key=lambda p: (p["name"].lower(), p["npi"]))

    taxonomy_union = sorted({
        code for idx in ordered
        for code in (records[idx].get("provider_taxonomy_codes") or [])
    })
    specialties = sorted({
        (records[idx].get("specialty") or "").strip()
        for idx in ordered
        if (records[idx].get("specialty") or "").strip()
    })

    cluster_set = set(members)
    cluster_edges = [e for e in edges if e[2] in cluster_set and e[3] in cluster_set]
    best_score = max((e[0] for e in cluster_edges), default=0)
    matched_fields = sorted({f for e in cluster_edges for f in e[1]})

    # The unit is kept as its own field, never left folded inside the street.
    # Sources routinely ship "2800 L St #500" in one column; the parsed unit is
    # what guards against over-merging, so it must survive onto the record and
    # not live only inside the comparison keys.
    base_identity = identity_of(merged)
    if not (merged.get("address_unit") or "").strip():
        merged["address_unit"] = base_identity["unit"]
    merged["address_street_normalized"] = base_identity["street"]
    merged["address_unit_normalized"] = base_identity["unit"]

    # Naming runs after providers are known: the never-a-person constraint
    # depends on how many providers the location actually carries.
    if len(ordered) > 1:
        name, name_source = _resolve_practice_name(
            names=[records[i].get("practice_name") or "" for i in ordered],
            org_names=[records[i].get("npi_practice_name") or "" for i in ordered
                       if records[i].get("npi_entity_type") == "organization"],
            domain=base_identity["domain"],
            provider_count=len(providers),
            street=merged.get("address_street") or "",
        )
        merged["practice_name"] = name or merged.get("practice_name") or ""
        merged["practice_name_source"] = name_source
    else:
        merged["practice_name_source"] = "source_row"

    merged["providers"] = providers
    merged["provider_count"] = len(providers)
    merged["specialties"] = specialties
    merged["provider_taxonomy_codes"] = taxonomy_union
    merged["source_row_ids"] = sorted(str(records[i].get("id") or "") for i in ordered)
    merged["consolidation"] = {
        "rule_fired": "merged" if len(ordered) > 1 else "single",
        "matched_fields": matched_fields,
        "score": best_score,
        "merged_count": len(ordered),
        "reviewed_by": "",
        "review_candidates": [],
    }
    return merged


# ---------------------------------------------------------------------------
# Deterministic practice identity
# ---------------------------------------------------------------------------

def _identity_base(ident: dict, shared_domains: frozenset[str]) -> tuple[str, str]:
    """Strongest available stable identity for a location, as (kind, key)."""
    if ident["zip5"] and ident["street"]:
        return "addr", f"{ident['zip5']}|{ident['street']}|{ident['unit']}"
    if ident["domain"] and ident["domain"] not in shared_domains:
        return "domain", ident["domain"]
    if ident["phone"]:
        return "phone", ident["phone"]
    if ident["name"] and ident["zip5"]:
        return "namezip", f"{ident['name']}|{ident['zip5']}"
    return "name", ident["name"]


def _assign_practice_ids(cluster_items: list[dict], shared_domains: frozenset[str]) -> None:
    """Stamp a deterministic practice_id on each cluster, in place.

    Derived from normalized keys only. When two distinct clusters legitimately
    share a base key — the review-queue case, where scoring kept them apart at
    one address — they are disambiguated by their sorted member signatures, so
    the result is still identical on every run.
    """
    by_base: dict[tuple[str, str], list[dict]] = {}
    for item in cluster_items:
        by_base.setdefault(_identity_base(item["identity"], shared_domains), []).append(item)

    for (kind, key), items in by_base.items():
        if len(items) == 1:
            items[0]["practice_id"] = "P-" + stable_hash(kind, key)
            continue
        for position, item in enumerate(sorted(items, key=lambda it: it["cluster_signature"])):
            parts = [kind, key] if position == 0 else [kind, key, item["cluster_signature"]]
            item["practice_id"] = "P-" + stable_hash(*parts)


# ---------------------------------------------------------------------------
# Pass 2 — link locations into groups (never merges)
# ---------------------------------------------------------------------------

def link_location_groups(records: list[dict], shared_domains: frozenset[str]) -> int:
    """Stamp group fields on practice locations sharing a registrable domain.

    Records are never combined here. A group of six locations stays six records;
    each learns that it is one of six. Denylisted and empty domains never group —
    a shared directory host says nothing about shared ownership.

    Returns the number of multi-location groups found.
    """
    by_domain: dict[str, list[dict]] = {}
    for record in records:
        ident = identity_of(record)
        domain = ident["domain"]
        if not domain or domain in shared_domains:
            continue
        by_domain.setdefault(domain, []).append(record)

    for record in records:
        record.setdefault("group_id", "")
        record.setdefault("group_name", "")
        record.setdefault("location_index", 1)
        record.setdefault("location_count", 1)

    multi_location_groups = 0
    for domain, members in by_domain.items():
        if len(members) < 2:
            continue
        multi_location_groups += 1
        group_id = "G-" + stable_hash("domain", domain)
        group_name = _pick_most_common([m.get("practice_name") or "" for m in members])
        ordered = sorted(members, key=lambda m: (
            (m.get("address_zip") or ""), (m.get("address_street") or ""),
            (m.get("address_unit") or ""), str(m.get("practice_id") or m.get("id") or ""),
        ))
        for position, member in enumerate(ordered, start=1):
            member["group_id"] = group_id
            member["group_name"] = group_name or domain
            member["location_index"] = position
            member["location_count"] = len(ordered)

    return multi_location_groups


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def consolidate_records(records: list[dict], run_config: dict) -> tuple[list[dict], dict]:
    """Run Pass 1 then Pass 2 over ingested records.

    Returns (consolidated_records, summary). The input list is not mutated; the
    returned records are new objects carrying providers[], source_row_ids[], the
    consolidation block, a deterministic practice_id, and Pass 2 group fields.
    """
    if not records:
        return [], {"enabled": _is_enabled(run_config), "input_count": 0,
                    "output_count": 0, "merged_groups": 0, "rows_merged_away": 0,
                    "review_pairs": 0, "unblocked_count": 0,
                    "multi_location_groups": 0}

    noise_domains, umbrella_domains = domain_policy(run_config)
    # Pass 2 and practice identity ignore both lists; Pass 1 disqualifies only noise.
    shared_domains = noise_domains | umbrella_domains
    if not _is_enabled(run_config):
        return records, {"enabled": False, "input_count": len(records),
                         "output_count": len(records), "merged_groups": 0,
                         "rows_merged_away": 0, "review_pairs": 0,
                         "unblocked_count": 0, "multi_location_groups": 0}

    pass1 = _merge_practice_locations(records, noise_domains)
    identities = pass1["identities"]
    signatures = pass1["signatures"]

    cluster_items: list[dict] = []
    index_to_item: dict[int, dict] = {}
    for root in sorted(pass1["clusters"], key=lambda r: signatures[r]):
        members = pass1["clusters"][root]
        merged = _merge_cluster(members, records, identities, signatures,
                                pass1["applied_edges"])
        item = {
            "record": merged,
            "members": members,
            "identity": identities[_base_index(members, records, signatures)],
            "cluster_signature": "|".join(sorted(signatures[i] for i in members)),
        }
        cluster_items.append(item)
        for member in members:
            index_to_item[member] = item

    _assign_practice_ids(cluster_items, shared_domains)

    # Review-queue pairs: scored 4-5, or blocked by the unit gate. Recorded on
    # both survivors so a near-match is visible to an analyst, never silently
    # dropped and never silently merged.
    review_pairs = 0
    for score, matched, i, j in pass1["review_edges"]:
        left, right = index_to_item[i], index_to_item[j]
        if left is right:
            continue
        review_pairs += 1
        for source, other in ((left, right), (right, left)):
            candidates = source["record"]["consolidation"]["review_candidates"]
            entry = {
                "practice_id": other["practice_id"],
                "score": score,
                "matched_fields": matched,
            }
            if entry not in candidates:
                candidates.append(entry)

    consolidated: list[dict] = []
    for item in cluster_items:
        record = item["record"]
        record["practice_id"] = item["practice_id"]
        # practice_id is the record's identity from here on: it is deterministic
        # and stable across runs, where the ingest id was per-source-row.
        record["id"] = item["practice_id"]
        record["consolidation"]["review_candidates"].sort(
            key=lambda c: (-c["score"], c["practice_id"])
        )
        consolidated.append(record)

    consolidated.sort(key=lambda r: str(r.get("practice_id") or ""))
    multi_location_groups = link_location_groups(consolidated, shared_domains)

    merged_groups = sum(1 for item in cluster_items if len(item["members"]) > 1)
    return consolidated, {
        "enabled": True,
        "input_count": len(records),
        "output_count": len(consolidated),
        "merged_groups": merged_groups,
        "rows_merged_away": len(records) - len(consolidated),
        "review_pairs": review_pairs,
        "unblocked_count": pass1["unblocked"],
        "multi_location_groups": multi_location_groups,
    }
