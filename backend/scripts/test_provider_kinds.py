from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import provider_kinds as pk  # noqa: E402


class _FakeProvider:
    def __init__(self, kind):
        self.KIND = kind


def test_fallback_tuple_contents():
    assert "claude" in pk.FALLBACK_PROVIDER_KINDS
    assert "codex" in pk.FALLBACK_PROVIDER_KINDS
    assert "agy" in pk.FALLBACK_PROVIDER_KINDS


def test_all_kinds_registry_unavailable_returns_sorted_fallback(monkeypatch):
    import provider

    def _known_boom():
        raise RuntimeError("no registry")

    monkeypatch.setattr(provider, "known_providers", _known_boom)
    result = pk.all_provider_kinds()
    assert result == sorted(pk.FALLBACK_PROVIDER_KINDS)


def test_all_kinds_merges_registry_and_fallback_dedup_sorted(monkeypatch):
    import provider

    monkeypatch.setattr(
        provider,
        "known_providers",
        lambda: [
            _FakeProvider("claude"),      # duplicate of fallback → deduped
            _FakeProvider("zeta-extra"),  # new from registry
            _FakeProvider(""),            # empty filtered out
            _FakeProvider(123),           # non-str filtered out
            _FakeProvider(None),
        ],
    )
    result = pk.all_provider_kinds()
    assert "zeta-extra" in result
    assert "claude" in result
    assert result == sorted(set(result))  # sorted, deduped
    for k in pk.FALLBACK_PROVIDER_KINDS:
        assert k in result
