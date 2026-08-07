"""Unit tests for ``internal_guards`` — the authority checks gating ``/api/internal/*``.

This is a security boundary: there is exactly one implementation of each
check and every internal router shares it. The tests pin the two distinct
concerns the module keeps separate (authority vs. runtime readiness) and
the fail-closed default of an unconfigured module.

Pins:
  1. ``authority_is_valid`` / ``require_internal``: unconfigured -> False +
     403 (fail closed); configured with a bound principal -> True + pass;
     configured with a principal source returning None -> False + 403.
  2. ``internal_authority_extension_id``: None when unconfigured, None when
     no principal, None when the principal is not an extension, and the
     extension id when it is.
  3. ``require_builtin_runtime_extension``: 404 with the readiness message
     when the owning extension is not ready; pass when it is ready.
  4. ``require_role_internal``: authority is checked FIRST (a failing
     authority never reaches the readiness lookup), then the role is
     resolved to its owning extension and that extension's readiness gates
     the call with a 404.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_internal_guards_unit.py -v
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-internal-guards-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import internal_guards  # noqa: E402
from fastapi import HTTPException  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_principal_provider():
    """The principal source is a module global; clear it around every test
    so the fail-closed default and a configured state never leak across tests."""
    internal_guards._principal_provider = None
    yield
    internal_guards._principal_provider = None


# --------------------------------------------------------------------------- #
# authority_is_valid / require_internal
# --------------------------------------------------------------------------- #


def test_unconfigured_fails_closed():
    """An unconfigured module grants nothing — authority is invalid and the
    internal gate rejects with 403."""
    assert internal_guards.authority_is_valid() is False
    with pytest.raises(HTTPException) as exc:
        internal_guards.require_internal()
    assert exc.value.status_code == 403


def test_configured_with_bound_principal_is_valid():
    principal = SimpleNamespace(kind="runner")
    internal_guards.configure(lambda: principal)

    assert internal_guards.authority_is_valid() is True
    # Valid authority must not raise.
    assert internal_guards.require_internal() is None


def test_configured_principal_source_returning_none_is_invalid():
    """A bound source that yields no principal for this request is still
    unauthenticated -> 403, not a pass."""
    internal_guards.configure(lambda: None)

    assert internal_guards.authority_is_valid() is False
    with pytest.raises(HTTPException) as exc:
        internal_guards.require_internal()
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# internal_authority_extension_id
# --------------------------------------------------------------------------- #


def test_extension_id_none_when_unconfigured():
    assert internal_guards.internal_authority_extension_id() is None


def test_extension_id_none_when_no_principal():
    internal_guards.configure(lambda: None)
    assert internal_guards.internal_authority_extension_id() is None


def test_extension_id_none_for_non_extension_principal():
    internal_guards.configure(lambda: SimpleNamespace(kind="runner"))
    assert internal_guards.internal_authority_extension_id() is None


def test_extension_id_none_when_kind_attr_missing():
    """A principal without a ``kind`` attribute is treated as non-extension."""
    internal_guards.configure(lambda: object())
    assert internal_guards.internal_authority_extension_id() is None


def test_extension_id_returned_for_extension_principal():
    internal_guards.configure(lambda: SimpleNamespace(kind="extension", extension_id="ext-42"))
    assert internal_guards.internal_authority_extension_id() == "ext-42"


# --------------------------------------------------------------------------- #
# require_builtin_runtime_extension
# --------------------------------------------------------------------------- #


def test_require_builtin_runtime_extension_404_when_not_ready(monkeypatch):
    monkeypatch.setattr(
        internal_guards.extension_store,
        "runtime_not_ready_message",
        lambda extension_id: "extension not running",
    )

    with pytest.raises(HTTPException) as exc:
        internal_guards.require_builtin_runtime_extension("ext-1")
    assert exc.value.status_code == 404
    assert exc.value.detail == "extension not running"


def test_require_builtin_runtime_extension_passes_when_ready(monkeypatch):
    monkeypatch.setattr(
        internal_guards.extension_store,
        "runtime_not_ready_message",
        lambda extension_id: None,
    )

    assert internal_guards.require_builtin_runtime_extension("ext-1") is None


# --------------------------------------------------------------------------- #
# require_role_internal — composition + ordering
# --------------------------------------------------------------------------- #


def test_require_role_internal_passes_when_authority_and_readiness_ok(monkeypatch):
    internal_guards.configure(lambda: SimpleNamespace(kind="runner"))

    resolved: dict[str, str] = {}

    def fake_role_to_extension(role):
        resolved["role"] = role
        return "ext-for-" + role

    def fake_not_ready(extension_id):
        resolved["checked"] = extension_id
        return None

    monkeypatch.setattr(internal_guards.extension_store, "extension_id_for_role", fake_role_to_extension)
    monkeypatch.setattr(internal_guards.extension_store, "runtime_not_ready_message", fake_not_ready)

    assert internal_guards.require_role_internal("coordination") is None
    # The role is resolved to its owning extension, then that extension's
    # readiness is checked against the resolved id — not the raw role.
    assert resolved == {"role": "coordination", "checked": "ext-for-coordination"}


def test_require_role_internal_checks_authority_before_readiness(monkeypatch):
    """Failing authority must never reach the role/readiness lookup."""
    calls: list[str] = []

    def boom_role(_role):
        calls.append("role")
        return "ext"

    def boom_not_ready(_extension_id):
        calls.append("ready")
        return "down"

    monkeypatch.setattr(internal_guards.extension_store, "extension_id_for_role", boom_role)
    monkeypatch.setattr(internal_guards.extension_store, "runtime_not_ready_message", boom_not_ready)

    with pytest.raises(HTTPException) as exc:
        internal_guards.require_role_internal("coordination")
    assert exc.value.status_code == 403
    assert calls == []  # authority failed closed before any readiness work


def test_require_role_internal_404_when_readiness_fails_after_authority(monkeypatch):
    internal_guards.configure(lambda: SimpleNamespace(kind="runner"))
    monkeypatch.setattr(
        internal_guards.extension_store, "extension_id_for_role", lambda role: "ext-x"
    )
    monkeypatch.setattr(
        internal_guards.extension_store,
        "runtime_not_ready_message",
        lambda extension_id: f"{extension_id} down",
    )

    with pytest.raises(HTTPException) as exc:
        internal_guards.require_role_internal("coordination")
    assert exc.value.status_code == 404
    assert exc.value.detail == "ext-x down"


if __name__ == "__main__":
    raise SystemExit(os.system(f"{sys.executable} -m pytest {__file__} -v"))
