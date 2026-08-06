#!/usr/bin/env python3
"""Unit coverage for backend/adapters/command_port.py's Protocol,
backend/surface_commands.py's implementation, and
backend/adapters/chat_adapter.py's ChatSurfaceAdapter.submit() dispatch
onto it (ADR 0006 command plane).

Isolated via `paths.engage_test_home` before any backend import (no real
`~/.better-claude` touched). No LLM/provider subprocess, no live claude
CLI turn — the orchestrator `Coordinator` and `session_manager` are fully
faked, matching backend/scripts/test_chat_adapter.py's isolation recipe.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_command_port.py -q
    PYTHONPATH=. python3 backend/scripts/test_command_port.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-command-port-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import surface_commands  # noqa: E402  (bare — matches ws_chat.py's own import)
from backend.adapters.chat_adapter import ChatSurfaceAdapter  # noqa: E402
from backend.adapters.command_port import CommandResult  # noqa: E402
from backend.surface_contract.intents import (  # noqa: E402
    DeleteQueued,
    EditQueued,
    IntentAccepted,
    IntentRejected,
    SendMode,
    SendPrompt,
    SendTarget,
    SendTargetKind,
    Stop,
)


# ---- fakes -----------------------------------------------------------


class _FakeTurnManager:
    def __init__(self, cancel_result: bool) -> None:
        self.cancel_result = cancel_result
        self.cancel_calls: list[str] = []

    async def cancel_turn(self, session_id, **kwargs):
        self.cancel_calls.append(session_id)
        return self.cancel_result


class _FakeCoordinator:
    """Stands in for backend.orchestrator.Coordinator: records every call
    surface_commands._ChatCommandPortImpl makes, at the exact depth
    reachable without a live provider."""

    def __init__(
        self,
        *,
        cancel_result: bool = True,
        update_result: bool = True,
        cancel_queued_result: bool = True,
    ) -> None:
        self.turn_manager = _FakeTurnManager(cancel_result)
        self.update_queued_calls: list[tuple] = []
        self.finish_queued_edit_calls: list[tuple] = []
        self.cancel_queued_calls: list[tuple] = []
        self._update_result = update_result
        self._cancel_queued_result = cancel_queued_result

    async def update_queued(self, session_id, queued_id, content):
        self.update_queued_calls.append((session_id, queued_id, content))
        return self._update_result

    def finish_queued_edit(self, session_id, queued_id):
        self.finish_queued_edit_calls.append((session_id, queued_id))

    def cancel_queued(self, session_id, queued_id=None):
        self.cancel_queued_calls.append((session_id, queued_id))
        return self._cancel_queued_result


class _FakeSessionManager:
    def __init__(self) -> None:
        self.update_calls: list[tuple] = []
        self.remove_calls: list[tuple] = []

    def update_queued_prompt(self, sid, queued_id, updates):
        self.update_calls.append((sid, queued_id, updates))
        return None

    def remove_queued_prompt(self, sid, queued_id):
        self.remove_calls.append((sid, queued_id))
        return None


class _FakePort:
    """Records calls made through ChatSurfaceAdapter.submit()'s dispatch,
    independent of the real surface_commands implementation — isolates
    "does submit() dispatch the right intent to the right method" from
    "does the port method do the right thing" (covered separately above)."""

    def __init__(self) -> None:
        self.stop_calls: list[tuple] = []
        self.edit_queued_calls: list[tuple] = []
        self.delete_queued_calls: list[tuple] = []

    async def stop(self, session_id):
        self.stop_calls.append((session_id,))
        return CommandResult(accepted=True)

    async def edit_queued(self, session_id, node_id, text):
        self.edit_queued_calls.append((session_id, node_id, text))
        return CommandResult(accepted=True)

    async def delete_queued(self, session_id, node_id=None):
        self.delete_queued_calls.append((session_id, node_id))
        return CommandResult(accepted=True)


# ---- port impl: stop ---------------------------------------------------


def test_stop_cancels_active_turn_and_reports_accepted() -> None:
    coordinator = _FakeCoordinator(cancel_result=True)
    port = surface_commands.build_chat_command_port(coordinator=coordinator)
    result = asyncio.run(port.stop("sess-1"))
    assert result == CommandResult(accepted=True)
    assert coordinator.turn_manager.cancel_calls == ["sess-1"]


def test_stop_no_active_turn_reports_not_accepted() -> None:
    coordinator = _FakeCoordinator(cancel_result=False)
    port = surface_commands.build_chat_command_port(coordinator=coordinator)
    result = asyncio.run(port.stop("sess-1"))
    assert result.accepted is False
    assert result.code == "no_active_turn"
    assert coordinator.turn_manager.cancel_calls == ["sess-1"]


# ---- port impl: edit_queued ---------------------------------------------


def test_edit_queued_moves_content_through_coordinator_and_session_manager() -> None:
    coordinator = _FakeCoordinator(update_result=True)
    fake_session_manager = _FakeSessionManager()
    port = surface_commands.build_chat_command_port(coordinator=coordinator)
    with mock.patch.object(surface_commands, "session_manager", fake_session_manager):
        result = asyncio.run(port.edit_queued("sess-1", "q-1", "new text"))
    assert result == CommandResult(accepted=True)
    assert coordinator.update_queued_calls == [("sess-1", "q-1", "new text")]
    assert fake_session_manager.update_calls == [
        ("sess-1", "q-1", {"content": "new text"}),
    ]
    assert coordinator.finish_queued_edit_calls == [("sess-1", "q-1")]


def test_edit_queued_not_queued_still_finishes_edit_lock() -> None:
    """Mirrors the legacy ws_chat.py handler: finish_queued_edit fires
    unconditionally, even when update_queued reports nothing was
    updated — never leaving a begin_queued_edit lock stuck open."""
    coordinator = _FakeCoordinator(update_result=False)
    fake_session_manager = _FakeSessionManager()
    port = surface_commands.build_chat_command_port(coordinator=coordinator)
    with mock.patch.object(surface_commands, "session_manager", fake_session_manager):
        result = asyncio.run(port.edit_queued("sess-1", "q-missing", "x"))
    assert result.accepted is False
    assert result.code == "not_queued"
    assert coordinator.finish_queued_edit_calls == [("sess-1", "q-missing")]


# ---- port impl: delete_queued -------------------------------------------


def test_delete_queued_cancels_and_removes() -> None:
    coordinator = _FakeCoordinator(cancel_queued_result=True)
    fake_session_manager = _FakeSessionManager()
    port = surface_commands.build_chat_command_port(coordinator=coordinator)
    with mock.patch.object(surface_commands, "session_manager", fake_session_manager):
        result = asyncio.run(port.delete_queued("sess-1", "q-1"))
    assert result == CommandResult(accepted=True)
    assert coordinator.cancel_queued_calls == [("sess-1", "q-1")]
    assert fake_session_manager.remove_calls == [("sess-1", "q-1")]


def test_delete_queued_defaults_node_id_to_none_for_clear_all() -> None:
    coordinator = _FakeCoordinator(cancel_queued_result=True)
    fake_session_manager = _FakeSessionManager()
    port = surface_commands.build_chat_command_port(coordinator=coordinator)
    with mock.patch.object(surface_commands, "session_manager", fake_session_manager):
        result = asyncio.run(port.delete_queued("sess-1"))
    assert result.accepted is True
    assert coordinator.cancel_queued_calls == [("sess-1", None)]
    assert fake_session_manager.remove_calls == [("sess-1", None)]


def test_delete_queued_not_queued_reports_not_accepted() -> None:
    coordinator = _FakeCoordinator(cancel_queued_result=False)
    fake_session_manager = _FakeSessionManager()
    port = surface_commands.build_chat_command_port(coordinator=coordinator)
    with mock.patch.object(surface_commands, "session_manager", fake_session_manager):
        result = asyncio.run(port.delete_queued("sess-1", "q-1"))
    assert result.accepted is False
    assert result.code == "not_queued"


# ---- chat_adapter.submit(): dispatch onto the port ----------------------


def _make_intent_base(intent_id: str, session_id: str) -> dict:
    return {"cv": 1, "intent_id": intent_id, "session_id": session_id}


def test_submit_stop_dispatches_to_port_and_accepts() -> None:
    async def _run() -> None:
        port = _FakePort()
        adapter = ChatSurfaceAdapter()
        adapter._command_port = port
        intent = Stop(**_make_intent_base("i-1", "sess-1"), turn_id="turn-1")
        ack = adapter.submit(intent)
        assert isinstance(ack, IntentAccepted)
        assert ack.intent_id == "i-1"
        await asyncio.sleep(0)  # let the scheduled task run
        assert port.stop_calls == [("sess-1",)]

    asyncio.run(_run())


def test_submit_edit_queued_dispatches_to_port_and_accepts() -> None:
    async def _run() -> None:
        port = _FakePort()
        adapter = ChatSurfaceAdapter()
        adapter._command_port = port
        intent = EditQueued(
            **_make_intent_base("i-2", "sess-1"), node_id="q-1", text="hi",
        )
        ack = adapter.submit(intent)
        assert isinstance(ack, IntentAccepted)
        await asyncio.sleep(0)
        assert port.edit_queued_calls == [("sess-1", "q-1", "hi")]

    asyncio.run(_run())


def test_submit_delete_queued_dispatches_to_port_and_accepts() -> None:
    async def _run() -> None:
        port = _FakePort()
        adapter = ChatSurfaceAdapter()
        adapter._command_port = port
        intent = DeleteQueued(**_make_intent_base("i-3", "sess-1"), node_id="q-1")
        ack = adapter.submit(intent)
        assert isinstance(ack, IntentAccepted)
        await asyncio.sleep(0)
        assert port.delete_queued_calls == [("sess-1", "q-1")]

    asyncio.run(_run())


def test_submit_unsupported_intent_kind_is_rejected_even_with_port_wired() -> None:
    async def _run() -> None:
        port = _FakePort()
        adapter = ChatSurfaceAdapter()
        adapter._command_port = port
        intent = SendPrompt(
            **_make_intent_base("i-4", "sess-1"),
            text="hi",
            attachments=(),
            send_mode=SendMode.QUEUE,
            target=SendTarget(kind=SendTargetKind.CURRENT),
        )
        ack = adapter.submit(intent)
        assert isinstance(ack, IntentRejected)
        assert ack.intent_id == "i-4"
        assert ack.code == "unsupported"
        await asyncio.sleep(0)
        assert port.stop_calls == []
        assert port.edit_queued_calls == []
        assert port.delete_queued_calls == []

    asyncio.run(_run())


def test_submit_stop_without_wired_port_uses_legacy_rejection() -> None:
    """No build_adapter(command_port=...) call happened — matches every
    pre-migration ChatSurfaceAdapter() construction (including
    backend/scripts/test_chat_adapter.py's existing coverage)."""
    adapter = ChatSurfaceAdapter()
    intent = Stop(**_make_intent_base("i-5", "sess-1"), turn_id="turn-1")
    ack = adapter.submit(intent)
    assert isinstance(ack, IntentRejected)
    assert ack.code == "unsupported_contract_phase"
    assert ack.message


_TESTS = [
    test_stop_cancels_active_turn_and_reports_accepted,
    test_stop_no_active_turn_reports_not_accepted,
    test_edit_queued_moves_content_through_coordinator_and_session_manager,
    test_edit_queued_not_queued_still_finishes_edit_lock,
    test_delete_queued_cancels_and_removes,
    test_delete_queued_defaults_node_id_to_none_for_clear_all,
    test_delete_queued_not_queued_reports_not_accepted,
    test_submit_stop_dispatches_to_port_and_accepts,
    test_submit_edit_queued_dispatches_to_port_and_accepts,
    test_submit_delete_queued_dispatches_to_port_and_accepts,
    test_submit_unsupported_intent_kind_is_rejected_even_with_port_wired,
    test_submit_stop_without_wired_port_uses_legacy_rejection,
]


def _run_standalone() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
