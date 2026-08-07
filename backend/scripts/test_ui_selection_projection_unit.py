from __future__ import annotations

import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import pytest

import _test_home

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_test_home.isolate("bc-test-ui-selection-projection-unit-")

import ui_selection_projection as mod  # noqa: E402
import ui_selection  # noqa: E402


def _event(*, sid=None, payload=None):
    return SimpleNamespace(sid=sid, payload=payload)


def _patch_close(monkeypatch, *, return_value=None, raises=None):
    """Replace `ui_selection.close_session_tabs` with a recorder."""
    seen: list[list[str]] = []

    def _close(session_ids):
        seen.append(list(session_ids))
        if raises is not None:
            raise raises
        return return_value

    monkeypatch.setattr(ui_selection, "close_session_tabs", _close)
    return seen


def _bind_capture(monkeypatch, broadcast=None):
    """Run `bind` with the bus patched out, returning the registered handler."""
    captured: dict = {}
    monkeypatch.setattr(mod.bus, "unsubscribe", lambda name: None)
    monkeypatch.setattr(
        mod.bus,
        "subscribe",
        lambda pattern, handler, *, priority=50, name=None, **kw: captured.update(
            handler=handler, pattern=pattern, priority=priority, name=name
        ),
    )
    mod.bind(broadcast if broadcast is not None else (lambda *a, **k: None))
    return captured


# --- close_tabs_for_deleted -------------------------------------------------


def test_close_tabs_returns_without_broadcast_when_snapshot_is_none(monkeypatch):
    _patch_close(monkeypatch, return_value=None)
    broadcast: list = []
    monkeypatch.setattr(mod, "_broadcast_global", lambda *a, **k: broadcast.append(a))

    asyncio.run(mod.close_tabs_for_deleted(["s1"]))

    assert broadcast == []


def test_close_tabs_returns_without_broadcast_when_no_bound_broadcaster(monkeypatch):
    _patch_close(monkeypatch, return_value={"tabs": []})
    broadcast: list = []
    monkeypatch.setattr(mod, "_broadcast_global", None)

    asyncio.run(mod.close_tabs_for_deleted(["s1"]))

    assert broadcast == []


def test_close_tabs_broadcasts_snapshot_when_present(monkeypatch):
    snapshot = {"tabs": [{"sid": "s2"}]}
    _patch_close(monkeypatch, return_value=snapshot)
    broadcast: list = []

    async def _bcast(*args, **kwargs):
        broadcast.append(args)

    monkeypatch.setattr(mod, "_broadcast_global", _bcast)

    asyncio.run(mod.close_tabs_for_deleted(["s1"]))

    assert broadcast == [("ui_selection_changed", snapshot)]


# --- _handler payload branches (L42-44) -------------------------------------


@pytest.mark.parametrize(
    ("payload", "sid", "expected"),
    [
        ({"deleted_sids": ["a", "b"]}, "root", ["a", "b"]),
        ({}, "s9", ["s9"]),                 # no list -> sid-only fallback
        (None, "s9", ["s9"]),               # payload None -> sid-only fallback
        ({}, "", []),                        # no list, no sid -> empty
        ({}, None, []),                      # no list, falsy sid -> empty
        ({"deleted_sids": []}, "s9", ["s9"]),  # empty list -> sid-only fallback
        ({"deleted_sids": "not-a-list"}, "s9", ["s9"]),  # wrong type -> fallback
    ],
)
def test_handler_resolves_deleted_sids(monkeypatch, payload, sid, expected):
    seen = _patch_close(monkeypatch, return_value=None)
    captured = _bind_capture(monkeypatch)

    asyncio.run(captured["handler"](_event(sid=sid, payload=payload)))

    assert seen == [expected]


# --- _handler exception path (L47-48) ---------------------------------------


def test_handler_logs_and_swallows_close_failure(monkeypatch, caplog):
    _patch_close(monkeypatch, raises=RuntimeError("boom"))
    captured = _bind_capture(monkeypatch)

    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        # Must not raise: the handler's except logs and returns.
        asyncio.run(captured["handler"](_event(sid="s9", payload={})))

    assert any("ui_selection tab cleanup failed" in r.message for r in caplog.records)


# --- bind / unbind wiring ---------------------------------------------------


def test_bind_subscribes_to_session_deleted(monkeypatch):
    recorded: dict = {}
    monkeypatch.setattr(mod.bus, "unsubscribe", lambda name: None)
    monkeypatch.setattr(
        mod.bus,
        "subscribe",
        lambda pattern, handler, *, priority=50, name=None, **kw: recorded.update(
            pattern=pattern, priority=priority, name=name
        ),
    )
    broadcast = lambda *a, **k: None  # noqa: E731
    mod.bind(broadcast)

    assert recorded == {
        "pattern": "session.deleted",
        "priority": 50,
        "name": mod._SUBSCRIBER_NAME,
    }
    assert mod._broadcast_global is broadcast


def test_bind_is_idempotent_unsubscribes_first(monkeypatch):
    unsub: list[str] = []
    monkeypatch.setattr(mod.bus, "unsubscribe", lambda name: unsub.append(name))
    monkeypatch.setattr(mod.bus, "subscribe", lambda *a, **k: None)

    mod.bind(lambda *a, **k: None)
    mod.bind(lambda *a, **k: None)

    # Each bind unsubscribes the prior registration before re-subscribing.
    assert unsub == [mod._SUBSCRIBER_NAME, mod._SUBSCRIBER_NAME]


def test_unbind_clears_subscription_and_broadcaster(monkeypatch):
    unsub: list[str] = []
    monkeypatch.setattr(mod.bus, "unsubscribe", lambda name: unsub.append(name))
    monkeypatch.setattr(mod.bus, "subscribe", lambda *a, **k: None)
    mod.bind(lambda *a, **k: None)

    mod.unbind()

    assert mod._broadcast_global is None
    assert unsub[-1] == mod._SUBSCRIBER_NAME
