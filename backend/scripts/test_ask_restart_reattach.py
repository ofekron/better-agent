"""Phase 1: ask restart-resilience.

Proves the restart re-attach contract — a stable client-side `ask_id` lets a
retried `ask` call return the already-completed result instead of re-queueing
a duplicate prompt on the target — plus the durable completion signal
(terminal lifecycle event scan) that closes the crash-before-persist window.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ask-reattach-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ask_status_store
import paths
import user_msg_lifecycle
from orchestrator import Coordinator


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _write_route_status(
    ask_id: str,
    *,
    sender_session_id: str,
    target_session_id: str,
    **fields,
) -> None:
    ask_status_store.claim_route(
        ask_id,
        sender_session_id=sender_session_id,
        target_session_id=target_session_id,
    )
    ask_status_store.write_status(ask_id, **fields)


def test_ask_status_store_roundtrip():
    assert ask_status_store.read_status("ask_1") is None
    ask_status_store.write_status(
        "ask_1",
        lifecycle_msg_id="life-1",
        target_session_id="target-sess",
    )
    status = ask_status_store.read_status("ask_1")
    assert status["lifecycle_msg_id"] == "life-1"
    assert status["target_session_id"] == "target-sess"
    # merge, not overwrite
    ask_status_store.write_status("ask_1", result={"success": True})
    status = ask_status_store.read_status("ask_1")
    assert status["lifecycle_msg_id"] == "life-1"
    assert status["result"] == {"success": True, "ask_id": "ask_1"}
    ask_status_store.delete_status("ask_1")
    assert ask_status_store.read_status("ask_1") is None


def test_claim_route_backfills_unclaimed_legacy_shaped_record():
    """Regression: a record can exist under an ask_id without ever having
    gone through claim_route (`write_status()` called directly, seeding
    lifecycle_msg_id/target_session_id before route_kind/route_value ever
    existed — the shape every ask predating claim_route's introduction has,
    and the shape `_dispatch_prompt` still writes on first dispatch before
    claim_route runs a second time on reattach). `claim_route` must treat a
    missing `route_kind` as "not yet claimed" and backfill the route, not
    reject it as bound to a conflicting route."""
    ask_status_store.write_status(
        "ask_legacy1",
        lifecycle_msg_id="life-legacy1",
        queue_item_id="queue-legacy1",
        sender_session_id="sender-legacy",
        target_session_id="target-legacy",
    )
    claimed = ask_status_store.claim_route(
        "ask_legacy1",
        sender_session_id="sender-legacy",
        target_session_id="target-legacy",
    )
    assert claimed["route_kind"] == "session"
    assert claimed["route_value"] == "target-legacy"
    assert claimed["target_session_id"] == "target-legacy"
    assert claimed["lifecycle_msg_id"] == "life-legacy1"

    # A genuinely different sender must still be rejected as a real conflict.
    try:
        ask_status_store.claim_route(
            "ask_legacy1",
            sender_session_id="someone-else",
            target_session_id="target-legacy",
        )
        raise AssertionError("expected ValueError for a real sender conflict")
    except ValueError:
        pass
    ask_status_store.delete_status("ask_legacy1")


def test_ask_returns_cached_result_without_requeue():
    """Re-attach path: a stored result short-circuits before submit_prompt,
    so a backend-restart retry does NOT re-queue a duplicate target prompt."""
    cached = {
        "success": True,
        "target_session_id": "target-sess",
        "queued_id": "q-1",
        "response_message_id": "m-1",
        "assistant_content": "done already",
    }
    _write_route_status(
        "ask_restart1",
        sender_session_id="sender-sess",
        target_session_id="target-sess",
        result=cached,
    )

    coordinator = Coordinator()
    submit_calls: list[tuple] = []

    def _record_submit(app_session_id, params):
        submit_calls.append((app_session_id, params))
        return "q-x"

    coordinator.submit_prompt = _record_submit  # type: ignore[assignment]

    result = asyncio.run(
        coordinator.ask_team_message(
            sender_session_id="sender-sess",
            target_session_id="target-sess",
            message="hello",
            ask_id="ask_restart1",
        )
    )
    assert result == cached
    assert submit_calls == [], "reattach must not re-queue the target prompt"
    ask_status_store.delete_status("ask_restart1")


def test_terminal_event_for_lifecycle_scans_sub_session_under_root_dir():
    """Regression: a sub-session's events live under its ROOT's events.jsonl
    directory (sessions/<root_id>/events.jsonl), tagged with the sub-
    session's own sid — not under a directory named after the sub-session
    itself. `terminal_event_for_lifecycle` previously built the path from
    `app_session_id` instead of its resolved root_id, so restart-reattach
    could never find a completed sub-session's terminal event — exactly
    the shape every `create_sub_session` + `ask` target has."""
    from session_manager import manager as session_manager

    root = session_manager.create(name="root", cwd="/repo", orchestration_mode="native")
    sub = session_manager.create_sub_session(
        parent_session_id=root["id"], name="hidden reviewer", cwd="/repo",
    )
    assert sub["id"] != root["id"]
    root_id = session_manager._root_id_for(sub["id"])
    assert root_id == root["id"]

    events_path = paths.ba_home() / "sessions" / root_id / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps({
            "type": "user_message_done",
            "sid": sub["id"],
            "msg_id": "life-sub",
            "data": {"lifecycle_msg_id": "life-sub", "success": True},
        }) + "\n",
        encoding="utf-8",
    )

    terminal = user_msg_lifecycle.terminal_event_for_lifecycle(sub["id"], "life-sub")
    assert terminal is not None
    assert terminal["type"] == "user_message_done"


def test_terminal_event_for_lifecycle_scans_events_jsonl():
    """The durable completion signal used when the target turn finished during
    the restart window before the result was persisted."""
    from session_manager import manager as session_manager

    target = session_manager.create(name="t", cwd="/repo", orchestration_mode="native")
    root_id = session_manager._root_id_for(target["id"])
    assert root_id
    # Write under the RUNTIME ba_home() — when several test modules each set
    # BETTER_CLAUDE_HOME at import, pytest's single shared home may not equal
    # this module's _TMP_HOME, so resolve the actual home paths.ba_home() uses.
    events_path = paths.ba_home() / "sessions" / root_id / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)

    # No terminal event yet.
    events_path.write_text(
        json.dumps({"type": "user_message_sent", "data": {"lifecycle_msg_id": "life-x"}}) + "\n",
        encoding="utf-8",
    )
    assert user_msg_lifecycle.terminal_event_for_lifecycle(target["id"], "life-x") is None

    # A done event for a DIFFERENT lifecycle must not match.
    events_path.write_text(
        json.dumps({"type": "user_message_done", "data": {"lifecycle_msg_id": "other"}}) + "\n",
        encoding="utf-8",
    )
    assert user_msg_lifecycle.terminal_event_for_lifecycle(target["id"], "life-x") is None

    # Append the matching done event; scan finds it (even with older lines after).
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user_message_queued", "data": {"lifecycle_msg_id": "life-x"}}) + "\n")
        fh.write(json.dumps({"type": "user_message_done", "data": {"lifecycle_msg_id": "life-x"}}) + "\n")
        fh.write(json.dumps({"type": "agent_message", "data": {"uuid": "u1"}}) + "\n")
    terminal = user_msg_lifecycle.terminal_event_for_lifecycle(target["id"], "life-x")
    assert terminal is not None
    assert terminal["type"] == "user_message_done"

    # A failed event is also matched.
    events_path.write_text(
        json.dumps({"type": "user_message_failed", "data": {"lifecycle_msg_id": "life-x", "error": "boom"}}) + "\n",
        encoding="utf-8",
    )
    terminal = user_msg_lifecycle.terminal_event_for_lifecycle(target["id"], "life-x")
    assert terminal is not None
    assert terminal["type"] == "user_message_failed"


def test_terminal_event_for_lifecycle_async_runs_off_main_thread(monkeypatch):
    def fake_sync(app_session_id: str, lifecycle_msg_id: str):
        return {
            "app_session_id": app_session_id,
            "lifecycle_msg_id": lifecycle_msg_id,
            "thread_name": threading.current_thread().name,
        }

    monkeypatch.setattr(user_msg_lifecycle, "terminal_event_for_lifecycle", fake_sync)

    terminal = asyncio.run(
        user_msg_lifecycle.terminal_event_for_lifecycle_async("target", "life-async")
    )

    assert terminal["app_session_id"] == "target"
    assert terminal["lifecycle_msg_id"] == "life-async"
    assert terminal["thread_name"] != threading.main_thread().name


def test_reattach_uses_async_terminal_scan(monkeypatch):
    from session_manager import manager as session_manager

    sender = session_manager.create(name="sender async terminal", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target async terminal", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-async-terminal"
    _write_route_status(
        "ask_async_terminal",
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id="queued-async-terminal",
        sender_session_id=sender["id"],
        target_session_id=target["id"],
    )

    def sync_must_not_run(*_args, **_kwargs):
        raise AssertionError("async ask path must not scan events.jsonl on the event loop")

    async def fake_async(app_session_id: str, observed_lifecycle_msg_id: str):
        assert app_session_id == target["id"]
        assert observed_lifecycle_msg_id == lifecycle_msg_id
        return {"type": "user_message_done", "data": {"lifecycle_msg_id": lifecycle_msg_id}}

    coordinator = Coordinator()
    monkeypatch.setattr(user_msg_lifecycle, "terminal_event_for_lifecycle", sync_must_not_run)
    monkeypatch.setattr(user_msg_lifecycle, "terminal_event_for_lifecycle_async", fake_async)
    monkeypatch.setattr(
        coordinator,
        "submit_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reattach must not re-queue a duplicate prompt")
        ),
    )

    result = asyncio.run(coordinator.ask_team_message(
        sender_session_id=sender["id"],
        target_session_id=target["id"],
        message="question",
        ask_id="ask_async_terminal",
        timeout_s=0.01,
    ))

    assert result["success"] is True
    assert result["target_session_id"] == target["id"]
    assert result["queued_id"] == "queued-async-terminal"
    ask_status_store.delete_status("ask_async_terminal")


def test_reattach_artifact_lookup_runs_off_main_thread(monkeypatch):
    from session_manager import manager as session_manager

    target = session_manager.create(
        name="target async artifact", cwd="/repo", orchestration_mode="native",
    )
    lifecycle_msg_id = "life-async-artifact"
    session_manager.append_user_msg(target["id"], {
        "id": "user-async-artifact",
        "role": "user",
        "content": "question",
        "events": [],
        "timestamp": "2026-07-11T10:00:00",
        "lifecycle_msg_id": lifecycle_msg_id,
    })
    session_manager.append_assistant_msg(target["id"], {
        "id": "assistant-async-artifact",
        "role": "assistant",
        "content": "answer",
        "events": [],
        "timestamp": "2026-07-11T10:00:01",
    })
    observed_threads: list[str] = []
    coordinator = Coordinator()

    def fake_complete(**_kwargs):
        observed_threads.append(threading.current_thread().name)
        return {"success": True}

    monkeypatch.setattr(coordinator, "_team_message_complete_for_assistant", fake_complete)
    result = asyncio.run(coordinator._team_message_completed_result_from_store(
        target_session_id=target["id"],
        lifecycle_msg_id=lifecycle_msg_id,
    ))

    assert result["success"] is True
    assert observed_threads
    assert observed_threads[0] != threading.main_thread().name


def test_recovery_uses_async_terminal_scan(monkeypatch):
    from session_manager import manager as session_manager
    import run_recovery

    target = session_manager.create(name="recovery async terminal", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-recovery-async-terminal"
    session_manager.append_user_msg(target["id"], {
        "id": "user-recovery-async-terminal",
        "role": "user",
        "content": "question",
        "events": [],
        "timestamp": "2026-06-28T10:00:00",
        "lifecycle_msg_id": lifecycle_msg_id,
    })
    assistant_msg = session_manager.append_assistant_msg(target["id"], {
        "id": "assistant-recovery-async-terminal",
        "role": "assistant",
        "content": "answer",
        "events": [],
        "timestamp": "2026-06-28T10:00:01",
    })
    sess = session_manager.get(target["id"])
    captured: list[tuple] = []

    class _UPM:
        async def emit_user_msg_done(self, *args, **kwargs):
            captured.append(("done", args, kwargs))

        async def emit_user_msg_failed(self, *args, **kwargs):
            captured.append(("failed", args, kwargs))

    class _Coordinator:
        user_prompt_manager = _UPM()

    async def fake_async(app_session_id: str, observed_lifecycle_msg_id: str):
        assert app_session_id == target["id"]
        assert observed_lifecycle_msg_id == lifecycle_msg_id
        return {"type": "user_message_done", "data": {"lifecycle_msg_id": lifecycle_msg_id}}

    monkeypatch.setattr(user_msg_lifecycle, "terminal_event_for_lifecycle", lambda *_args: None)
    monkeypatch.setattr(user_msg_lifecycle, "terminal_event_for_lifecycle_async", fake_async)
    monkeypatch.setattr(run_recovery, "_salvage_complete_payload", lambda _run_id: None)

    asyncio.run(run_recovery._emit_recovered_user_message_terminal(
        coordinator=_Coordinator(),
        persist_sid=target["id"],
        mode="native",
        agent_sid=None,
        run_id="run-recovery-async-terminal",
        cancelled=False,
        sess=sess,
        assistant_msg=assistant_msg,
    ))

    assert captured == []

def test_reattach_returns_completed_target_message_without_terminal_event(monkeypatch):
    """Older recovered runs may have finalized the target assistant message
    before recovery emitted user_message_done/failed.  The ask retry must return
    that durable completed result instead of re-queueing or waiting forever."""
    from session_manager import manager as session_manager

    sender = session_manager.create(name="sender", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-recovered-success"
    _write_route_status(
        "ask_recovered_success",
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id="queued-recovered-success",
        sender_session_id=sender["id"],
        target_session_id=target["id"],
    )
    session_manager.append_user_msg(target["id"], {
        "id": "user-recovered-success",
        "role": "user",
        "content": "question",
        "events": [],
        "timestamp": "2026-06-28T10:00:00",
        "lifecycle_msg_id": lifecycle_msg_id,
    })
    session_manager.append_assistant_msg(target["id"], {
        "id": "assistant-recovered-success",
        "role": "assistant",
        "content": "durable recovered answer",
        "events": [],
        "timestamp": "2026-06-28T10:00:01",
        "completed_at": "2026-06-28T10:00:02",
    })

    coordinator = Coordinator()

    def fail_submit_prompt(*_args, **_kwargs):
        raise AssertionError("reattach must not re-queue a duplicate prompt")

    monkeypatch.setattr(coordinator, "submit_prompt", fail_submit_prompt)

    result = asyncio.run(coordinator.ask_team_message(
        sender_session_id=sender["id"],
        target_session_id=target["id"],
        message="question",
        ask_id="ask_recovered_success",
        timeout_s=0.01,
    ))

    assert result["success"] is True
    assert result["target_session_id"] == target["id"]
    assert result["queued_id"] == "queued-recovered-success"
    assert result["response_message_id"] == "assistant-recovered-success"
    assert result["assistant_content"] == "durable recovered answer"
    assert ask_status_store.read_status("ask_recovered_success")["result"] == result


def test_reattach_ignores_unfinished_target_message_without_terminal_event(monkeypatch):
    """A mere assistant placeholder is not a completion signal; without a
    terminal event or completed/error marker the call should still wait."""
    from session_manager import manager as session_manager

    sender = session_manager.create(name="sender unfinished", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target unfinished", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-recovered-unfinished"
    _write_route_status(
        "ask_recovered_unfinished",
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id="queued-recovered-unfinished",
        sender_session_id=sender["id"],
        target_session_id=target["id"],
    )
    session_manager.append_user_msg(target["id"], {
        "id": "user-recovered-unfinished",
        "role": "user",
        "content": "question",
        "events": [],
        "timestamp": "2026-06-28T10:00:00",
        "lifecycle_msg_id": lifecycle_msg_id,
    })
    session_manager.append_assistant_msg(target["id"], {
        "id": "assistant-recovered-unfinished",
        "role": "assistant",
        "content": "partial",
        "events": [],
        "timestamp": "2026-06-28T10:00:01",
    })

    coordinator = Coordinator()
    monkeypatch.setattr(
        coordinator,
        "submit_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reattach must not re-queue a duplicate prompt")
        ),
    )

    try:
        asyncio.run(coordinator.ask_team_message(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            message="question",
            ask_id="ask_recovered_unfinished",
            timeout_s=0.01,
        ))
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError("unfinished assistant must not be treated as success")

    assert "result" not in ask_status_store.read_status("ask_recovered_unfinished")

def test_recovery_emits_user_message_done_for_recovered_success(monkeypatch):
    from session_manager import manager as session_manager
    import run_recovery

    target = session_manager.create(name="recovered terminal", cwd="/repo", orchestration_mode="native")
    user_msg = session_manager.append_user_msg(target["id"], {
        "id": "user-recovery-terminal",
        "role": "user",
        "content": "question",
        "events": [],
        "timestamp": "2026-06-28T10:00:00",
        "lifecycle_msg_id": "life-recovery-terminal",
    })
    assistant_msg = session_manager.append_assistant_msg(target["id"], {
        "id": "assistant-recovery-terminal",
        "role": "assistant",
        "content": "answer",
        "events": [],
        "timestamp": "2026-06-28T10:00:01",
    })
    sess = session_manager.get(target["id"])
    captured: list[tuple] = []

    class _UPM:
        async def emit_user_msg_done(self, *args, **kwargs):
            captured.append(("done", args, kwargs))

        async def emit_user_msg_failed(self, *args, **kwargs):
            captured.append(("failed", args, kwargs))

    class _Coordinator:
        user_prompt_manager = _UPM()

    monkeypatch.setattr(
        run_recovery,
        "_salvage_complete_payload",
        lambda _run_id: {
            "success": True,
            "session_id": "agent-sid",
            "token_usage": {"input_tokens": 1},
        },
    )

    asyncio.run(run_recovery._emit_recovered_user_message_terminal(
        coordinator=_Coordinator(),
        persist_sid=target["id"],
        mode="native",
        agent_sid="agent-sid",
        run_id="run-recovered-success",
        cancelled=False,
        sess=sess,
        assistant_msg=assistant_msg,
    ))

    assert captured and captured[0][0] == "done"
    assert captured[0][1][0] == target["id"]
    assert captured[0][1][1] == "life-recovery-terminal"
    assert captured[0][1][2] == "native"
    assert captured[0][2].get("cancelled") is False


def test_recovery_emits_user_message_failed_when_complete_missing(monkeypatch):
    from session_manager import manager as session_manager
    import run_recovery

    target = session_manager.create(name="recovered failed terminal", cwd="/repo", orchestration_mode="native")
    session_manager.append_user_msg(target["id"], {
        "id": "user-recovery-failed",
        "role": "user",
        "content": "question",
        "events": [],
        "timestamp": "2026-06-28T10:00:00",
        "lifecycle_msg_id": "life-recovery-failed",
    })
    assistant_msg = session_manager.append_assistant_msg(target["id"], {
        "id": "assistant-recovery-failed",
        "role": "assistant",
        "content": "partial",
        "events": [],
        "timestamp": "2026-06-28T10:00:01",
    })
    sess = session_manager.get(target["id"])
    captured: list[tuple] = []

    class _UPM:
        async def emit_user_msg_done(self, *args, **kwargs):
            captured.append(("done", args, kwargs))

        async def emit_user_msg_failed(self, *args, **kwargs):
            captured.append(("failed", args, kwargs))

    class _Coordinator:
        user_prompt_manager = _UPM()

    monkeypatch.setattr(run_recovery, "_salvage_complete_payload", lambda _run_id: None)

    asyncio.run(run_recovery._emit_recovered_user_message_terminal(
        coordinator=_Coordinator(),
        persist_sid=target["id"],
        mode="native",
        agent_sid=None,
        run_id="run-recovered-missing-complete",
        cancelled=False,
        sess=sess,
        assistant_msg=assistant_msg,
    ))

    assert captured and captured[0][0] == "failed"
    assert captured[0][1][0] == target["id"]
    assert captured[0][1][1] == "life-recovery-failed"
    assert captured[0][2].get("reason") == "recovered_run_failed"


def test_reattach_redispatches_when_original_dispatch_never_landed():
    """Crash window: ask_status_store's correlation write (lifecycle_msg_id
    + queue_item_id + target_session_id) happens BEFORE the queue item is
    durably persisted to `queued_prompts`. If the backend crashes in
    between, a reattached ask must not wait forever for a turn that was
    never actually queued — it must detect the gap and redispatch."""
    from session_manager import manager as session_manager

    sender = session_manager.create(name="sender never-landed", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target never-landed", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-never-landed"
    queue_item_id = "queued-never-landed"
    # Simulate the crash landing exactly between the status write and the
    # durable queue write: status carries all three reattach fields, but
    # the target session's queued_prompts/messages are untouched.
    _write_route_status(
        "ask_never_landed",
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id=queue_item_id,
        sender_session_id=sender["id"],
        target_session_id=target["id"],
    )

    coordinator = Coordinator()
    submitted: list[tuple] = []

    def _record_submit(app_session_id, params, **_kwargs):
        submitted.append((app_session_id, params))
        return params.get("_queued_id") or "q-x"

    coordinator.submit_prompt = _record_submit  # type: ignore[assignment]

    try:
        asyncio.run(coordinator.ask_team_message(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            message="question",
            ask_id="ask_never_landed",
            timeout_s=0.01,
        ))
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError("nothing completes the turn in this test; must time out after redispatch")

    assert len(submitted) == 1, "the missing dispatch must be redone exactly once"
    app_session_id, params = submitted[0]
    assert app_session_id == target["id"]
    assert params["_queued_id"] == queue_item_id
    assert params["lifecycle_msg_id"] == lifecycle_msg_id

    queued = session_manager.get(target["id"]).get("queued_prompts") or []
    assert any(qp.get("id") == queue_item_id for qp in queued), (
        "redispatch must durably persist the queue item so a future "
        "restart's _re_enqueue_queued_prompts can recover it too"
    )
    ask_status_store.delete_status("ask_never_landed")


def test_reattach_does_not_redispatch_when_already_durably_queued(monkeypatch):
    """If the queue item DID land durably before the crash, a reattach must
    not redispatch — that would double-send the prompt to the target
    session. It must keep waiting for the WS terminal event instead."""
    from session_manager import manager as session_manager

    sender = session_manager.create(name="sender already-queued", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target already-queued", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-already-queued"
    queue_item_id = "queued-already-queued"
    _write_route_status(
        "ask_already_queued",
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id=queue_item_id,
        sender_session_id=sender["id"],
        target_session_id=target["id"],
    )
    session_manager.add_queued_prompt(target["id"], {
        "id": queue_item_id,
        "content": "question",
        "lifecycle_msg_id": lifecycle_msg_id,
        "source": "ask",
    })

    coordinator = Coordinator()
    monkeypatch.setattr(
        coordinator,
        "submit_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("already-queued reattach must not redispatch")
        ),
    )

    try:
        asyncio.run(coordinator.ask_team_message(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            message="question",
            ask_id="ask_already_queued",
            timeout_s=0.01,
        ))
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError("still-pending turn must time out, not resolve, in this test")
    ask_status_store.delete_status("ask_already_queued")


def test_reattach_does_not_redispatch_when_turn_already_started(monkeypatch):
    """If the target turn already started (a user message with this
    lifecycle_msg_id exists) before the crash, a reattach must not
    redispatch even though the queue item itself was already consumed
    (dequeued) by the time the turn started."""
    from session_manager import manager as session_manager

    sender = session_manager.create(name="sender already-started", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target already-started", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-already-started"
    queue_item_id = "queued-already-started"
    _write_route_status(
        "ask_already_started",
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id=queue_item_id,
        sender_session_id=sender["id"],
        target_session_id=target["id"],
    )
    session_manager.append_user_msg(target["id"], {
        "id": "user-already-started",
        "role": "user",
        "content": "question",
        "events": [],
        "timestamp": "2026-07-13T10:00:00",
        "lifecycle_msg_id": lifecycle_msg_id,
    })

    coordinator = Coordinator()
    monkeypatch.setattr(
        coordinator,
        "submit_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("already-started reattach must not redispatch")
        ),
    )

    try:
        asyncio.run(coordinator.ask_team_message(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            message="question",
            ask_id="ask_already_started",
            timeout_s=0.01,
        ))
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError("in-flight turn must time out, not resolve, in this test")
    ask_status_store.delete_status("ask_already_started")


def test_reattach_does_not_redispatch_while_genuinely_in_flight_same_process(monkeypatch):
    """Narrower race: `_run_session_processor` dequeues (removes the
    durable `queued_prompts` entry) BEFORE the turn's user message lands
    in `messages` — a real gap between those two durable checks. A
    reattach landing exactly in that gap must not redispatch if the turn
    is genuinely still running in this process. `user_prompt_manager`'s
    in-flight marker is set the instant a queue item is claimed and
    cleared only once the attempt fully concludes, so it exactly spans
    this window."""
    from session_manager import manager as session_manager

    sender = session_manager.create(name="sender in-flight", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target in-flight", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-in-flight"
    queue_item_id = "queued-in-flight"
    _write_route_status(
        "ask_in_flight",
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id=queue_item_id,
        sender_session_id=sender["id"],
        target_session_id=target["id"],
    )
    # Neither durable signal is present — target session has no
    # queued_prompts entry and no messages yet — simulating the exact
    # dequeue-to-append gap.
    coordinator = Coordinator()
    coordinator.user_prompt_manager.set_in_flight_lifecycle_msg_id(
        target["id"], lifecycle_msg_id,
    )
    monkeypatch.setattr(
        coordinator,
        "submit_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("genuinely in-flight reattach must not redispatch")
        ),
    )

    try:
        asyncio.run(coordinator.ask_team_message(
            sender_session_id=sender["id"],
            target_session_id=target["id"],
            message="question",
            ask_id="ask_in_flight",
            timeout_s=0.01,
        ))
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError("in-flight turn must time out, not resolve, in this test")
    ask_status_store.delete_status("ask_in_flight")
