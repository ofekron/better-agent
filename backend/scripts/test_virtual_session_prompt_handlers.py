from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import virtual_session_prompt_handlers as vsp  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _isolate_handler_registry():
    vsp._handlers.clear()
    yield
    vsp._handlers.clear()


async def _noop_dispatch(_frame):
    return None


def test_register_rejects_empty_session_id():
    with pytest.raises(ValueError, match="session_id is required"):
        vsp.register("", lambda *a, **k: None)


def test_register_stores_handler():
    handler = lambda *a, **k: None  # noqa: E731
    vsp.register("s1", handler)
    assert vsp._handlers["s1"] is handler


async def test_handle_unregistered_session_returns_false():
    result = await vsp.handle(
        "missing",
        prompt="p",
        cwd="/",
        client_id=None,
        lifecycle_msg_id=None,
        dispatch_ws=_noop_dispatch,
    )
    assert result is False


async def test_handle_forwards_all_args_and_returns_handler_result():
    forwarded = {}

    async def handler(session_id, prompt, cwd, client_id, lifecycle_msg_id, dispatch_ws):
        forwarded["session_id"] = session_id
        forwarded["prompt"] = prompt
        forwarded["cwd"] = cwd
        forwarded["client_id"] = client_id
        forwarded["lifecycle_msg_id"] = lifecycle_msg_id
        forwarded["dispatch_ws"] = dispatch_ws
        return True

    vsp.register("s1", handler)
    result = await vsp.handle(
        "s1",
        prompt="hi",
        cwd="/x",
        client_id="c1",
        lifecycle_msg_id="l1",
        dispatch_ws=_noop_dispatch,
    )
    assert result is True
    assert forwarded == {
        "session_id": "s1",
        "prompt": "hi",
        "cwd": "/x",
        "client_id": "c1",
        "lifecycle_msg_id": "l1",
        "dispatch_ws": _noop_dispatch,
    }


async def test_handle_propagates_false_from_handler():
    async def handler(*_args, **_kw):
        return False

    vsp.register("s1", handler)
    result = await vsp.handle(
        "s1",
        prompt="p",
        cwd="/",
        client_id=None,
        lifecycle_msg_id=None,
        dispatch_ws=_noop_dispatch,
    )
    assert result is False


async def test_handler_can_dispatch_ws_frames():
    sent = []

    async def recording_dispatch(frame):
        sent.append(frame)

    async def handler(_sid, _prompt, _cwd, _client_id, _lifecycle, dispatch_ws):
        await dispatch_ws({"type": "ack"})
        await dispatch_ws({"type": "progress"})
        return True

    vsp.register("s1", handler)
    await vsp.handle(
        "s1",
        prompt="p",
        cwd="/",
        client_id=None,
        lifecycle_msg_id=None,
        dispatch_ws=recording_dispatch,
    )
    assert sent == [{"type": "ack"}, {"type": "progress"}]


async def test_register_replaces_existing_handler():
    async def first(*_a, **_k):
        return True

    async def second(*_a, **_k):
        return False

    vsp.register("s1", first)
    vsp.register("s1", second)
    result = await vsp.handle(
        "s1", prompt="p", cwd="/", client_id=None, lifecycle_msg_id=None, dispatch_ws=_noop_dispatch
    )
    assert result is False
