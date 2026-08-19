"""
tests/test_browser_proxy.py
PIPELINE_BROWSER_PROXY parsing for the browser crawler — pure env-to-settings
mapping, no Playwright launch, no network.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from extraction.playwright_extractor import _proxy_settings_from_env  # noqa: E402


def test_unset_means_no_proxy(monkeypatch):
    monkeypatch.delenv("PIPELINE_BROWSER_PROXY", raising=False)
    assert _proxy_settings_from_env() is None


def test_vendor_url_with_credentials_splits_cleanly(monkeypatch):
    monkeypatch.setenv("PIPELINE_BROWSER_PROXY", "http://cust-abc:s3cret@gate.provider.com:7000")
    settings = _proxy_settings_from_env()
    assert settings == {
        "server": "http://gate.provider.com:7000",
        "username": "cust-abc",
        "password": "s3cret",
    }
    # Credentials must never leak into the server string Playwright logs.
    assert "s3cret" not in settings["server"]


def test_bare_host_port_assumes_http(monkeypatch):
    monkeypatch.setenv("PIPELINE_BROWSER_PROXY", "gate.provider.com:7000")
    assert _proxy_settings_from_env() == {"server": "http://gate.provider.com:7000"}


def test_percent_encoded_password_is_decoded(monkeypatch):
    monkeypatch.setenv("PIPELINE_BROWSER_PROXY", "http://user:p%40ss@proxy.example:8000")
    assert _proxy_settings_from_env()["password"] == "p@ss"


def test_no_credentials_omits_the_fields(monkeypatch):
    monkeypatch.setenv("PIPELINE_BROWSER_PROXY", "http://proxy.example:8000")
    settings = _proxy_settings_from_env()
    assert "username" not in settings and "password" not in settings


def test_malformed_values_degrade_to_direct(monkeypatch):
    for bad in ("http://", "http://host:notaport", "   "):
        monkeypatch.setenv("PIPELINE_BROWSER_PROXY", bad)
        assert _proxy_settings_from_env() is None, f"expected None for {bad!r}"
