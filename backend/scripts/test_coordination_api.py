from __future__ import annotations

import pytest
from fastapi import HTTPException

import coordination
import coordination_api
import internal_guards

pytestmark = pytest.mark.anyio


def _patch_guards(
    monkeypatch,
    *,
    authority_valid: bool = True,
    principal: str | None = "core",
    lock_result=None,
):
    """Replace every external dependency of the handler with fakes."""
    calls: dict = {"lock_ops": None}

    monkeypatch.setattr(
        internal_guards, "require_builtin_runtime_extension", lambda _ext: None
    )
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: authority_valid)
    monkeypatch.setattr(
        internal_guards, "internal_authority_extension_id", lambda: principal
    )

    async def fake_lock_ops(**kwargs):
        calls["lock_ops"] = kwargs
        return lock_result

    monkeypatch.setattr(coordination, "lock_ops", fake_lock_ops)
    return calls


async def _invoke(body: dict):
    return await coordination_api.internal_coordination_lock_ops(
        body, x_internal_token="tok"
    )


class TestAuthorityGate:
    async def test_invalid_authority_rejected_before_any_op(self, monkeypatch) -> None:
        calls = _patch_guards(monkeypatch, authority_valid=False)
        with pytest.raises(HTTPException) as exc:
            await _invoke({"op": "acquire"})
        assert exc.value.status_code == 403
        assert calls["lock_ops"] is None  # never reached lock_ops

    async def test_valid_authority_proceeds_to_lock_ops(self, monkeypatch) -> None:
        _patch_guards(monkeypatch, authority_valid=True, principal="core",
                       lock_result={"ok": True})
        result = await _invoke({"op": "acquire", "key": "k"})
        assert result == {"ok": True}


class TestOwnerAuthRestriction:
    """Owner-based ops must be rejected for any non-core principal."""

    @pytest.mark.parametrize("op", ["reattach", "list_owned", "release_owned"])
    async def test_explicit_owner_op_blocked_for_non_core(self, monkeypatch, op) -> None:
        _patch_guards(monkeypatch, principal="ext.some-extension")
        with pytest.raises(HTTPException) as exc:
            await _invoke({"op": op})
        assert exc.value.status_code == 403
        assert "trusted runner identity required" in exc.value.detail

    @pytest.mark.parametrize("op", ["reattach", "list_owned", "release_owned"])
    async def test_explicit_owner_op_allowed_for_core(self, monkeypatch, op) -> None:
        _patch_guards(monkeypatch, principal="core", lock_result={"done": op})
        result = await _invoke({"op": op})
        assert result == {"done": op}

    async def test_renew_without_token_blocked_for_non_core(self, monkeypatch) -> None:
        _patch_guards(monkeypatch, principal="ext.x")
        with pytest.raises(HTTPException) as exc:
            await _invoke({"renew": True})
        assert exc.value.status_code == 403

    async def test_renew_with_token_allowed_for_non_core(self, monkeypatch) -> None:
        _patch_guards(monkeypatch, principal="ext.x", lock_result={"r": 1})
        result = await _invoke({"renew": True, "holder_token": "tok-1"})
        assert result == {"r": 1}

    async def test_release_owned_derived_from_flags_blocked_non_core(
        self, monkeypatch
    ) -> None:
        _patch_guards(monkeypatch, principal="ext.x")
        with pytest.raises(HTTPException) as exc:
            await _invoke({"release": True, "owned": True})
        assert exc.value.status_code == 403

    async def test_owned_alone_derives_list_owned_blocked_non_core(
        self, monkeypatch
    ) -> None:
        _patch_guards(monkeypatch, principal="ext.x")
        with pytest.raises(HTTPException) as exc:
            await _invoke({"owned": True})
        assert exc.value.status_code == 403

    async def test_reattach_flag_derives_owner_op_blocked_non_core(
        self, monkeypatch
    ) -> None:
        # {"reattach": True} with no explicit op derives requested_op="reattach"
        _patch_guards(monkeypatch, principal="ext.x")
        with pytest.raises(HTTPException) as exc:
            await _invoke({"reattach": True})
        assert exc.value.status_code == 403


class TestNonOwnerOpsFromNonCore:
    async def test_acquire_proceeds(self, monkeypatch) -> None:
        _patch_guards(monkeypatch, principal="ext.x", lock_result={"ok": 1})
        result = await _invoke({})
        assert result == {"ok": 1}

    async def test_release_alone_proceeds(self, monkeypatch) -> None:
        _patch_guards(monkeypatch, principal="ext.x", lock_result={"ok": 1})
        result = await _invoke({"release": True})
        assert result == {"ok": 1}

    async def test_validate_proceeds(self, monkeypatch) -> None:
        _patch_guards(monkeypatch, principal="ext.x", lock_result={"ok": 1})
        result = await _invoke({"validate": True})
        assert result == {"ok": 1}


class TestOpNormalization:
    async def test_dashed_uppercase_op_normalized_to_owner_op(self, monkeypatch) -> None:
        # "  Release-Owned  " → strip/lower/replace("-","_") → "release_owned"
        # which is an owner op → owner_auth_required → 403 for non-core
        _patch_guards(monkeypatch, principal="ext.x")
        with pytest.raises(HTTPException) as exc:
            await _invoke({"op": "  Release-Owned  "})
        assert exc.value.status_code == 403

    async def test_dashed_form_that_is_not_an_owner_op_proceeds(
        self, monkeypatch
    ) -> None:
        # "re-attach" → "re_attach" (NOT "reattach") → not an owner op → proceeds
        _patch_guards(monkeypatch, principal="ext.x", lock_result={"ok": 1})
        result = await _invoke({"op": "re-attach"})
        assert result == {"ok": 1}


class TestPassthrough:
    async def test_lock_ops_called_with_raw_op_and_flags(self, monkeypatch) -> None:
        calls = _patch_guards(monkeypatch, principal="core", lock_result={"ok": 1})
        await _invoke({
            "op": "acquire",
            "key": "k1",
            "keys": ["k1", "k2"],
            "release": False,
            "renew": False,
            "validate": False,
            "reattach": False,
            "owned": False,
            "holder_token": "tok",
            "timeout_seconds": 5,
            "lease_seconds": 30,
            "owner": {"source": "test", "extra": 1},
        })
        kwargs = calls["lock_ops"]
        assert kwargs["key"] == "k1"
        assert kwargs["keys"] == ["k1", "k2"]
        assert kwargs["op"] == "acquire"
        assert kwargs["holder_token"] == "tok"
        assert kwargs["timeout_seconds"] == 5
        assert kwargs["lease_seconds"] == 30
        # owner gets principal stamped + default source preserved
        assert kwargs["owner"]["principal_extension_id"] == "core"
        assert kwargs["owner"]["source"] == "test"
        assert kwargs["owner"]["extra"] == 1

    async def test_non_dict_owner_becomes_empty(self, monkeypatch) -> None:
        calls = _patch_guards(monkeypatch, principal="core", lock_result={"ok": 1})
        await _invoke({"owner": "not-a-dict"})
        owner = calls["lock_ops"]["owner"]
        assert owner["principal_extension_id"] == "core"
        assert owner["source"] == "internal_coordination_lock_ops"

    async def test_non_list_keys_becomes_none(self, monkeypatch) -> None:
        calls = _patch_guards(monkeypatch, principal="core", lock_result={"ok": 1})
        await _invoke({"keys": "not-a-list"})
        assert calls["lock_ops"]["keys"] is None

    async def test_principal_defaults_to_core_when_extension_id_none(
        self, monkeypatch
    ) -> None:
        calls = _patch_guards(monkeypatch, principal=None, lock_result={"ok": 1})
        await _invoke({"op": "reattach"})
        # principal None → "core" → owner op allowed, stamp "core"
        assert calls["lock_ops"]["owner"]["principal_extension_id"] == "core"

    async def test_missing_owner_uses_default_source(self, monkeypatch) -> None:
        calls = _patch_guards(monkeypatch, principal="core", lock_result={"ok": 1})
        await _invoke({"op": "acquire"})
        assert calls["lock_ops"]["owner"]["source"] == "internal_coordination_lock_ops"
