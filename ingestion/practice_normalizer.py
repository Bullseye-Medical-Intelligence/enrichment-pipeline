"""
practice_normalizer.py
Shared normalization for practice-location consolidation (ingest Passes 1 and 2).

Every comparison key used by the consolidator is produced here, so the merge
rule, the group rule, and the deterministic practice_id all read the same
normalized values. Pure functions: no I/O, no config, no network.

The address split is the important one. `split_street_and_unit` returns the
street and the unit as TWO fields and never folds the unit into the street.
The unit is the primary guard against over-merging: two independent practices
at one street address in different suites must never collapse, so the unit has
to survive normalization as its own comparable value.

This module is intentionally separate from `discovery/matcher.py`. That module
is a hand-maintained parity twin of `pipeline-api/practice_matching.py` (guarded
by tests/test_matching_parity.py) and its normalization is deliberately looser —
it has no unit parsing, no USPS expansion, and treats a domain as its bare
hostname rather than eTLD+1. Editing it to serve consolidation would break that
parity contract, so consolidation gets its own stricter normalizer.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from ingestion.public_suffix_list import public_suffix_of

# ---------------------------------------------------------------------------
# USPS street abbreviations (C1 publication 28, the subset that appears in
# scraped US practice addresses). Expanded so "123 Main St" and "123 Main
# Street" produce one key.
# ---------------------------------------------------------------------------

_STREET_SUFFIXES: dict[str, str] = {
    "st": "street", "str": "street",
    "ave": "avenue", "av": "avenue", "aven": "avenue",
    "blvd": "boulevard", "blv": "boulevard", "boul": "boulevard",
    "rd": "road", "dr": "drive", "drv": "drive",
    "ln": "lane", "ct": "court", "crt": "court",
    "pl": "place", "plz": "plaza", "sq": "square",
    "ter": "terrace", "terr": "terrace",
    "pkwy": "parkway", "pky": "parkway", "pkway": "parkway",
    "cir": "circle", "circ": "circle",
    "hwy": "highway", "hway": "highway",
    "trl": "trail", "expy": "expressway", "fwy": "freeway",
    "tpke": "turnpike", "aly": "alley", "brg": "bridge",
    "cswy": "causeway", "ctr": "center", "cntr": "center",
    "crk": "creek", "xing": "crossing", "gdns": "gardens", "gdn": "garden",
    "gln": "glen", "hl": "hill", "hls": "hills", "jct": "junction",
    "lk": "lake", "mtn": "mountain", "pt": "point", "rdg": "ridge",
    "riv": "river", "rte": "route", "spg": "spring", "sta": "station",
    "vly": "valley", "vw": "view", "wlk": "walk", "ext": "extension",
}

_DIRECTIONALS: dict[str, str] = {
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
    "no": "north", "so": "south",
}

# Unit designators, mapped to a canonical token. "#" and "no"/"number" resolve
# to "suite": in commercial medical addressing they are the same thing, so
# "Suite 200" and "#200" must produce one key rather than two.
_UNIT_DESIGNATORS: dict[str, str] = {
    "suite": "suite", "ste": "suite", "#": "suite",
    "number": "suite", "num": "suite",
    "apt": "apt", "apartment": "apt",
    "unit": "unit",
    "floor": "floor", "fl": "floor", "flr": "floor",
    "bldg": "bldg", "building": "bldg", "bld": "bldg",
    "rm": "room", "room": "room",
    "dept": "dept", "department": "dept",
    "office": "office", "ofc": "office",
}

# Legal-entity and clinical-credential tokens stripped from a practice name so
# "Valley OBGYN, PLLC" and "Valley OBGYN" compare equal.
_NAME_NOISE_TOKENS: frozenset[str] = frozenset({
    "llc", "lc", "pllc", "pa", "pc", "plc", "llp", "lp", "ltd",
    "inc", "incorporated", "corp", "corporation", "co", "company",
    "dba", "the", "and",
    "md", "do", "dds", "dmd", "od", "dpm", "dc", "np", "pac", "rn",
    "phd", "psyd", "mbbs", "facog", "faap", "facs", "facc", "facp", "faan",
})

_PUNCTUATION_RE = re.compile(r"[^\w\s#]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_DIGIT_RE = re.compile(r"\d")


def _collapse(text: str) -> str:
    """Collapse runs of whitespace and trim."""
    return _WHITESPACE_RE.sub(" ", text or "").strip()


def _tokenize_address(raw: str) -> list[str]:
    """Lowercase, isolate '#', drop other punctuation, split into tokens."""
    text = (raw or "").lower().replace("#", " # ")
    text = _PUNCTUATION_RE.sub(" ", text)
    return _collapse(text).split()


def _is_unit_value(token: str) -> bool:
    """True when a token can be a unit value (contains a digit, or is a lone letter)."""
    return bool(_DIGIT_RE.search(token)) or len(token) == 1


def split_street_and_unit(raw_street: str) -> tuple[str, str]:
    """Split a raw street line into (normalized_street, normalized_unit).

    The unit is returned separately and is NEVER folded back into the street:
    it is what stops two practices in one building from merging. Returns
    ("", "") for empty input, and a "" unit when the line carries none.

    A designator only opens the unit when it is actually followed by a value
    ("ste 200", "# 4", "floor 2"), so a street such as "123 North Road" is not
    mistaken for a unit on its directional token.
    """
    tokens = _tokenize_address(raw_street)
    if not tokens:
        return "", ""

    split_at = None
    for i, token in enumerate(tokens):
        if token not in _UNIT_DESIGNATORS:
            continue
        # A designator needs a following value token to count as a unit.
        if i + 1 < len(tokens) and _is_unit_value(tokens[i + 1]):
            split_at = i
            break

    if split_at is None:
        return _expand_street_tokens(tokens), ""

    street_tokens = tokens[:split_at]
    unit_tokens = tokens[split_at:]
    return _expand_street_tokens(street_tokens), _normalize_unit_tokens(unit_tokens)


def _expand_street_tokens(tokens: list[str]) -> str:
    """Expand USPS abbreviations and directionals across street tokens."""
    if not tokens:
        return ""
    out: list[str] = []
    last = len(tokens) - 1
    for i, token in enumerate(tokens):
        if token == "st" and i == 0:
            out.append("saint")          # leading "St Mary's", not a suffix
            continue
        if token in _STREET_SUFFIXES and i > 0:
            out.append(_STREET_SUFFIXES[token])
            continue
        # Directionals expand anywhere except the final slot, where a bare
        # letter is far more likely a suffix abbreviation already handled above.
        if token in _DIRECTIONALS and i != last:
            out.append(_DIRECTIONALS[token])
            continue
        if token in _DIRECTIONALS and i == last:
            out.append(_DIRECTIONALS[token])
            continue
        out.append(token)
    return _collapse(" ".join(out))


def _normalize_unit_tokens(tokens: list[str]) -> str:
    """Canonicalize a unit fragment, e.g. ['ste','200'] -> 'suite 200'."""
    if not tokens:
        return ""
    designator = _UNIT_DESIGNATORS.get(tokens[0], tokens[0])
    value = " ".join(tokens[1:])
    return _collapse(f"{designator} {value}")


def normalize_address_street(raw_street: str) -> str:
    """Normalized street line with any unit removed."""
    return split_street_and_unit(raw_street)[0]


def normalize_address_unit(raw_unit: str) -> str:
    """Normalize a unit that arrived in its own column (e.g. 'Ste. 200')."""
    tokens = _tokenize_address(raw_unit)
    if not tokens:
        return ""
    if tokens[0] in _UNIT_DESIGNATORS:
        return _normalize_unit_tokens(tokens)
    # A bare value such as "200" is still a unit; give it the default designator.
    return _collapse(f"suite {' '.join(tokens)}")


def normalize_zip5(raw_zip: str) -> str:
    """First five digits of a postal code, or "" when there are fewer than five."""
    digits = re.sub(r"\D", "", raw_zip or "")
    return digits[:5] if len(digits) >= 5 else ""


def normalize_phone(raw_phone: str) -> str:
    """Digits only, US country code dropped, last 10 kept. "" when under 10."""
    digits = re.sub(r"\D", "", raw_phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else ""


def registrable_domain(raw_url: str) -> str:
    """Registrable domain (eTLD+1) of a URL or bare host, lowercase, no www.

    Uses the vendored public-suffix table so "practice.co.uk" resolves to
    "practice.co.uk" rather than "co.uk". Returns "" when there is no host or
    the host is a bare public suffix.
    """
    text = (raw_url or "").strip().lower()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        host = urlsplit(text).netloc
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0].strip().strip(".")
    if not host or host.startswith("www.") and host == "www.":
        return ""
    if host.startswith("www."):
        host = host[4:]
    labels = [p for p in host.split(".") if p]
    if len(labels) < 2:
        return ""

    suffix = public_suffix_of(host)
    if suffix:
        suffix_labels = suffix.count(".") + 1
        if len(labels) <= suffix_labels:
            return ""            # bare public suffix, not registrable
        return ".".join(labels[-(suffix_labels + 1):])
    return ".".join(labels[-2:])


def normalize_practice_name(raw_name: str) -> str:
    """Lowercase name with legal-entity and credential tokens removed."""
    text = (raw_name or "").lower()
    text = _PUNCTUATION_RE.sub(" ", text.replace("#", " "))
    tokens = [t for t in _collapse(text).split() if t and t not in _NAME_NOISE_TOKENS]
    return _collapse(" ".join(tokens))


def stable_hash(*parts: str) -> str:
    """Deterministic short digest over the given parts — no time, no randomness."""
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def identity_of(record: dict) -> dict:
    """Build the normalized comparison keys for one input record.

    Reads the canonical ingest fields only. `address_unit` is taken from its own
    column when present and otherwise parsed out of the street line, so a unit
    is captured either way.
    """
    street_raw = (
        record.get("address_street")
        or record.get("address_line1")
        or record.get("address_full")
        or ""
    )
    street, parsed_unit = split_street_and_unit(street_raw)
    explicit_unit = normalize_address_unit(record.get("address_unit") or "")
    return {
        "street": street,
        "unit": explicit_unit or parsed_unit,
        "zip5": normalize_zip5(record.get("address_zip") or ""),
        "phone": normalize_phone(record.get("phone") or ""),
        "domain": registrable_domain(record.get("website_url") or ""),
        "name": normalize_practice_name(record.get("practice_name") or ""),
    }
