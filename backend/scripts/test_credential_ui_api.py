"""Unit tests for credential_ui_api — the credential-broker consent surface
and OS-keychain password-manager UI routes.

The real secret confinement lives downstream (internal_guards authority,
credential_broker consent/password stores, password_manager keychain).
credential_ui_api.py itself is thin wiring: the internal-authority guard
(403), input validation (400s), consent approve/deny/revoke dispatch into
the broker with reason->status mapping, the coordinator broadcast fan-out,
and the password_manager try/except branches. All deterministic -> unit
tier. Route handlers are called directly as async functions; the lazy
in-handler imports are driven by monkeypatching the underlying modules.
"""
from __future__ import annotations

import os
import sys

import _test_home  # noqa: F401  (engages prod-home guard before backend import)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest

pytestmark = pytest.mark.anyio

import credential_ui_api  # noqa: E402
import credential_broker.consent_store as consent_store  # noqa: E402
import credential_broker.broker as broker  # noqa: E402
import password_manager  # noqa: E402
from credential_broker.broker import BrokerError  # noqa: E402
from password_manager import PasswordManagerError  # noqa: E402


def _http_status(excinfo) -> int:
    return excinfo.value.status_code


def _raising(exc):
    """Factory for a stub that ignores args and raises a fixed exception."""

    def _fn(*_args, **_kwargs):
        raise exc

    return _fn


class _FakeCoordinator:
    """Records credential-consent-changed broadcasts."""

    def __init__(self) -> None:
        self.broadcasts: list[object] = []

    async def broadcast_credential_consent_changed(self, app_session_id) -> None:
        self.broadcasts.append(app_session_id)


@pytest.fixture
def guarded(monkeypatch):
    """Make the internal-authority guard pass for credential-broker."""
    monkeypatch.setattr(credential_ui_api.internal_guards, "authority_is_valid", lambda: True)

    def _require(ext_id: str) -> None:  # no-op stand-in for the runtime check
        return None

    monkeypatch.setattr(credential_ui_api.internal_guards, "require_builtin_runtime_extension", _require)
    monkeypatch.setattr(credential_ui_api.extension_store, "extension_id_for_role", lambda role: f"ext-{role}")
    return credential_ui_api


@pytest.fixture
def coord(monkeypatch):
    fake = _FakeCoordinator()
    credential_ui_api.configure(coordinator=fake)
    yield fake
    credential_ui_api.configure(coordinator=None)


# --------------------------------------------------------------------------- #
# configure / _coordinator
# --------------------------------------------------------------------------- #
async def test_coordinator_unconfigured_raises_503():
    credential_ui_api.configure(coordinator=None)
    with pytest.raises(Exception) as excinfo:
        credential_ui_api._coordinator()
    assert _http_status(excinfo) == 503


async def test_configure_binds_coordinator():
    fake = _FakeCoordinator()
    credential_ui_api.configure(coordinator=fake)
    assert credential_ui_api._coordinator() is fake
    credential_ui_api.configure(coordinator=None)


# --------------------------------------------------------------------------- #
# guard
# --------------------------------------------------------------------------- #
async def test_guard_rejects_when_authority_invalid(monkeypatch):
    monkeypatch.setattr(credential_ui_api.internal_guards, "authority_is_valid", lambda: False)
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_list_pending_credentials(
            body={}, x_internal_token="t"
        )
    assert _http_status(excinfo) == 403


# --------------------------------------------------------------------------- #
# list pending
# --------------------------------------------------------------------------- #
async def test_list_pending_with_app_session(guarded, monkeypatch):
    seen = {}

    def fake_list_pending(*, app_session_id=None):
        seen["app_session_id"] = app_session_id
        return [{"id": "c1", "app_session_id": app_session_id}]

    def fake_public_view(rec):
        return {"id": rec["id"], "view": True}

    monkeypatch.setattr(consent_store, "list_pending", fake_list_pending)
    monkeypatch.setattr(consent_store, "public_view", fake_public_view)

    out = await credential_ui_api.internal_list_pending_credentials(
        body={"app_session_id": "sess-1"}, x_internal_token="t"
    )
    assert seen["app_session_id"] == "sess-1"
    assert out == {"consents": [{"id": "c1", "view": True}]}


async def test_list_pending_empty_body_yields_none_app_session(guarded, monkeypatch):
    seen = {}

    def fake_list_pending(*, app_session_id=None):
        seen["app_session_id"] = app_session_id
        return []

    monkeypatch.setattr(consent_store, "list_pending", fake_list_pending)
    monkeypatch.setattr(consent_store, "public_view", lambda rec: rec)

    # body=None exercises the `(body or {})` falsy branch.
    out = await credential_ui_api.internal_list_pending_credentials(
        body=None, x_internal_token="t"
    )
    assert seen["app_session_id"] is None
    assert out == {"consents": []}


# --------------------------------------------------------------------------- #
# approve
# --------------------------------------------------------------------------- #
def _patch_approve(monkeypatch, rec, reason, *, raises=None, recorder=None):
    def fake_approve(consent_id, secret_value=None, secret_values=None):
        if recorder is not None:
            recorder["args"] = (consent_id, secret_value, secret_values)
        if raises is not None:
            raise raises
        return rec, reason

    monkeypatch.setattr(broker, "approve_consent", fake_approve)


async def test_approve_missing_consent_id(guarded, coord):
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_approve_credential_consent(
            body={}, x_internal_token="t"
        )
    assert _http_status(excinfo) == 400
    assert coord.broadcasts == []


async def test_approve_whitespace_consent_id(guarded, coord):
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_approve_credential_consent(
            body={"consent_id": "   "}, x_internal_token="t"
        )
    assert _http_status(excinfo) == 400


async def test_approve_secrets_not_object(guarded, coord):
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_approve_credential_consent(
            body={"consent_id": "c1", "secrets": "nope"}, x_internal_token="t"
        )
    assert _http_status(excinfo) == 400


async def test_approve_broker_error_maps_400(guarded, coord, monkeypatch):
    _patch_approve(monkeypatch, None, "x", raises=BrokerError("boom"))
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_approve_credential_consent(
            body={"consent_id": "c1"}, x_internal_token="t"
        )
    assert _http_status(excinfo) == 400
    assert "boom" in excinfo.value.detail


async def test_approve_reason_missing_maps_404(guarded, coord, monkeypatch):
    _patch_approve(monkeypatch, None, "missing")
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_approve_credential_consent(
            body={"consent_id": "c1"}, x_internal_token="t"
        )
    assert _http_status(excinfo) == 404
    assert coord.broadcasts == []


async def test_approve_reason_expired_maps_410(guarded, coord, monkeypatch):
    _patch_approve(monkeypatch, None, "expired")
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_approve_credential_consent(
            body={"consent_id": "c1"}, x_internal_token="t"
        )
    assert _http_status(excinfo) == 410
    assert coord.broadcasts == []


async def test_approve_success_broadcasts_and_returns_status(guarded, coord, monkeypatch):
    rec = {"app_session_id": "sess-9"}
    recorder = {}
    _patch_approve(monkeypatch, rec, "approved", recorder=recorder)
    out = await credential_ui_api.internal_approve_credential_consent(
        body={"consent_id": "c1", "secret": "s", "secrets": {"a": "b"}},
        x_internal_token="t",
    )
    assert out == {"status": "approved"}
    assert recorder["args"] == ("c1", "s", {"a": "b"})
    assert coord.broadcasts == ["sess-9"]


async def test_approve_success_null_rec_broadcasts_none(guarded, coord, monkeypatch):
    # reason != missing/expired but rec is None -> (rec or {}) guard -> broadcast(None)
    _patch_approve(monkeypatch, None, "ok")
    out = await credential_ui_api.internal_approve_credential_consent(
        body={"consent_id": "c1"}, x_internal_token="t"
    )
    assert out == {"status": "ok"}
    assert coord.broadcasts == [None]


# --------------------------------------------------------------------------- #
# deny / revoke (shared shape: missing-consent -> 400, missing -> 404, else broadcast+status)
# --------------------------------------------------------------------------- #
async def test_deny_missing_consent_id(guarded, coord):
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_deny_credential_consent(body={}, x_internal_token="t")
    assert _http_status(excinfo) == 400


async def test_deny_reason_missing_maps_404(guarded, coord, monkeypatch):
    monkeypatch.setattr(broker, "deny_consent", lambda cid: (None, "missing"))
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_deny_credential_consent(
            body={"consent_id": "c1"}, x_internal_token="t"
        )
    assert _http_status(excinfo) == 404
    assert coord.broadcasts == []


async def test_deny_success_broadcasts(guarded, coord, monkeypatch):
    monkeypatch.setattr(broker, "deny_consent", lambda cid: ({"app_session_id": "s2"}, "denied"))
    out = await credential_ui_api.internal_deny_credential_consent(
        body={"consent_id": "c1"}, x_internal_token="t"
    )
    assert out == {"status": "denied"}
    assert coord.broadcasts == ["s2"]


async def test_deny_success_null_rec_broadcasts_none(guarded, coord, monkeypatch):
    monkeypatch.setattr(broker, "deny_consent", lambda cid: (None, "denied"))
    out = await credential_ui_api.internal_deny_credential_consent(
        body={"consent_id": "c1"}, x_internal_token="t"
    )
    assert out == {"status": "denied"}
    assert coord.broadcasts == [None]


async def test_revoke_missing_consent_id(guarded, coord):
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_revoke_credential_consent(body={}, x_internal_token="t")
    assert _http_status(excinfo) == 400


async def test_revoke_reason_missing_maps_404(guarded, coord, monkeypatch):
    monkeypatch.setattr(broker, "revoke_consent", lambda cid: (None, "missing"))
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_revoke_credential_consent(
            body={"consent_id": "c1"}, x_internal_token="t"
        )
    assert _http_status(excinfo) == 404


async def test_revoke_success_broadcasts(guarded, coord, monkeypatch):
    monkeypatch.setattr(broker, "revoke_consent", lambda cid: ({"app_session_id": "s3"}, "revoked"))
    out = await credential_ui_api.internal_revoke_credential_consent(
        body={"consent_id": "c1"}, x_internal_token="t"
    )
    assert out == {"status": "revoked"}
    assert coord.broadcasts == ["s3"]


# --------------------------------------------------------------------------- #
# password-manager list
# --------------------------------------------------------------------------- #
async def test_pm_list_success(guarded, monkeypatch):
    monkeypatch.setattr(password_manager, "list_service_passwords", lambda: {"services": ["a"]})
    out = await credential_ui_api.internal_list_password_manager_secrets(
        body=None, x_internal_token="t"
    )
    assert out == {"services": ["a"]}


async def test_pm_list_password_manager_error_maps_500(guarded, monkeypatch):
    monkeypatch.setattr(
        password_manager, "list_service_passwords", _raising(PasswordManagerError("nope"))
    )
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_list_password_manager_secrets(body=None, x_internal_token="t")
    assert _http_status(excinfo) == 500
    assert "nope" in excinfo.value.detail


async def test_pm_list_generic_error_maps_500(guarded, monkeypatch):
    monkeypatch.setattr(
        password_manager, "list_service_passwords", _raising(RuntimeError("kaboom"))
    )
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_list_password_manager_secrets(body=None, x_internal_token="t")
    assert _http_status(excinfo) == 500
    assert excinfo.value.detail == "failed to list passwords"


# --------------------------------------------------------------------------- #
# password-manager store
# --------------------------------------------------------------------------- #
async def test_pm_store_success(guarded, monkeypatch):
    seen = {}

    def fake_store(payload):
        seen["payload"] = payload
        return {"id": "st1"}

    monkeypatch.setattr(password_manager, "store_service_password", fake_store)
    out = await credential_ui_api.internal_store_password_manager_secret(
        body={"service": "x"}, x_internal_token="t"
    )
    assert seen["payload"] == {"service": "x"}
    assert out == {"status": "stored", "id": "st1"}


async def test_pm_store_null_body(guarded, monkeypatch):
    monkeypatch.setattr(password_manager, "store_service_password", lambda p: {"id": "st2"})
    out = await credential_ui_api.internal_store_password_manager_secret(body=None, x_internal_token="t")
    assert out == {"status": "stored", "id": "st2"}


async def test_pm_store_password_manager_error_maps_400(guarded, monkeypatch):
    monkeypatch.setattr(
        password_manager, "store_service_password", _raising(PasswordManagerError("bad"))
    )
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_store_password_manager_secret(body={}, x_internal_token="t")
    assert _http_status(excinfo) == 400
    assert "bad" in excinfo.value.detail


async def test_pm_store_generic_error_maps_500(guarded, monkeypatch):
    monkeypatch.setattr(
        password_manager, "store_service_password", _raising(ValueError("oops"))
    )
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_store_password_manager_secret(body={}, x_internal_token="t")
    assert _http_status(excinfo) == 500
    assert excinfo.value.detail == "failed to store password"


# --------------------------------------------------------------------------- #
# password-manager delete
# --------------------------------------------------------------------------- #
async def test_pm_delete_success(guarded, monkeypatch):
    seen = {}

    def fake_delete(payload):
        seen["payload"] = payload
        return {"id": "d1"}

    monkeypatch.setattr(password_manager, "delete_service_password", fake_delete)
    out = await credential_ui_api.internal_delete_password_manager_secret(
        body={"service": "y"}, x_internal_token="t"
    )
    assert seen["payload"] == {"service": "y"}
    assert out == {"status": "deleted", "id": "d1"}


async def test_pm_delete_null_body(guarded, monkeypatch):
    monkeypatch.setattr(password_manager, "delete_service_password", lambda p: {"id": "d2"})
    out = await credential_ui_api.internal_delete_password_manager_secret(body=None, x_internal_token="t")
    assert out == {"status": "deleted", "id": "d2"}


async def test_pm_delete_password_manager_error_maps_400(guarded, monkeypatch):
    monkeypatch.setattr(
        password_manager, "delete_service_password", _raising(PasswordManagerError("no"))
    )
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_delete_password_manager_secret(body={}, x_internal_token="t")
    assert _http_status(excinfo) == 400
    assert "no" in excinfo.value.detail


async def test_pm_delete_generic_error_maps_500(guarded, monkeypatch):
    monkeypatch.setattr(
        password_manager, "delete_service_password", _raising(RuntimeError("dead"))
    )
    with pytest.raises(Exception) as excinfo:
        await credential_ui_api.internal_delete_password_manager_secret(body={}, x_internal_token="t")
    assert _http_status(excinfo) == 500
    assert excinfo.value.detail == "failed to delete password"
