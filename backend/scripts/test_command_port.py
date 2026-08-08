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

import session_bridge  # noqa: E402  (bare — matches surface_commands.py's own import)
import surface_commands  # noqa: E402  (bare — matches ws_chat.py's own import)
import tool_approval  # noqa: E402  (bare — matches surface_commands.py's own import)
import user_input_store  # noqa: E402  (bare — matches surface_commands.py's own import)
from backend.adapters.chat_adapter import ChatSurfaceAdapter  # noqa: E402
from backend.adapters.command_port import CommandResult  # noqa: E402
from backend.surface_contract.intents import (  # noqa: E402
    ApprovalResponse,
    ChoiceResponse,
    DeleteQueued,
    EditQueued,
    InputResponse,
    IntentAccepted,
    IntentRejected,
    ResolveInteraction,
    Rewind,
    SetSelectors,
    Stop,
)
from backend.surface_contract.nodes import (  # noqa: E402
    ApprovalDecision,
    DELEGATE_CHOICE_REF_PREFIX,
    TOOL_APPROVAL_REF_PREFIX,
    USER_INPUT_REF_PREFIX,
    WORKER_APPROVAL_REF_PREFIX,
)
from stores import pending_approvals  # noqa: E402  (bare — matches surface_commands.py's own import)


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
        self.resolve_approval_calls: list[tuple] = []
        self._update_result = update_result
        self._cancel_queued_result = cancel_queued_result

    async def update_queued(self, session_id, queued_id, content):
        self.update_queued_calls.append((session_id, queued_id, content))
        return self._update_result

    def _resolve_approval(self, delegation_id, rec):
        """Stands in for `orchestrator.Coordinator._resolve_approval` —
        the worker-spawn callback `resolve_interaction`'s worker-approval
        branch invokes on the SAME function object
        `pending_approvals_api.configure` binds (see that port method's
        docstring)."""
        self.resolve_approval_calls.append((delegation_id, rec))

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
        self.rewind_calls: list[tuple] = []
        self.resolve_interaction_calls: list[tuple] = []

    async def stop(self, session_id):
        self.stop_calls.append((session_id,))
        return CommandResult(accepted=True)

    async def edit_queued(self, session_id, node_id, text):
        self.edit_queued_calls.append((session_id, node_id, text))
        return CommandResult(accepted=True)

    async def rewind(self, session_id, node_id):
        self.rewind_calls.append((session_id, node_id))
        return CommandResult(accepted=True)

    async def resolve_interaction(self, session_id, interaction_ref, response):
        self.resolve_interaction_calls.append((session_id, interaction_ref, response))
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


# ---- port impl: resolve_interaction (ADR 0006 §5) ------------------------
#
# Each of the 3 legacy mechanisms is exercised against its REAL store
# (`tool_approval.registry`, `stores.pending_approvals`, `session_bridge`)
# — resolve_interaction's whole point is routing to that SAME store, so a
# fake store would only prove the routing logic against itself.


def test_resolve_tool_approval_approve_decides_the_real_registry() -> None:
    async def _run() -> None:
        rec = tool_approval.registry.create(
            app_session_id="sess-1", run_id="run-1", provider_kind="claude",
            tool_name="Bash", summary={"command": "ls"},
        )
        coordinator = _FakeCoordinator()
        port = surface_commands.build_chat_command_port(coordinator=coordinator)
        result = await port.resolve_interaction(
            "sess-1", f"{TOOL_APPROVAL_REF_PREFIX}{rec.approval_id}",
            ApprovalResponse(decision=ApprovalDecision.APPROVE),
        )
        assert result == CommandResult(accepted=True)
        assert rec.future.done()
        assert rec.future.result() is True
    asyncio.run(_run())


def test_resolve_tool_approval_deny_decides_false() -> None:
    async def _run() -> None:
        rec = tool_approval.registry.create(
            app_session_id="sess-1", run_id="run-1", provider_kind="claude",
            tool_name="Bash", summary={},
        )
        port = surface_commands.build_chat_command_port(coordinator=_FakeCoordinator())
        result = await port.resolve_interaction(
            "sess-1", f"{TOOL_APPROVAL_REF_PREFIX}{rec.approval_id}",
            ApprovalResponse(decision=ApprovalDecision.DENY),
        )
        assert result == CommandResult(accepted=True)
        assert rec.future.result() is False
    asyncio.run(_run())


def test_resolve_tool_approval_wrong_session_is_not_found() -> None:
    """Ref-scoping (ADR 0006 §0): the approval belongs to 'sess-1', a
    resolve for 'sess-OTHER' must be rejected — never resolved across
    sessions."""
    async def _run() -> None:
        rec = tool_approval.registry.create(
            app_session_id="sess-1", run_id="run-1", provider_kind="claude",
            tool_name="Bash", summary={},
        )
        port = surface_commands.build_chat_command_port(coordinator=_FakeCoordinator())
        result = await port.resolve_interaction(
            "sess-OTHER", f"{TOOL_APPROVAL_REF_PREFIX}{rec.approval_id}",
            ApprovalResponse(decision=ApprovalDecision.APPROVE),
        )
        assert result.accepted is False
        assert result.code == "not_found"
        assert not rec.future.done()
    asyncio.run(_run())


def test_resolve_tool_approval_rejects_a_mismatched_response_kind() -> None:
    async def _run() -> None:
        rec = tool_approval.registry.create(
            app_session_id="sess-1", run_id="run-1", provider_kind="claude",
            tool_name="Bash", summary={},
        )
        port = surface_commands.build_chat_command_port(coordinator=_FakeCoordinator())
        result = await port.resolve_interaction(
            "sess-1", f"{TOOL_APPROVAL_REF_PREFIX}{rec.approval_id}",
            ChoiceResponse(picked_ref="x"),
        )
        assert result.accepted is False
        assert result.code == "invalid_response"
        assert not rec.future.done()
    asyncio.run(_run())


def test_resolve_worker_approval_approve_transitions_store_and_calls_coordinator() -> None:
    async def _run() -> None:
        rec = pending_approvals.create(
            delegation_id=f"deleg-{id(_run)}", app_session_id="sess-1", cwd="/tmp",
            justification="need it", proposed_description="a worker",
            proposed_orchestration_mode="native", instructions_preview="", model="claude",
        )
        try:
            coordinator = _FakeCoordinator()
            port = surface_commands.build_chat_command_port(coordinator=coordinator)
            result = await port.resolve_interaction(
                "sess-1", f"{WORKER_APPROVAL_REF_PREFIX}{rec['delegation_id']}",
                ApprovalResponse(decision=ApprovalDecision.APPROVE),
            )
            assert result == CommandResult(accepted=True)
            stored = pending_approvals.get(rec["delegation_id"])
            assert stored["status"] == "approved"
            assert coordinator.resolve_approval_calls
            assert coordinator.resolve_approval_calls[0][0] == rec["delegation_id"]
        finally:
            pending_approvals.delete(rec["delegation_id"])
    asyncio.run(_run())


def test_resolve_worker_approval_wrong_session_is_forbidden() -> None:
    async def _run() -> None:
        rec = pending_approvals.create(
            delegation_id=f"deleg-{id(_run)}-b", app_session_id="sess-1", cwd="/tmp",
            justification="", proposed_description="", proposed_orchestration_mode="native",
            instructions_preview="", model="claude",
        )
        try:
            coordinator = _FakeCoordinator()
            port = surface_commands.build_chat_command_port(coordinator=coordinator)
            result = await port.resolve_interaction(
                "sess-OTHER", f"{WORKER_APPROVAL_REF_PREFIX}{rec['delegation_id']}",
                ApprovalResponse(decision=ApprovalDecision.APPROVE),
            )
            assert result.accepted is False
            assert result.code == "forbidden"
            assert pending_approvals.get(rec["delegation_id"])["status"] == "pending"
            assert not coordinator.resolve_approval_calls
        finally:
            pending_approvals.delete(rec["delegation_id"])
    asyncio.run(_run())


def test_resolve_delegate_choice_pick_resolves_the_real_pending_future() -> None:
    async def _run() -> None:
        delegation_id = f"sbd-{id(_run)}"
        fut = asyncio.get_running_loop().create_future()
        session_bridge._pending[delegation_id] = {
            "future": fut, "caller_sid": "sess-1", "caller_msg_id": "msg-1",
            "target_sid": "target-sess", "prompt": "do it", "run_mode": "fork",
            "proposed_ids": ["target-sess"],
        }
        fake_session_manager = mock.Mock()
        try:
            port = surface_commands.build_chat_command_port(coordinator=_FakeCoordinator())
            with mock.patch.object(session_bridge, "session_manager", fake_session_manager):
                result = await port.resolve_interaction(
                    "sess-1", f"{DELEGATE_CHOICE_REF_PREFIX}{delegation_id}",
                    ChoiceResponse(picked_ref="target-sess"),
                )
            assert result == CommandResult(accepted=True)
            assert fut.done()
            assert fut.result() == "target-sess"
            assert fake_session_manager.set_msg_ask_result.called
        finally:
            session_bridge._pending.pop(delegation_id, None)
    asyncio.run(_run())


def test_resolve_delegate_choice_wrong_session_is_forbidden() -> None:
    async def _run() -> None:
        delegation_id = f"sbd-{id(_run)}-b"
        fut = asyncio.get_running_loop().create_future()
        session_bridge._pending[delegation_id] = {
            "future": fut, "caller_sid": "sess-1", "caller_msg_id": "msg-1",
            "target_sid": "target-sess", "prompt": "do it", "run_mode": "fork",
            "proposed_ids": ["target-sess"],
        }
        try:
            port = surface_commands.build_chat_command_port(coordinator=_FakeCoordinator())
            result = await port.resolve_interaction(
                "sess-OTHER", f"{DELEGATE_CHOICE_REF_PREFIX}{delegation_id}",
                ChoiceResponse(picked_ref="target-sess"),
            )
            assert result.accepted is False
            assert result.code == "forbidden"
            assert not fut.done()
        finally:
            session_bridge._pending.pop(delegation_id, None)
    asyncio.run(_run())


def test_resolve_interaction_unknown_ref_prefix_is_rejected() -> None:
    async def _run() -> None:
        port = surface_commands.build_chat_command_port(coordinator=_FakeCoordinator())
        result = await port.resolve_interaction(
            "sess-1", "some_other_namespace:abc", ApprovalResponse(decision=ApprovalDecision.APPROVE),
        )
        assert result.accepted is False
        assert result.code == "unknown_interaction"
    asyncio.run(_run())


# ---- resolve_interaction: user-input (4th mechanism, kind INPUT) -------
# Routes to `user_input_store.resolve_request` directly (real store, not a
# fake) — same rationale as the 3 mechanisms above: resolve_interaction's
# whole point is routing to that SAME store.


def test_resolve_user_input_questions_resolves_the_real_store() -> None:
    async def _run() -> None:
        req = user_input_store.create_request(
            app_session_id="sess-1",
            questions=[{"id": "q1", "header": "H", "question": "Proceed?", "options": []}],
            timeout_seconds=60,
        )
        port = surface_commands.build_chat_command_port(coordinator=_FakeCoordinator())
        result = await port.resolve_interaction(
            "sess-1", f"{USER_INPUT_REF_PREFIX}{req['request_id']}",
            InputResponse(response={"answers": {"q1": "Yes"}}),
        )
        assert result == CommandResult(accepted=True)
        stored = user_input_store.get_request(req["request_id"])
        assert stored["status"] == "resolved"
        assert stored["response"] == {"q1": "Yes"}
    asyncio.run(_run())


def test_resolve_user_input_approval_kind_validates_response_shape() -> None:
    async def _run() -> None:
        req = user_input_store.create_request(
            app_session_id="sess-1", kind="approval", questions=[], prompt="Deploy?",
            timeout_seconds=60,
        )
        port = surface_commands.build_chat_command_port(coordinator=_FakeCoordinator())
        bad = await port.resolve_interaction(
            "sess-1", f"{USER_INPUT_REF_PREFIX}{req['request_id']}",
            InputResponse(response={"approved": "not-a-bool"}),
        )
        assert bad.accepted is False
        assert bad.code == "invalid_response"
        assert user_input_store.get_request(req["request_id"])["status"] == "pending"

        good = await port.resolve_interaction(
            "sess-1", f"{USER_INPUT_REF_PREFIX}{req['request_id']}",
            InputResponse(response={"approved": True}),
        )
        assert good == CommandResult(accepted=True)
        assert user_input_store.get_request(req["request_id"])["response"] == {"approved": True}
    asyncio.run(_run())


def test_resolve_user_input_wrong_session_is_forbidden() -> None:
    async def _run() -> None:
        req = user_input_store.create_request(
            app_session_id="sess-1",
            questions=[{"id": "q1", "header": "H", "question": "Proceed?", "options": []}],
            timeout_seconds=60,
        )
        port = surface_commands.build_chat_command_port(coordinator=_FakeCoordinator())
        result = await port.resolve_interaction(
            "sess-OTHER", f"{USER_INPUT_REF_PREFIX}{req['request_id']}",
            InputResponse(response={"answers": {"q1": "Yes"}}),
        )
        assert result.accepted is False
        assert result.code == "forbidden"
        assert user_input_store.get_request(req["request_id"])["status"] == "pending"
    asyncio.run(_run())


def test_resolve_user_input_rejects_a_mismatched_response_kind() -> None:
    async def _run() -> None:
        req = user_input_store.create_request(
            app_session_id="sess-1",
            questions=[{"id": "q1", "header": "H", "question": "Proceed?", "options": []}],
            timeout_seconds=60,
        )
        port = surface_commands.build_chat_command_port(coordinator=_FakeCoordinator())
        result = await port.resolve_interaction(
            "sess-1", f"{USER_INPUT_REF_PREFIX}{req['request_id']}",
            ApprovalResponse(decision=ApprovalDecision.APPROVE),
        )
        assert result.accepted is False
        assert result.code == "invalid_response"
        assert user_input_store.get_request(req["request_id"])["status"] == "pending"
    asyncio.run(_run())


def test_resolve_user_input_unknown_request_id_is_not_found() -> None:
    async def _run() -> None:
        port = surface_commands.build_chat_command_port(coordinator=_FakeCoordinator())
        result = await port.resolve_interaction(
            "sess-1", f"{USER_INPUT_REF_PREFIX}does-not-exist",
            InputResponse(response={"answers": {"q1": "Yes"}}),
        )
        assert result.accepted is False
        assert result.code == "not_found"
    asyncio.run(_run())


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


def test_submit_rewind_dispatches_to_port_and_accepts() -> None:
    """`ChatSurfaceAdapter.submit()` now dispatches Rewind onto the port
    (previously unwired — ADR 0006 §5 alter/resolve_interaction pass);
    `port.rewind` itself may still return `unsupported` (see
    `surface_commands._ChatCommandPortImpl.rewind`) — this test covers
    only the dispatch wiring, matching `test_submit_stop_dispatches_to_
    port_and_accepts`'s own scope."""
    async def _run() -> None:
        port = _FakePort()
        adapter = ChatSurfaceAdapter()
        adapter._command_port = port
        intent = Rewind(**_make_intent_base("i-4", "sess-1"), node_id="node-1")
        ack = adapter.submit(intent)
        assert isinstance(ack, IntentAccepted)
        assert ack.intent_id == "i-4"
        await asyncio.sleep(0)
        assert port.rewind_calls == [("sess-1", "node-1")]

    asyncio.run(_run())


def test_submit_resolve_interaction_dispatches_to_port_and_accepts() -> None:
    async def _run() -> None:
        port = _FakePort()
        adapter = ChatSurfaceAdapter()
        adapter._command_port = port
        response = ApprovalResponse(decision=ApprovalDecision.APPROVE)
        intent = ResolveInteraction(
            **_make_intent_base("i-5", "sess-1"),
            interaction_ref=f"{TOOL_APPROVAL_REF_PREFIX}abc", response=response,
        )
        ack = adapter.submit(intent)
        assert isinstance(ack, IntentAccepted)
        assert ack.intent_id == "i-5"
        await asyncio.sleep(0)
        assert port.resolve_interaction_calls == [
            ("sess-1", f"{TOOL_APPROVAL_REF_PREFIX}abc", response),
        ]

    asyncio.run(_run())


def test_submit_unsupported_intent_kind_is_rejected_even_with_port_wired() -> None:
    """SetSelectors is not yet migrated onto ChatSurfaceAdapter.submit()'s
    isinstance dispatch (unlike Stop/EditQueued/DeleteQueued/Rewind/
    ResolveInteraction/SendPrompt — see
    backend/scripts/test_send_prompt_port.py for SendPrompt's own
    dispatch coverage)."""
    async def _run() -> None:
        port = _FakePort()
        adapter = ChatSurfaceAdapter()
        adapter._command_port = port
        intent = SetSelectors(**_make_intent_base("i-6", "sess-1"))
        ack = adapter.submit(intent)
        assert isinstance(ack, IntentRejected)
        assert ack.intent_id == "i-6"
        assert ack.code == "unsupported"
        await asyncio.sleep(0)
        assert port.stop_calls == []
        assert port.edit_queued_calls == []
        assert port.delete_queued_calls == []
        assert port.rewind_calls == []
        assert port.resolve_interaction_calls == []

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
    test_resolve_tool_approval_approve_decides_the_real_registry,
    test_resolve_tool_approval_deny_decides_false,
    test_resolve_tool_approval_wrong_session_is_not_found,
    test_resolve_tool_approval_rejects_a_mismatched_response_kind,
    test_resolve_worker_approval_approve_transitions_store_and_calls_coordinator,
    test_resolve_worker_approval_wrong_session_is_forbidden,
    test_resolve_delegate_choice_pick_resolves_the_real_pending_future,
    test_resolve_delegate_choice_wrong_session_is_forbidden,
    test_resolve_interaction_unknown_ref_prefix_is_rejected,
    test_resolve_user_input_questions_resolves_the_real_store,
    test_resolve_user_input_approval_kind_validates_response_shape,
    test_resolve_user_input_wrong_session_is_forbidden,
    test_resolve_user_input_rejects_a_mismatched_response_kind,
    test_resolve_user_input_unknown_request_id_is_not_found,
    test_submit_stop_dispatches_to_port_and_accepts,
    test_submit_edit_queued_dispatches_to_port_and_accepts,
    test_submit_delete_queued_dispatches_to_port_and_accepts,
    test_submit_rewind_dispatches_to_port_and_accepts,
    test_submit_resolve_interaction_dispatches_to_port_and_accepts,
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
