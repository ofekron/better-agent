from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import extension_store
import node_link
import node_op
import node_rpc_handlers

pytestmark = pytest.mark.anyio


def _patch_dispatch(monkeypatch, exc):
    """Make node_rpc_handlers.call_local_or_remote raise `exc` (or return a value)."""

    async def fake_call(node_id, method, params):
        if isinstance(exc, BaseException):
            raise exc
        return exc

    monkeypatch.setattr(node_rpc_handlers, "call_local_or_remote", fake_call)


def _patch_ready(monkeypatch, message):
    monkeypatch.setattr(extension_store, "runtime_not_ready_message", lambda _role: message)


class TestPrimaryDispatch:
    async def test_primary_success_returns_value(self, monkeypatch) -> None:
        _patch_dispatch(monkeypatch, {"ok": True})
        result = await node_op.node_op("primary", "ping", {})
        assert result == {"ok": True}

    async def test_primary_not_ready_branch_skipped(self, monkeypatch) -> None:
        # node_id == primary → the not-ready check is never reached even if message set
        _patch_ready(monkeypatch, "nodes offline")
        _patch_dispatch(monkeypatch, {"ok": True})
        result = await node_op.node_op("primary", "ping", {})
        assert result == {"ok": True}


class TestNonPrimaryReadiness:
    async def test_non_primary_ready_proceeds(self, monkeypatch) -> None:
        _patch_ready(monkeypatch, None)
        _patch_dispatch(monkeypatch, {"ok": True})
        result = await node_op.node_op("worker-1", "ping", {})
        assert result == {"ok": True}

    async def test_non_primary_not_ready_raises_404(self, monkeypatch) -> None:
        _patch_ready(monkeypatch, "machine-nodes extension not loaded")
        with pytest.raises(HTTPException) as exc:
            await node_op.node_op("worker-1", "ping", {})
        assert exc.value.status_code == 404
        assert exc.value.detail == "machine-nodes extension not loaded"


class TestExceptionTranslation:
    async def test_http_exception_passthrough(self, monkeypatch) -> None:
        _patch_dispatch(monkeypatch, HTTPException(status_code=418, detail="teapot"))
        with pytest.raises(HTTPException) as exc:
            await node_op.node_op("primary", "ping", {})
        assert exc.value.status_code == 418

    async def test_node_offline_maps_503_not_502(self, monkeypatch) -> None:
        # NodeOffline subclasses RuntimeError — ordering must catch it first
        _patch_dispatch(monkeypatch, node_link.NodeOffline("down"))
        with pytest.raises(HTTPException) as exc:
            await node_op.node_op("primary", "ping", {})
        assert exc.value.status_code == 503
        assert "down" in exc.value.detail

    async def test_timeout_maps_504(self, monkeypatch) -> None:
        _patch_dispatch(monkeypatch, asyncio.TimeoutError())
        with pytest.raises(HTTPException) as exc:
            await node_op.node_op("primary", "ping", {})
        assert exc.value.status_code == 504

    async def test_file_not_found_maps_404(self, monkeypatch) -> None:
        _patch_dispatch(monkeypatch, FileNotFoundError("missing"))
        with pytest.raises(HTTPException) as exc:
            await node_op.node_op("primary", "ping", {})
        assert exc.value.status_code == 404

    async def test_file_exists_maps_409(self, monkeypatch) -> None:
        _patch_dispatch(monkeypatch, FileExistsError("exists"))
        with pytest.raises(HTTPException) as exc:
            await node_op.node_op("primary", "ping", {})
        assert exc.value.status_code == 409

    async def test_permission_maps_403(self, monkeypatch) -> None:
        _patch_dispatch(monkeypatch, PermissionError("denied"))
        with pytest.raises(HTTPException) as exc:
            await node_op.node_op("primary", "ping", {})
        assert exc.value.status_code == 403

    async def test_value_error_maps_400(self, monkeypatch) -> None:
        _patch_dispatch(monkeypatch, ValueError("bad input"))
        with pytest.raises(HTTPException) as exc:
            await node_op.node_op("primary", "ping", {})
        assert exc.value.status_code == 400

    async def test_runtime_error_maps_502(self, monkeypatch) -> None:
        _patch_dispatch(monkeypatch, RuntimeError("remote handler failed"))
        with pytest.raises(HTTPException) as exc:
            await node_op.node_op("primary", "ping", {})
        assert exc.value.status_code == 502
