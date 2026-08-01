from __future__ import annotations

import os
import pathlib
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import paths  # noqa: E402
import keychain_names as kn  # noqa: E402


# --- service_names -----------------------------------------------------------

def test_service_names_defaults():
    assert kn.service_names() == ("better-agent", "better-claude")


def test_service_names_custom():
    assert kn.service_names("a", "b") == ("a", "b")


def test_service_names_dedup_when_primary_equals_legacy():
    assert kn.service_names("x", "x") == ("x",)


def test_service_names_skips_falsy_and_none():
    assert kn.service_names("", "b") == ("b",)
    assert kn.service_names("a", "") == ("a",)
    assert kn.service_names("", "") == ()
    # explicit None means "use default", not "skip" → legacy defaults in
    assert kn.service_names("a", None) == ("a", "better-claude")


# --- home_suffix -------------------------------------------------------------

def test_home_suffix_empty_for_default_home(monkeypatch):
    same = pathlib.Path("/some/default/home")
    monkeypatch.setattr(paths, "ba_home", lambda: same)
    monkeypatch.setattr(paths, "_default_home", lambda: same)
    assert kn.home_suffix() == ""


def test_home_suffix_empty_when_paths_raises(monkeypatch):
    def _boom():
        raise RuntimeError("no home")
    monkeypatch.setattr(paths, "ba_home", _boom)
    assert kn.home_suffix() == ""


def test_home_suffix_slug_for_non_default_home():
    tmp = tempfile.mkdtemp(prefix="ba-kn-")
    paths.engage_test_home(tmp)
    suffix = kn.home_suffix()
    assert suffix  # non-empty slug
    assert all(c.isalnum() or c == "-" for c in suffix)  # filesystem-safe
    assert len(suffix) <= 63


# --- auth_services -----------------------------------------------------------

def test_auth_services_unsuffixed_when_default_home(monkeypatch):
    same = pathlib.Path("/some/default/home")
    monkeypatch.setattr(paths, "ba_home", lambda: same)
    monkeypatch.setattr(paths, "_default_home", lambda: same)
    assert kn.auth_services() == kn.service_names()


def test_auth_services_suffixed_for_non_default_home():
    tmp = tempfile.mkdtemp(prefix="ba-kn-auth-")
    paths.engage_test_home(tmp)
    try:
        services = kn.auth_services()
        suffix = kn.home_suffix()
        assert kn.PRIMARY_SERVICE not in services  # replaced by suffixed form
        assert any(s.endswith(suffix) for s in services)
        assert services == kn.service_names(
            f"{kn.PRIMARY_SERVICE}-{suffix}", f"{kn.LEGACY_SERVICE}-{suffix}"
        )
    finally:
        paths.engage_test_home(tempfile.mkdtemp(prefix="ba-kn-rest-"))
