"""Unit tests for ``provider_kinds.all_provider_kinds``.

The registry lookup is isolated by injecting a fake ``provider`` module into
``sys.modules``; no real provider registry is read.

Pins:
  1. Happy path: string ``KIND`` values merge with the fallback set, deduped
     and sorted. Empty-string ``KIND`` is dropped, non-string ``KIND`` is
     dropped (``isinstance`` guard), and a missing ``KIND`` attribute falls
     back to ``""`` and is dropped.
  2. Failure path: ``known_providers`` raising, and ``provider`` lacking
     ``known_providers`` (import-time ``AttributeError``/``ImportError``),
     both fall back to the sorted fallback tuple.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_provider_kinds_unit.py -v
"""

from __future__ import annotations

import os
import sys
import types

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-provider-kinds-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import provider_kinds as pk  # noqa: E402
from provider_kinds import FALLBACK_PROVIDER_KINDS, all_provider_kinds  # noqa: E402


class _Prov:
    def __init__(self, kind):
        self.KIND = kind


def _install_fake_provider(monkeypatch, known_providers):
    """Point ``provider.known_providers`` at a fake callable.

    The real ``known_providers`` is a function returning an iterable of
    provider objects, so the fake must be callable too.
    """
    fake = types.ModuleType("provider")
    fake.known_providers = known_providers
    monkeypatch.setitem(sys.modules, "provider", fake)


def test_fallback_constant_is_sorted():
    assert FALLBACK_PROVIDER_KINDS == tuple(sorted(FALLBACK_PROVIDER_KINDS))
    assert "claude" in FALLBACK_PROVIDER_KINDS


def test_known_providers_merged_sorted_deduped(monkeypatch):
    _install_fake_provider(monkeypatch, lambda: [_Prov("claude"), _Prov("mykind")])
    kinds = all_provider_kinds()
    assert kinds == sorted(set(kinds))
    assert "mykind" in kinds
    assert kinds.count("claude") == 1


def test_empty_kind_dropped(monkeypatch):
    _install_fake_provider(monkeypatch, lambda: [_Prov(""), _Prov("valid")])
    kinds = all_provider_kinds()
    assert "valid" in kinds
    assert "" not in kinds


def test_non_str_kind_dropped(monkeypatch):
    _install_fake_provider(monkeypatch, lambda: [_Prov(123), _Prov("valid")])
    kinds = all_provider_kinds()
    assert "valid" in kinds
    assert 123 not in kinds


def test_missing_kind_attr_dropped(monkeypatch):
    bare = types.SimpleNamespace()
    _install_fake_provider(monkeypatch, lambda: [bare, _Prov("valid")])
    kinds = all_provider_kinds()
    assert "valid" in kinds


def test_known_providers_raises_uses_fallback(monkeypatch):
    def boom():
        raise RuntimeError("registry unavailable")

    _install_fake_provider(monkeypatch, boom)
    assert all_provider_kinds() == sorted(FALLBACK_PROVIDER_KINDS)


def test_provider_lacks_known_providers_uses_fallback(monkeypatch):
    fake = types.ModuleType("provider")  # no known_providers attr
    monkeypatch.setitem(sys.modules, "provider", fake)
    assert all_provider_kinds() == sorted(FALLBACK_PROVIDER_KINDS)
