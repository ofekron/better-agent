"""Unit tests for ``keychain_names`` service-name resolution.

Pure-logic module with one ``ba_home``-dependent function (``home_suffix``),
which is driven here by monkeypatching ``paths.ba_home`` /
``paths._default_home`` so no real filesystem layout is required.

Pins:
  1. ``service_names``: defaults, per-argument defaults, same-name dedup,
     empty-string skipping, ordering preservation.
  2. ``home_suffix``: default-home -> ``""``, distinct-home slug folding,
     all-symbol path -> ``""``, 63-char truncation, and the
     ``paths``-import/resolve failure -> ``""`` branch.
  3. ``auth_services``: home-scoped suffix threaded into both service names;
     no-suffix falls back to the global ``service_names()``.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_keychain_names_unit.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-keychain-names-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import keychain_names as kn  # noqa: E402
from keychain_names import (  # noqa: E402
    LEGACY_SERVICE,
    PRIMARY_SERVICE,
    auth_services,
    service_names,
)


def test_service_names_defaults():
    assert service_names() == (PRIMARY_SERVICE, LEGACY_SERVICE)


def test_service_names_custom_both():
    assert service_names("a", "b") == ("a", "b")


def test_service_names_primary_only_defaults_legacy():
    assert service_names("a", None) == ("a", LEGACY_SERVICE)


def test_service_names_legacy_only_defaults_primary():
    assert service_names(None, "b") == (PRIMARY_SERVICE, "b")


def test_service_names_dedup_same_name():
    assert service_names("x", "x") == ("x",)


def test_service_names_empty_strings_skipped():
    assert service_names("", "") == ()


def test_service_names_explicit_empty_primary_keeps_legacy():
    assert service_names("", None) == (LEGACY_SERVICE,)


def _patch_home(monkeypatch, home, default):
    import paths

    monkeypatch.setattr(paths, "ba_home", lambda: Path(home))
    monkeypatch.setattr(paths, "_default_home", lambda: Path(default))


def test_home_suffix_default_home_empty(monkeypatch):
    _patch_home(monkeypatch, "/same/where", "/same/where")
    assert kn.home_suffix() == ""


def test_home_suffix_slug_folding(monkeypatch):
    _patch_home(monkeypatch, "/tmp/bc!!!weird path", "/different")
    # "/tmp/bc!!!weird path" -> non-[alnum-dash] to "-" -> strip -> collapse
    assert kn.home_suffix() == "tmp-bc-weird-path"


def test_home_suffix_all_symbols_empty(monkeypatch):
    _patch_home(monkeypatch, "/!!!", "/d")
    assert kn.home_suffix() == ""


def test_home_suffix_truncated_to_63(monkeypatch):
    _patch_home(monkeypatch, "/x/" + "a" * 70, "/d")
    slug = kn.home_suffix()
    assert len(slug) == 63
    assert set(slug) == {"a"}


def test_home_suffix_resolve_failure_returns_empty(monkeypatch):
    import paths

    def boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(paths, "ba_home", boom)
    assert kn.home_suffix() == ""


def test_auth_services_no_suffix(monkeypatch):
    _patch_home(monkeypatch, "/same", "/same")
    assert auth_services() == (PRIMARY_SERVICE, LEGACY_SERVICE)


def test_auth_services_with_suffix(monkeypatch):
    _patch_home(monkeypatch, "/tmp/bc@inst", "/default")
    suffix = kn.home_suffix()
    expected = (
        f"{PRIMARY_SERVICE}-{suffix}",
        f"{LEGACY_SERVICE}-{suffix}",
    )
    assert auth_services() == expected
