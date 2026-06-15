"""
Tests for subpage discovery and value-ranked selection in the web extractor.

The crawler has a limited per-practice page budget, so it must spend it on the
strongest-evidence pages (services, procedures, provider bios) before weaker
ones (about, contact) and must never spend it on blog/news content.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from extraction.web_extractor import _find_relevant_subpages

BASE = "https://clinic.example"


def _html(*links: str) -> str:
    """Wrap anchor tags in a minimal HTML document."""
    return f"<html><body><nav>{''.join(links)}</nav></body></html>"


def test_service_page_outranks_keyword_rich_admin_page():
    """A focused /procedures page beats a slug matching about+care+team."""
    html = _html(
        '<a href="/about-our-care-team">About Our Care Team</a>',
        '<a href="/procedures">Procedures</a>',
    )
    # max_pages=2 reserves one slot for the homepage, leaving one subpage slot.
    result = _find_relevant_subpages(html, BASE, max_pages=2)
    assert result == [f"{BASE}/procedures"]


def test_blog_pages_excluded_even_when_slug_matches_keyword():
    """A blog post whose slug matches a specialty keyword is not crawled."""
    keywords = ["service", "iui"]
    html = _html(
        '<a href="/blog/iui-explained">IUI Explained</a>',
        '<a href="/services">Our Services</a>',
    )
    result = _find_relevant_subpages(html, BASE, max_pages=5, keywords=keywords)
    assert f"{BASE}/services" in result
    assert all("/blog/" not in u for u in result)


def test_specialty_keyword_outranks_admin_page():
    """An operator specialty keyword defaults to the high tier, beating contact."""
    keywords = ["contact", "infertil"]
    html = _html(
        '<a href="/contact">Contact</a>',
        '<a href="/infertility">Infertility</a>',
    )
    result = _find_relevant_subpages(html, BASE, max_pages=2, keywords=keywords)
    assert result == [f"{BASE}/infertility"]


def test_match_count_breaks_ties_within_same_tier():
    """Same top tier: the page matching more keywords ranks first."""
    html = _html(
        '<a href="/services">Services</a>',
        '<a href="/services-and-procedures">Services and Procedures</a>',
    )
    result = _find_relevant_subpages(html, BASE, max_pages=5)
    assert result[0] == f"{BASE}/services-and-procedures"


def test_reserves_one_slot_for_homepage():
    """Never returns more than max_pages - 1 subpages."""
    links = [f'<a href="/services-{i}">Services {i}</a>' for i in range(10)]
    result = _find_relevant_subpages(_html(*links), BASE, max_pages=4)
    assert len(result) == 3


def test_offsite_anchor_and_mailto_links_ignored():
    """Cross-domain, anchor, and mailto links are never candidates."""
    html = _html(
        '<a href="https://other.example/services">Off-site</a>',
        '<a href="#services">Anchor</a>',
        '<a href="mailto:x@clinic.example">Email</a>',
        '<a href="/services">Services</a>',
    )
    result = _find_relevant_subpages(html, BASE, max_pages=5)
    assert result == [f"{BASE}/services"]
