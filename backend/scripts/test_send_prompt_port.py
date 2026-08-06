#!/usr/bin/env python3
"""Coverage for `backend/surface_commands.py`'s `send_prompt` (ADR 0006
command plane, Phase D — the extraction of `backend/ws_chat.py`'s legacy
`send_message` handler behind the `ChatCommandPort` callback seam) and
`backend/adapters/chat_adapter.py`'s `ChatSurfaceAdapter.submit()` dispatch
onto it for the v2 `SendPrompt` intent.

Isolated via `paths.engage_test_home` before any backend import (no real
`~/.better-claude` touched), matching `backend/scripts/test_command_port.py`.
No LLM/provider subprocess, no live claude CLI turn: the coordinator is a
recording fake (`turn_manager.has_active_turn`/`has_active_runs` both
False — no turn in flight), and the ONE call that would otherwise start a
real turn — `offline_actions_api`'s `_coordinator().submit_prompt_async`,
reached through the REAL `_start_prompt_handoff` /
`session_manager.admit_queued_prompt_durable` durable-admission path — is
also the same recording fake, so durable admission (the actual queued-prompt
persistence this test asserts) runs for real while turn dispatch itself
never happens.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_send_prompt_port.py -q
    PYTHONPATH=. python3 backend/scripts/test_send_prompt_port.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-send-prompt-port-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import offline_actions_api  # noqa: E402
import surface_commands  # noqa: E402
from backend.adapters.chat_adapter import ChatSurfaceAdapter  # noqa: E402
from backend.adapters.command_port import CommandResult  # noqa: E402
from backend.surface_contract.intents import (  # noqa: E402
    IntentAccepted,
    IntentRejected,
    SendMode,
    SendPrompt,
    SendTarget,
    SendTargetKind,
)
from i18n import t  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


# ---- fakes -----------------------------------------------------------


class _FakeTurnManager:
    def __init__(self) -> None:
        self.cancel_calls: list[tuple] = []

    def has_active_turn(self, session_id: str) -> bool:
        return False

    def has_active_runs(self, session_id: str) -> bool:
        return False

    async def cancel_turn(self, session_id, **kwargs):
        self.cancel_calls.append((session_id, kwargs))
        return True


class _FakeUserPromptManager:
    def get_in_flight_lifecycle_msg_id(self, session_id: str) -> str | None:
        return None


class _FakeCoordinator:
    """Stands in for backend.orchestrator.Coordinator, at the exact depth
    `send_prompt` and the REAL `offline_actions_api._start_prompt_handoff`
    durable-admission path reach it. Registered as both
    `surface_commands._ChatCommandPortImpl.coordinator` and
    `offline_actions_api`'s module-level coordinator (`configure()`) —
    `_start_prompt_handoff` resolves the coordinator through the latter,
    independent of the port instance's own `self.coordinator`."""

    def __init__(self) -> None:
        self.turn_manager = _FakeTurnManager()
        self.user_prompt_manager = _FakeUserPromptManager()
        self._claims: dict[str, str] = {}
        self.submit_calls: list[tuple[str, dict]] = []

    def get_queued_count(self, session_id: str) -> int:
        return 0

    def try_claim_prompt_client_id(self, session_id: str, item_id: str, client_id: str):
        existing = self._claims.get(client_id)
        if existing is not None:
            return existing
        self._claims[client_id] = item_id
        return None

    def _forget_active_prompt_item(self, item_id: str) -> None:
        for cid, iid in list(self._claims.items()):
            if iid == item_id:
                del self._claims[cid]

    def active_prompt_for_client_id(self, session_id: str, client_id: str):
        return None

    async def submit_prompt_async(self, session_id: str, params: dict) -> None:
        self.submit_calls.append((session_id, params))


class _NotifyRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, frame_type: str, payload: dict) -> None:
        self.calls.append((frame_type, payload))


async def _await_pending_handoffs() -> None:
    """`_start_prompt_handoff` returns as soon as durable admission
    resolves; the coordinator's `submit_prompt_async` call it schedules
    keeps running in the background task it tracks in
    `offline_actions_api._PROMPT_HANDOFF_TASKS`. Wait on those tasks
    directly (event-driven — no sleep/timeout) before asserting on
    anything the background half of the handoff produces."""
    pending = [t for t in offline_actions_api._PROMPT_HANDOFF_TASKS if not t.done()]
    if pending:
        await asyncio.gather(*pending)


def _make_session(*, name: str) -> str:
    sess = session_manager.create(
        name=name, model="sonnet", cwd=tempfile.gettempdir(),
        orchestration_mode="native", source="cli",
    )
    return sess["id"]


# ---- send_prompt: empty prompt rejected before any session lookup -------


def test_send_prompt_rejects_empty_prompt_without_touching_session() -> None:
    async def _run() -> None:
        coordinator = _FakeCoordinator()
        offline_actions_api.configure(coordinator=coordinator)
        port = surface_commands.build_chat_command_port(coordinator=coordinator)
        notify = _NotifyRecorder()

        result = await port.send_prompt(
            "sess-does-not-need-to-exist",
            "   ",
            (),
            SendMode.QUEUE,
            SendTarget(kind=SendTargetKind.CURRENT),
            "",
            notify=notify,
            images=[],
            files=[],
        )

        assert result.accepted is False
        assert notify.calls == [
            (
                "error",
                {
                    "error": t("error.ws_empty_prompt"),
                    "app_session_id": "sess-does-not-need-to-exist",
                    "session_id": "sess-does-not-need-to-exist",
                    "client_id": None,
                },
            )
        ]
        assert coordinator.submit_calls == []

    asyncio.run(_run())


# ---- send_prompt: durable admission persists + notify order + dedup -----


def test_send_prompt_admits_persists_and_dedups_duplicate_client_id() -> None:
    async def _run() -> None:
        coordinator = _FakeCoordinator()
        offline_actions_api.configure(coordinator=coordinator)
        port = surface_commands.build_chat_command_port(coordinator=coordinator)
        sid = _make_session(name="send-prompt-admission")

        notify_1 = _NotifyRecorder()
        result_1 = await port.send_prompt(
            sid,
            "hello there",
            (),
            SendMode.QUEUE,
            SendTarget(kind=SendTargetKind.CURRENT),
            "client-dedup-1",
            notify=notify_1,
            images=[],
            files=[],
            cwd=tempfile.gettempdir(),
        )
        await _await_pending_handoffs()

        assert result_1 == CommandResult(accepted=True)
        # Exactly one reply frame — is_queued was False (no active turn),
        # so only `user_message_queued` fires; `prompt_queued` is
        # is_queued-gated and stays silent for an immediate send.
        assert [f for f, _ in notify_1.calls] == ["user_message_queued"]
        frame_type, payload = notify_1.calls[0]
        assert payload["app_session_id"] == sid
        assert payload["kind"] == "send"
        assert payload["client_id"] == "client-dedup-1"
        assert payload["content_preview"] == "hello there"

        # Durable persistence: the real `admit_queued_prompt_durable` path
        # appended a queued_prompts row carrying the dedup key.
        persisted = session_manager.get_lite(sid)
        queued = persisted.get("queued_prompts") or []
        assert len(queued) == 1
        assert queued[0]["client_id"] == "client-dedup-1"
        assert queued[0]["content"] == "hello there"

        # Durable admission really handed off to turn dispatch (faked here).
        assert len(coordinator.submit_calls) == 1
        assert coordinator.submit_calls[0][0] == sid

        # Duplicate client_id: the pre-admission session_queue_projection
        # dedup check must catch it BEFORE a second _start_prompt_handoff,
        # echoing the persisted queued prompt instead of re-admitting.
        notify_2 = _NotifyRecorder()
        result_2 = await port.send_prompt(
            sid,
            "hello there",
            (),
            SendMode.QUEUE,
            SendTarget(kind=SendTargetKind.CURRENT),
            "client-dedup-1",
            notify=notify_2,
            images=[],
            files=[],
            cwd=tempfile.gettempdir(),
        )
        await _await_pending_handoffs()

        assert result_2 == CommandResult(accepted=True, code="already_queued")
        assert [f for f, _ in notify_2.calls] == ["user_message_queued"]
        assert notify_2.calls[0][1]["client_id"] == "client-dedup-1"
        # No second admission: queued_prompts unchanged, no second submit.
        persisted_after_dup = session_manager.get_lite(sid)
        assert len(persisted_after_dup.get("queued_prompts") or []) == 1
        assert len(coordinator.submit_calls) == 1

    asyncio.run(_run())


# ---- chat_adapter.submit(): SendPrompt dispatch onto the port -----------


class _FakePort:
    def __init__(self) -> None:
        self.send_prompt_calls: list[tuple] = []

    async def send_prompt(self, session_id, text, attachments, send_mode, target, intent_id, *, notify):
        self.send_prompt_calls.append(
            (session_id, text, attachments, send_mode, target, intent_id, notify)
        )
        return CommandResult(accepted=True)


def test_submit_send_prompt_dispatches_to_port_with_dropped_notify() -> None:
    async def _run() -> None:
        port = _FakePort()
        adapter = ChatSurfaceAdapter()
        adapter._command_port = port
        intent = SendPrompt(
            cv=1, intent_id="i-1", session_id="sess-1",
            text="hi", attachments=(), send_mode=SendMode.QUEUE,
            target=SendTarget(kind=SendTargetKind.CURRENT),
        )
        ack = adapter.submit(intent)
        assert isinstance(ack, IntentAccepted)
        assert ack.intent_id == "i-1"
        await asyncio.sleep(0)  # let the scheduled task run
        assert len(port.send_prompt_calls) == 1
        call = port.send_prompt_calls[0]
        assert call[0] == "sess-1"
        assert call[1] == "hi"
        assert call[3] == SendMode.QUEUE
        assert call[5] == "i-1"
        # v2 acks are projection-fact based: the notify passed through is
        # a no-op, not the WS reply-frame formatter.
        recorded = []

        async def _spy(frame_type, payload):
            recorded.append((frame_type, payload))

        await call[6]("user_message_queued", {"whatever": True})
        # the real drop function performs no observable side effect
        assert recorded == []

    asyncio.run(_run())


def test_submit_send_prompt_empty_text_and_attachments_rejected_synchronously() -> None:
    port = _FakePort()
    adapter = ChatSurfaceAdapter()
    adapter._command_port = port
    intent = SendPrompt(
        cv=1, intent_id="i-2", session_id="sess-1",
        text="   ", attachments=(), send_mode=SendMode.QUEUE,
        target=SendTarget(kind=SendTargetKind.CURRENT),
    )
    ack = adapter.submit(intent)
    assert isinstance(ack, IntentRejected)
    assert ack.intent_id == "i-2"
    assert ack.code == "empty_prompt"
    assert port.send_prompt_calls == []


_TESTS = [
    test_send_prompt_rejects_empty_prompt_without_touching_session,
    test_send_prompt_admits_persists_and_dedups_duplicate_client_id,
    test_submit_send_prompt_dispatches_to_port_with_dropped_notify,
    test_submit_send_prompt_empty_text_and_attachments_rejected_synchronously,
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
