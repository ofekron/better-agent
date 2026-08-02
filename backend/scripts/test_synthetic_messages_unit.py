"""Unit tests for ``synthetic_messages``.

Async orchestrators that inject a synthetic prompt into an existing session
(``inject``) and append an operator-authored assistant message
(``append_operator_message``). They coordinate ``session_manager``,
``session_readiness``, ``user_msg_lifecycle``, the coordinator's turn
manager, and (lazily) ``event_journal``. None of those backends are exercised
here — every collaborator is replaced with a recording fake via monkeypatch,
so these tests pin pure orchestration logic, not wiring.

Pins:
  1. ``inject``: ``session_id`` / ``prompt`` validation, session-not-found,
     the three ``is_queued`` disjuncts (active turn, active runs, awaiting
     fork provisioning) vs. an immediate send, ``display_prompt`` /
     ``orchestration_mode`` / ``model`` / ``cwd`` / ``client_id`` fallbacks,
     submit-failure queued-prompt rollback + re-raise, and the lifecycle
     emit + return shape.
  2. ``append_operator_message``: ``session_id`` / ``content`` validation,
     session-not-found from ``get_lite`` and from a ``None`` append result,
     happy-path journal publish, and the ``append_assistant_message`` alias.

Async entry points are driven through ``asyncio.run`` (the repo runs its
async tests this way; there is no pytest-asyncio configured).

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_synthetic_messages_unit.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-synthetic-messages-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import synthetic_messages as sm  # noqa: E402


# --- recording collaborators -------------------------------------------


class _TurnManager:
    def __init__(self, active_turn=False, active_runs=False):
        self._turn = active_turn
        self._runs = active_runs

    def has_active_turn(self, _sid):
        return self._turn

    def has_active_runs(self, _sid):
        return self._runs


class _Coordinator:
    """Records ``submit_prompt_async`` params + queued count; controls queueing."""

    def __init__(self, *, active_turn=False, active_runs=False, queued_count=0,
                 submit_raises=None):
        self.turn_manager = _TurnManager(active_turn, active_runs)
        self._queued_count = queued_count
        self._submit_raises = submit_raises
        self.submitted = []

    def get_queued_count(self, _sid):
        return self._queued_count

    async def submit_prompt_async(self, sid, params):
        self.submitted.append((sid, params))
        if self._submit_raises:
            raise self._submit_raises


class _SessionManager:
    def __init__(self, session=None, append_result="truthy"):
        self._session = session
        self._append_result = append_result
        self.added = []
        self.removed = []
        self.appended = []

    def get_lite(self, _sid):
        return self._session

    def add_queued_prompt(self, sid, prompt):
        self.added.append((sid, prompt))

    def remove_queued_prompt(self, sid, item_id):
        self.removed.append((sid, item_id))

    def append_assistant_msg(self, sid, msg):
        self.appended.append((sid, msg))
        return self._append_result


class _Readiness:
    def __init__(self, awaiting=False):
        self._awaiting = awaiting

    def is_awaiting_fork_provisioning(self, _sid):
        return self._awaiting


@pytest.fixture
def fake_lifecycle(monkeypatch):
    """Replace ``emit_queued`` (async) + ``new_lifecycle_msg_id`` with recorders."""
    calls = []

    async def _emit_queued(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(sm, "emit_queued", _emit_queued)
    monkeypatch.setattr(sm, "new_lifecycle_msg_id", lambda: "lifecycle-id-1")
    return calls


@pytest.fixture
def fake_journal(monkeypatch):
    """Inject a recording ``event_journal`` module for the lazy import."""
    published = []
    journal = types.ModuleType("event_journal")

    async def _publish_event(**kwargs):
        published.append(kwargs)

    journal.publish_event = _publish_event  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "event_journal", journal)
    return published


def _patch_backends(monkeypatch, *, session=None, append_result="truthy",
                    active_turn=False, active_runs=False, awaiting=False,
                    queued_count=0, submit_raises=None):
    coordinator = _Coordinator(
        active_turn=active_turn, active_runs=active_runs,
        queued_count=queued_count, submit_raises=submit_raises,
    )
    session_manager = _SessionManager(session=session, append_result=append_result)
    readiness = _Readiness(awaiting=awaiting)
    monkeypatch.setattr(sm, "session_manager", session_manager)
    monkeypatch.setattr(sm, "session_readiness", readiness)
    return coordinator, session_manager


# --- inject: validation ------------------------------------------------


def test_inject_requires_session_id(monkeypatch, fake_lifecycle):
    coordinator, _ = _patch_backends(monkeypatch, session={"orchestration_mode": "native"})
    with pytest.raises(ValueError, match="session_id is required"):
        asyncio.run(sm.inject(coordinator, "", prompt="hi"))


@pytest.mark.parametrize("prompt", ["", "   ", "\t\n"])
def test_inject_requires_prompt(monkeypatch, fake_lifecycle, prompt):
    coordinator, _ = _patch_backends(monkeypatch, session={"orchestration_mode": "native"})
    with pytest.raises(ValueError, match="prompt is required"):
        asyncio.run(sm.inject(coordinator, "sid", prompt=prompt))


def test_inject_session_not_found(monkeypatch, fake_lifecycle):
    coordinator, _ = _patch_backends(monkeypatch, session=None)
    with pytest.raises(ValueError, match="session not found"):
        asyncio.run(sm.inject(coordinator, "sid", prompt="hi"))


# --- inject: queueing disjuncts ----------------------------------------


def test_inject_immediate_send_when_idle(monkeypatch, fake_lifecycle):
    coordinator, ssm = _patch_backends(
        monkeypatch, session={"orchestration_mode": "native", "model": "m", "cwd": "/c"},
        active_turn=False, active_runs=False, awaiting=False, queued_count=0,
    )
    result = asyncio.run(sm.inject(coordinator, "sid", prompt="hi"))

    assert result == {
        "success": True, "session_id": "sid",
        "queued_id": result["queued_id"], "lifecycle_msg_id": "lifecycle-id-1",
        "queued": False,
    }
    # The queued prompt is recorded with the "send" lifecycle kind.
    assert ssm.added[0][1]["kind"] == "send"
    assert ssm.added[0][1]["queue_position"] == 0
    assert ssm.removed == []  # no rollback on success
    _sid, params = coordinator.submitted[0]
    assert params["prompt"] == "hi"
    assert params["cli_prompt"] == "hi"
    assert params["model"] == "m"
    assert params["cwd"] == "/c"
    assert params["orchestration_mode"] == "native"
    assert params["client_id"] is None
    assert fake_lifecycle[0]["kind"] == "send"


def test_inject_queued_when_active_turn(monkeypatch, fake_lifecycle):
    coordinator, ssm = _patch_backends(
        monkeypatch, session={"orchestration_mode": "native"}, active_turn=True,
        queued_count=3,
    )
    result = asyncio.run(sm.inject(coordinator, "sid", prompt="hi"))
    assert result["queued"] is True
    assert ssm.added[0][1]["kind"] == "queued_behind"
    assert ssm.added[0][1]["queue_position"] == 3
    assert fake_lifecycle[0]["kind"] == "queued_behind"


def test_inject_queued_when_active_runs(monkeypatch, fake_lifecycle):
    coordinator, ssm = _patch_backends(
        monkeypatch, session={"orchestration_mode": "native"}, active_runs=True,
    )
    assert asyncio.run(sm.inject(coordinator, "sid", prompt="hi"))["queued"] is True
    assert ssm.added[0][1]["kind"] == "queued_behind"


def test_inject_queued_when_awaiting_fork_provisioning(monkeypatch, fake_lifecycle):
    coordinator, ssm = _patch_backends(
        monkeypatch, session={"orchestration_mode": "native"}, awaiting=True,
    )
    assert asyncio.run(sm.inject(coordinator, "sid", prompt="hi"))["queued"] is True
    assert ssm.added[0][1]["kind"] == "queued_behind"


# --- inject: fallbacks + rollback --------------------------------------


def test_inject_uses_display_prompt_and_explicit_overrides(monkeypatch, fake_lifecycle):
    coordinator, ssm = _patch_backends(
        monkeypatch, session={"orchestration_mode": "team", "model": "sess-model", "cwd": "/sess"},
    )
    asyncio.run(sm.inject(
        coordinator, "sid", prompt="raw-cli",
        display_prompt="shown", model="explicit-model", cwd="/explicit",
        orchestration_mode="native", client_id="client-1",
    ))
    _sid, params = coordinator.submitted[0]
    # display_prompt wins for the visible prompt; cli_prompt keeps the raw input.
    assert params["prompt"] == "shown"
    assert params["cli_prompt"] == "raw-cli"
    # Explicit model / cwd / mode / client_id override the session defaults.
    assert params["model"] == "explicit-model"
    assert params["cwd"] == "/explicit"
    assert params["orchestration_mode"] == "native"
    assert params["client_id"] == "client-1"
    assert ssm.added[0][1]["content"] == "shown"


def test_inject_submit_failure_rolls_back_queued_prompt(monkeypatch, fake_lifecycle):
    coordinator, ssm = _patch_backends(
        monkeypatch, session={"orchestration_mode": "native"},
        submit_raises=RuntimeError("dispatch failed"),
    )
    with pytest.raises(RuntimeError, match="dispatch failed"):
        asyncio.run(sm.inject(coordinator, "sid", prompt="hi"))
    # The prompt was added then removed on the failed submit; no lifecycle emit.
    assert len(ssm.added) == 1
    assert len(ssm.removed) == 1
    assert ssm.removed[0] == ("sid", ssm.added[0][1]["id"])
    assert fake_lifecycle == []


# --- append_operator_message -------------------------------------------


def test_append_operator_message_requires_session_id(monkeypatch, fake_lifecycle):
    _patch_backends(monkeypatch, session={"orchestration_mode": "native"})
    with pytest.raises(ValueError, match="session_id is required"):
        asyncio.run(sm.append_operator_message("", content="hi"))


@pytest.mark.parametrize("content", ["", "   "])
def test_append_operator_message_requires_content(monkeypatch, fake_lifecycle, content):
    _patch_backends(monkeypatch, session={"orchestration_mode": "native"})
    with pytest.raises(ValueError, match="content is required"):
        asyncio.run(sm.append_operator_message("sid", content=content))


def test_append_operator_message_session_not_found_get_lite(monkeypatch, fake_lifecycle):
    _patch_backends(monkeypatch, session=None)
    with pytest.raises(ValueError, match="session not found"):
        asyncio.run(sm.append_operator_message("sid", content="hi"))


def test_append_operator_message_session_not_found_append_none(monkeypatch, fake_lifecycle):
    # get_lite finds the session, but append_assistant_msg returns None.
    _patch_backends(monkeypatch, session={"orchestration_mode": "native"}, append_result=None)
    with pytest.raises(ValueError, match="session not found"):
        asyncio.run(sm.append_operator_message("sid", content="hi"))


def test_append_operator_message_publishes_and_returns(monkeypatch, fake_lifecycle, fake_journal):
    _, ssm = _patch_backends(
        monkeypatch, session={"orchestration_mode": "native"}, append_result="ok",
    )
    result = asyncio.run(sm.append_operator_message("sid", content="hello-op", source="replay"))

    msg = result["message"]
    assert result["success"] is True
    assert result["session_id"] == "sid"
    assert msg["role"] == "operator"
    assert msg["source"] == "replay"
    assert msg["events"][0]["type"] == "agent_message"
    assert msg["events"][0]["data"]["message"]["content"] == [{"type": "text", "text": "hello-op"}]
    # The same message object was appended to the session and published to the journal.
    assert ssm.appended[0][1] is msg
    assert fake_journal[0]["event_type"] == "agent_message"
    assert fake_journal[0]["message_id"] == msg["id"]
    assert fake_journal[0]["data"] is msg["events"][0]["data"]


def test_append_assistant_message_is_alias():
    assert sm.append_assistant_message is sm.append_operator_message
