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


# A unit fragment can carry more than one designator ("Building A, Suite 360").
# PRIMARY designators identify the tenant's own space; a floor is implied by a
# suite number, so it is dropped when a primary is present ("Suite 360, 3rd Floor"
# is Suite 360). Sorting by rank makes the output order-independent, so
# "Building A, Suite 360" and "Suite 360, Building A" compare equal.
_PRIMARY_UNIT_DESIGNATORS: frozenset[str] = frozenset(
    {"suite", "apt", "unit", "room", "office"}
)
_DESIGNATOR_RANK: dict[str, int] = {
    "bldg": 0, "suite": 1, "apt": 1, "unit": 1, "room": 1, "office": 1,
    "floor": 2, "dept": 3,
}
_CANONICAL_DESIGNATORS: frozenset[str] = frozenset(_UNIT_DESIGNATORS.values())


_ORDINAL_RE = re.compile(r"^(\d+)(st|nd|rd|th)$")


def _normalize_unit_value_token(token: str) -> str:
    """Ordinals and cardinals name the same place: "3rd" and "3" are one floor."""
    match = _ORDINAL_RE.match(token)
    return match.group(1) if match else token


def _designator_first(tokens: list[str]) -> list[str]:
    """Rewrite English value-first forms ("3rd Floor") as designator-first.

    Units are written both ways in the same column. Without this, "Suite 360,
    3rd Floor" keeps "3rd" inside the suite value and never equals "Suite 360".
    """
    out: list[str] = []
    for i, token in enumerate(tokens):
        following_is_value = i + 1 < len(tokens) and _is_unit_value(tokens[i + 1])
        if (token in _UNIT_DESIGNATORS and not following_is_value
                and out and _is_unit_value(out[-1])):
            out.insert(len(out) - 1, token)
            continue
        out.append(token)
    return out


def _unit_components(tokens: list[str]) -> list[tuple[str, str]]:
    """Split a unit fragment into canonical (designator, value) components.

    A stray "#" inside a value is dropped: _tokenize_address isolates it so it can
    be recognised as a designator, which left "SUITE #114" as "suite # 114" and
    compared unequal to "suite 114" — the same suite, written two ways.
    """
    components: list[tuple[str, list[str]]] = []
    for token in _designator_first(tokens):
        canonical = _UNIT_DESIGNATORS.get(token)
        if canonical and not (token == "#" and components):
            components.append((canonical, []))
        elif token == "#":
            continue                      # stray separator, never part of a value
        elif components:
            components[-1][1].append(_normalize_unit_value_token(token))
        else:
            # Bare value, e.g. "200", takes the default designator.
            components.append(("suite", [_normalize_unit_value_token(token)]))
    return [(designator, _collapse(" ".join(value)))
            for designator, value in components if value]


def _normalize_unit_tokens(tokens: list[str]) -> str:
    """Canonicalize a unit fragment, e.g. ['ste','200'] -> 'suite 200'."""
    components = _unit_components(tokens)
    if not components:
        return ""
    if any(d in _PRIMARY_UNIT_DESIGNATORS for d, _ in components):
        components = [c for c in components if c[0] != "floor"]
    components.sort(key=lambda c: (_DESIGNATOR_RANK.get(c[0], 9), c[0], c[1]))
    return _collapse(" ".join(f"{d} {v}" for d, v in components))


def normalize_address_street(raw_street: str) -> str:
    """Normalized street line with any unit removed."""
    return split_street_and_unit(raw_street)[0]


def normalize_address_unit(raw_unit: str) -> str:
    """Normalize a unit that arrived in its own column (e.g. 'Ste. 200').

    A bare value such as "200" is still a unit and takes the default designator.
    """
    return _normalize_unit_tokens(_tokenize_address(raw_unit))


def normalize_zip5(raw_zip: str) -> str:
    """First five digits of a postal code, or "" when there are fewer than five."""
    digits = re.sub(r"\D", "", raw_zip or "")
    return digits[:5] if len(digits) >= 5 else ""


# An extension is written a dozen ways and its digits are not part of the number.
# Left in place they shift the last-ten window: "(916) 431-0860 ext. 4" became
# 1643108604, a number that exists nowhere and compares unequal to itself.
# "x4" has no word boundary between the x and the digit, so the x alternative
# anchors only on its left — which also stops it matching the x inside "fax".
_EXTENSION_RE = re.compile(
    r"(?:\b(?:ext|extension|xt)\b\.?|\bx|#)\s*\.?\s*\d+\s*$", re.IGNORECASE
)

# NANP structure. A number failing this cannot be dialled, so it is not evidence
# of anything — see normalize_phone.
_NANP_RE = re.compile(r"^[2-9](?!11)\d{2}[2-9](?!11)\d{2}\d{4}$")


def strip_phone_extension(raw_phone: str) -> str:
    """Remove a trailing extension so its digits cannot enter the number."""
    return _EXTENSION_RE.sub("", (raw_phone or "").strip()).strip(" -.,;:")


def is_dialable_phone(digits: str) -> bool:
    """True when ten digits satisfy NANP structure for a subscriber line.

    Area code and exchange must start 2-9, and neither may be an N11 service
    code (911, 411 and friends are not exchanges).
    """
    return bool(_NANP_RE.match(digits or ""))


def normalize_phone(raw_phone: str) -> str:
    """Comparable ten-digit phone, or "" when the input cannot be dialled.

    A structurally impossible number is returned as EMPTY rather than as a
    distinct value, and the asymmetry is the whole reason: a corrupted number
    silently argues two locations are different, while an absent one argues
    nothing. Absence is the safe default because the failure modes are not
    symmetric.
    """
    digits = re.sub(r"\D", "", strip_phone_extension(raw_phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) < 10:
        return ""
    candidate = digits[-10:]
    return candidate if is_dialable_phone(candidate) else ""


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
