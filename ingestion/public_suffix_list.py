"""
public_suffix_list.py
Vendored, offline public-suffix data for registrable-domain (eTLD+1) extraction.

Deliberately a static table, not a library: consolidation runs inside the
pipeline and the test suite is hermetic, so nothing here may fetch the IANA list
at runtime or at test time. There is no network access in this module and no
dependency that performs one.

Scope: the ICANN multi-label suffixes a US healthcare prospect list realistically
contains, plus the major international ones so a foreign-hosted site is not
mis-parsed. Single-label suffixes (com, org, net, io, ...) need no table — they
are handled by the default "last two labels" rule in
practice_normalizer.registrable_domain.

To extend: add the suffix (without a leading dot) to MULTI_LABEL_SUFFIXES. Keep
entries lowercase. Hosting and directory domains do NOT belong here — those are
aggregator concerns, handled by the consolidation denylist, because they are
registrable domains that merely happen to be shared.
"""

from __future__ import annotations

# Public suffixes made of two or more labels. A domain whose trailing labels
# match one of these needs one MORE label to become registrable.
MULTI_LABEL_SUFFIXES: frozenset[str] = frozenset({
    # United Kingdom
    "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk",
    "ac.uk", "gov.uk", "nhs.uk", "police.uk", "mod.uk",
    # Ireland
    "co.ie",
    # Australia
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    # New Zealand
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz", "health.nz", "school.nz",
    # South Africa
    "co.za", "org.za", "net.za", "gov.za", "ac.za", "web.za",
    # Brazil
    "com.br", "net.br", "org.br", "gov.br", "edu.br",
    # Mexico
    "com.mx", "org.mx", "net.mx", "gob.mx", "edu.mx",
    # Argentina / Peru / Colombia / Chile
    "com.ar", "net.ar", "org.ar", "gob.ar",
    "com.pe", "org.pe", "net.pe", "gob.pe",
    "com.co", "net.co", "nom.co", "org.co",
    "gob.cl",
    # Japan
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "lg.jp", "ed.jp",
    # South Korea
    "co.kr", "or.kr", "ne.kr", "go.kr", "re.kr", "ac.kr",
    # China / Hong Kong / Taiwan
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.hk", "org.hk", "net.hk", "edu.hk", "gov.hk",
    "com.tw", "org.tw", "net.tw", "edu.tw", "gov.tw",
    # India
    "co.in", "net.in", "org.in", "gen.in", "firm.in", "ind.in",
    "gov.in", "ac.in", "edu.in", "res.in",
    # Singapore / Malaysia / Philippines / Indonesia / Thailand
    "com.sg", "net.sg", "org.sg", "edu.sg", "gov.sg",
    "com.my", "net.my", "org.my", "edu.my", "gov.my",
    "com.ph", "net.ph", "org.ph", "edu.ph", "gov.ph",
    "co.id", "or.id", "ac.id", "go.id",
    "co.th", "or.th", "ac.th", "go.th", "in.th",
    # Israel / Turkey / UAE / Saudi
    "co.il", "org.il", "net.il", "ac.il", "gov.il",
    "com.tr", "net.tr", "org.tr", "edu.tr", "gov.tr",
    "co.ae", "net.ae", "org.ae", "ac.ae", "gov.ae",
    "com.sa", "net.sa", "org.sa", "edu.sa", "gov.sa",
    # Europe (multi-label cases only)
    "co.at", "or.at", "ac.at", "gv.at",
    "com.es", "org.es", "nom.es", "edu.es", "gob.es",
    "com.pl", "net.pl", "org.pl", "edu.pl", "gov.pl",
    "com.pt", "org.pt", "edu.pt", "gov.pt",
    "com.gr", "net.gr", "org.gr", "edu.gr", "gov.gr",
    "com.ua", "net.ua", "org.ua", "edu.ua", "gov.ua",
    "com.ru", "net.ru", "org.ru", "edu.ru", "gov.ru",
    # United States — the state second-levels a clinic site might use
    "k12.ca.us", "k12.ny.us", "k12.tx.us", "k12.fl.us",
    "state.ca.us", "state.ny.us", "state.tx.us", "state.fl.us",
    "ci.la.ca.us",
})

# The longest suffix in the table, in labels — bounds the lookup loop.
MAX_SUFFIX_LABELS: int = max(s.count(".") + 1 for s in MULTI_LABEL_SUFFIXES)


def public_suffix_of(host: str) -> str:
    """Return the public suffix for a lowercase hostname, or "" when unknown.

    Longest match wins, so "k12.ca.us" is preferred over "state.ca.us"-style
    shorter overlaps. Returns "" for a host whose trailing labels match no
    multi-label entry — the caller then applies the single-label default.
    """
    labels = [p for p in (host or "").split(".") if p]
    for size in range(min(MAX_SUFFIX_LABELS, len(labels)), 1, -1):
        candidate = ".".join(labels[-size:])
        if candidate in MULTI_LABEL_SUFFIXES:
            return candidate
    return ""
