"""
Tests for subpage discovery and value-ranked selection in the web extractor.

Every non-noise internal page is eligible to be crawled; keyword matches set the
ORDER so the strongest-evidence pages (services, procedures, provider bios,
billing) come first, within a text budget and a high safety ceiling. The crawler
must never spend a slot on blog/news/legal/auth/commerce noise.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from extraction.web_extractor import _find_relevant_subpages, _same_registered_domain

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


def test_billing_insurance_page_selected_over_admin_by_default():
    """A billing/insurance page is crawled by default and outranks about/contact.

    Cash-pay and self-pay evidence lives on the Billing & Insurance page, not the
    homepage. Regression guard for the missed cash-pay gate on those pages.
    """
    html = _html(
        '<a href="/about">About Us</a>',
        '<a href="/billing-and-insurance">Billing and Insurance</a>',
    )
    # One subpage slot: the financial page (tier 2) must beat the about page (tier 1).
    result = _find_relevant_subpages(html, BASE, max_pages=2)
    assert result == [f"{BASE}/billing-and-insurance"]


def test_clinical_page_outranks_billing_page():
    """Clinical evidence still wins the first slot: a services page (tier 3)
    outranks a billing page (tier 2) when only one subpage fits."""
    html = _html(
        '<a href="/billing-and-insurance">Billing and Insurance</a>',
        '<a href="/services">Our Services</a>',
    )
    result = _find_relevant_subpages(html, BASE, max_pages=2)
    assert result == [f"{BASE}/services"]


def test_unkeyworded_page_is_crawled_but_ranked_last():
    """Every non-noise page is eligible: a page matching no keyword is still
    crawled, just ranked after keyword-matched evidence pages."""
    html = _html(
        '<a href="/patient-forms">Patient Forms</a>',   # matches no keyword
        '<a href="/services">Services</a>',              # tier 3
    )
    result = _find_relevant_subpages(html, BASE, max_pages=5)
    assert f"{BASE}/patient-forms" in result            # crawled (new behavior)
    assert result.index(f"{BASE}/services") < result.index(f"{BASE}/patient-forms")


def test_noise_pages_are_skipped():
    """Blog/news plus legal/auth/commerce noise pages are never crawled, even
    though every other page is now eligible."""
    html = _html(
        '<a href="/privacy-policy">Privacy Policy</a>',
        '<a href="/login">Patient Login</a>',
        '<a href="/cart">Cart</a>',
        '<a href="/sitemap">Sitemap</a>',
        '<a href="/blog/iui">Blog Post</a>',
        '<a href="/services">Services</a>',
    )
    result = _find_relevant_subpages(html, BASE, max_pages=10)
    assert result == [f"{BASE}/services"]


def test_operator_keywords_augment_defaults():
    """A specialty-only keyword list does not drop the generic page-type defaults:
    a billing page still outranks a plain about page even when the operator passes
    only specialty terms (regression guard for the cash-pay miss on client ICPs)."""
    html = _html(
        '<a href="/about">About</a>',
        '<a href="/billing-and-insurance">Billing and Insurance</a>',
    )
    result = _find_relevant_subpages(html, BASE, max_pages=2, keywords=["iui", "infertil"])
    assert result == [f"{BASE}/billing-and-insurance"]


# ---------------------------------------------------------------------------
# Off-domain redirect guard (subpage text must stay on the practice's domain)
# ---------------------------------------------------------------------------

def test_same_domain_www_and_subdomain_accepted():
    assert _same_registered_domain("https://clinic.com/a", "https://www.clinic.com/b")
    assert _same_registered_domain("https://booking.clinic.com/x", "https://clinic.com/")


def test_off_domain_redirect_rejected():
    # A subpage that redirects to a pay portal or social page is a different
    # registered domain and must not become practice evidence.
    assert not _same_registered_domain("https://pay.instamed.com/x", "https://clinic.com/")
    assert not _same_registered_domain("https://facebook.com/clinic", "https://clinic.com/")


def test_fetch_html_resniffs_encoding_when_header_lacks_charset(monkeypatch):
    """A header-less UTF-8 page must not be decoded as ISO-8859-1 (mojibake)."""
    import extraction.web_extractor as we

    class FakeResp:
        def __init__(self):
            self.headers = {"content-type": "text/html"}  # no charset
            self.url = "https://clinic.com/"
            self.encoding = "ISO-8859-1"  # requests' header-less default
        @property
        def apparent_encoding(self):
            return "utf-8"
        @property
        def text(self):
            return "Dr. José — Women’s Health" if self.encoding.lower() == "utf-8" else "Dr. JosÃ©"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(we.requests, "get", lambda *a, **k: FakeResp())
    html, final, err = we._fetch_html("https://clinic.com/", retries=0)
    assert err == ""
    assert "José" in html
