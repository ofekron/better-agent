"""Unit coverage for credential_broker/manifest.py — provider host pinning.

The pin is the credential broker's first line of defense against a
malicious/compromised provider redirecting a secret to an attacker host:
a request whose computed host is not on the provider's allowed_sinks pin
is rejected BEFORE a consent is created. Fail-closed: empty/missing pin
or host matches nothing.

descriptor validation (test_credential_broker.py) already exercises the
exact-match happy path and the PinViolation path indirectly; this module
owns every branch of host_allowed/enforce directly — especially the
wildcard semantics, where ``*.github.com`` must match subdomains but NOT
``github.com`` itself.
"""
from __future__ import annotations

import pytest

from credential_broker import manifest


# ── fail-closed: empty / missing inputs ─────────────────────────


def test_host_allowed_rejects_empty_host():
    assert manifest.host_allowed("", ["api.github.com"]) is False


def test_host_allowed_rejects_none_host():
    assert manifest.host_allowed(None, ["api.github.com"]) is False  # type: ignore[arg-type]


def test_host_allowed_rejects_empty_pin():
    assert manifest.host_allowed("api.github.com", []) is False


def test_host_allowed_rejects_none_pin():
    assert manifest.host_allowed("api.github.com", None) is False  # type: ignore[arg-type]


# ── exact match ─────────────────────────────────────────────────


def test_host_allowed_exact_match():
    assert manifest.host_allowed("api.github.com", ["api.github.com"]) is True


def test_host_allowed_is_case_insensitive():
    assert manifest.host_allowed("API.GitHub.Com", ["api.github.com"]) is True


def test_host_allowed_strips_pin_whitespace():
    assert manifest.host_allowed("api.github.com", ["  api.github.com  "]) is True


def test_host_allowed_no_match():
    assert manifest.host_allowed("api.example.com", ["api.github.com"]) is False


# ── wildcard match ──────────────────────────────────────────────


def test_host_allowed_wildcard_matches_subdomain():
    assert manifest.host_allowed("api.github.com", ["*.github.com"]) is True


def test_host_allowed_wildcard_matches_deep_subdomain():
    assert manifest.host_allowed("a.b.github.com", ["*.github.com"]) is True


def test_host_allowed_wildcard_does_not_match_base_domain():
    # *.github.com must NOT match github.com itself — the self-exclusion
    # that prevents a wildcard pin from accidentally admitting the apex.
    assert manifest.host_allowed("github.com", ["*.github.com"]) is False


def test_host_allowed_wildcard_rejects_unrelated_host():
    assert manifest.host_allowed("api.example.com", ["*.github.com"]) is False


# ── malformed pin entries are skipped, not fatal ────────────────


def test_host_allowed_skips_non_string_pin_entries():
    # A garbage entry must not crash or short-circuit; matching continues.
    assert manifest.host_allowed("api.github.com", [123, "api.github.com"]) is True


def test_host_allowed_skips_empty_pin_entries():
    assert manifest.host_allowed("api.github.com", ["", "api.github.com"]) is True


def test_host_allowed_returns_false_when_all_entries_garbage():
    assert manifest.host_allowed("api.github.com", ["", 0, None]) is False


# ── enforce ─────────────────────────────────────────────────────


def test_enforce_allows_pinned_host():
    # No exception raised.
    manifest.enforce("api.github.com", ["api.github.com"])


def test_enforce_raises_for_unpinned_host():
    with pytest.raises(manifest.PinViolation, match="not on the provider's allowed_sinks pin"):
        manifest.enforce("evil.example.com", ["api.github.com"])


def test_enforce_raises_when_pin_empty():
    with pytest.raises(manifest.PinViolation):
        manifest.enforce("api.github.com", [])
