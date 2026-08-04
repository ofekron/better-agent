"""Full unit coverage for ``marketplace_auth.py``.

``service_name`` derives the macOS Keychain service name for the marketplace
OAuth account. It is unsuffixed for the default Better Agent home (so legacy
keychain entries keep working) and suffixed with a sha256 digest of the
resolved home when ``BETTER_AGENT_HOME`` points elsewhere, so each instance
owns its own marketplace auth.

The module is dependency-light (``hashlib`` + ``paths`` + ``keychain_names``),
so both branches are exercised in-process: the empty-suffix branch via a
monkeypatched ``home_suffix``, and the digest branch via an isolated
``BETTER_AGENT_HOME`` tempdir that drives the real ``home_suffix`` end-to-end.
"""

from __future__ import annotations

import hashlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import marketplace_auth as ma  # noqa: E402
import paths  # noqa: E402


def test_default_home_returns_unsuffixed_prefix(monkeypatch):
    # Empty home_suffix -> legacy unsuffixed service name, no digest work.
    monkeypatch.setattr(ma, "home_suffix", lambda: "")
    assert ma.service_name() == "better-agent-marketplace"


def test_isolated_home_returns_sha256_suffixed_prefix(monkeypatch, tmp_path):
    # A non-default BETTER_AGENT_HOME makes home_suffix() return a non-empty
    # slug, so service_name must append the sha256 digest of the resolved
    # home. Drive the real home_suffix end-to-end (no monkeypatch) and assert
    # the digest matches the same ba_home() the module reads.
    home = tmp_path / "mp-auth-home"
    home.mkdir()
    monkeypatch.setenv("BETTER_AGENT_HOME", str(home))

    # Sanity: the isolation actually produces a non-empty suffix, otherwise
    # we would be testing the wrong branch without knowing it.
    assert ma.home_suffix(), "isolated home should yield a non-empty suffix"

    expected_digest = hashlib.sha256(
        str(paths.ba_home().resolve()).encode()
    ).hexdigest()
    assert ma.service_name() == f"better-agent-marketplace-{expected_digest}"


def test_suffix_is_full_hexdigest_and_prefix_constant(monkeypatch, tmp_path):
    # Pin the digest shape (full 64-char sha256 hex) and the prefix constant
    # so a future change to a truncated/fuzzy digest is caught.
    home = tmp_path / "mp-auth-home2"
    home.mkdir()
    monkeypatch.setenv("BETTER_AGENT_HOME", str(home))
    name = ma.service_name()
    assert name.startswith("better-agent-marketplace-")
    digest = name[len("better-agent-marketplace-"):]
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_distinct_homes_yield_distinct_service_names(monkeypatch, tmp_path):
    # Two different isolated homes must not collide on the marketplace
    # service name (the whole point of per-instance suffixing).
    names = []
    for sub in ("home-a", "home-b"):
        h = tmp_path / sub
        h.mkdir()
        monkeypatch.setenv("BETTER_AGENT_HOME", str(h))
        names.append(ma.service_name())
    assert names[0] != names[1]
    assert all(n.startswith("better-agent-marketplace-") for n in names)


def test_auth_account_constant():
    # Pin the keychain account name used by the marketplace OAuth flow.
    assert ma.AUTH_ACCOUNT == "oauth-session"
