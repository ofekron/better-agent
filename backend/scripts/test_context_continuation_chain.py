from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-continuation-chain-")
import _test_installation
_test_installation.activate(Path(_TMP_HOME))

from continuation import PROVIDER_CAPABILITIES_CHANGED_ERROR  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import continuation_flow  # noqa: E402
from continuation_flow import start_continuation_for  # noqa: E402


def test_session_manager_persists_continuation_chain() -> None:
    sess = session_manager.create(name="continuation", cwd="/tmp", model="m")
    sid = sess["id"]
    updated = session_manager.set_continuation_chain(
        sid,
        ["old-provider-sid", "", " next-provider-sid "],
    )
    assert updated is not None
    fresh = session_manager.get(sid)
    assert fresh is not None
    assert fresh.get("continuation_chain") == [
        "old-provider-sid",
        "next-provider-sid",
    ]


def test_start_continuation_for_same_session() -> None:
    sess = session_manager.create(name="continuation-flow", cwd="/tmp", model="m")
    sid = sess["id"]
    native_path = Path(_TMP_HOME) / "native" / "old-provider-sid.jsonl"
    run_dir = Path(_TMP_HOME) / "runs" / "run-old-provider-sid"
    run_dir.mkdir(parents=True)
    (run_dir / "backend_state.json").write_text(
        json.dumps({
            "session_id": "old-provider-sid",
            "jsonl_path": str(native_path),
        }),
        encoding="utf-8",
    )
    started = start_continuation_for(
        session_manager=session_manager,
        app_session_id=sid,
        prompt="continue the work",
        old_provider_sid="old-provider-sid",
    )
    fresh = session_manager.get(sid)
    assert fresh is not None
    assert fresh.get("continuation_chain") == ["old-provider-sid"]
    assert started.continuation_chain == ["old-provider-sid"]
    assert started.chain_depth == 1
    assert "Better Agent session id: " + sid in started.prompt
    expected_session_path = Path(_TMP_HOME).resolve() / "sessions" / f"{sid}.json"
    assert f"Better Agent session file path: {expected_session_path}" in started.prompt
    assert "Previous provider session ids: old-provider-sid" in started.prompt
    assert f"- old-provider-sid: {native_path}" in started.prompt
    assert "fire_get_requirements" in started.prompt
    assert "get_requirements_results" in started.prompt
    assert "query_provider_native_transcript_index" not in started.prompt
    assert "native_element_fts.sid" not in started.prompt
    assert "agent_session_id" in started.prompt
    assert "supervisor_agent_session_id" in started.prompt
    assert "already native ids" in started.prompt
    assert started.prompt.endswith("continue the work")


def _append_message(sid: str, message: dict) -> None:
    if message["role"] == "user":
        session_manager.append_user_msg(sid, message, strict=True)
        return
    session_manager.append_assistant_msg(sid, message, strict=True)


def test_capability_restart_uses_exact_completed_prior_exchange() -> None:
    sess = session_manager.create(name="capability-referent", cwd="/tmp", model="m")
    sid = sess["id"]
    _append_message(sid, {
        "id": "prior-user",
        "role": "user",
        "content": "Why does Agent Board not manage MCP services?",
        "events": [],
    })
    _append_message(sid, {
        "id": "prior-assistant",
        "role": "assistant",
        "content": "stale snapshot",
        "_content_dirty": True,
        "completed_at": "2026-07-25T12:00:00Z",
        "events": [{
            "uuid": "answer",
            "type": "agent_message",
            "data": {
                "type": "assistant",
                "final_answer": True,
                "message": {
                    "content": [{
                        "type": "text",
                        "text": "Agent Board owns JSON state, not MCP services.",
                    }],
                },
            },
        }],
    })
    _append_message(sid, {
        "id": "current-user",
        "role": "user",
        "content": "why",
        "cli_prompt": "WRAPPED PROVIDER PROMPT",
        "events": [],
    })
    _append_message(sid, {
        "id": "target-assistant",
        "role": "assistant",
        "content": "",
        "events": [{"type": "tool_payload", "secret": "EXCLUDE-ME"}],
    })

    started = start_continuation_for(
        session_manager=session_manager,
        app_session_id=sid,
        prompt="STALE RUNTIME PROMPT",
        old_provider_sid="old-provider-sid",
        reason=PROVIDER_CAPABILITIES_CHANGED_ERROR,
        target_assistant_msg_id="target-assistant",
    )

    assert "Why does Agent Board not manage MCP services?" in started.prompt
    assert "Agent Board owns JSON state, not MCP services." in started.prompt
    assert started.prompt.endswith("why")
    assert "stale snapshot" not in started.prompt
    assert "WRAPPED PROVIDER PROMPT" not in started.prompt
    assert "STALE RUNTIME PROMPT" not in started.prompt
    assert "EXCLUDE-ME" not in started.prompt
    assert "restart metadata is not the user's topic" in started.prompt


def test_capability_restart_rejects_noncompleted_or_ambiguous_target() -> None:
    invalid_states = (
        {},
        {"completed_at": True},
        {"completed_at": "not-a-timestamp"},
        {"completed_at": "2026-07-25"},
        {"completed_at": "2026-07-25T12:00:00Z", "error": {}},
        {"completed_at": "2026-07-25T12:00:00Z", "stopped_at": 0},
    )
    for index, assistant_state in enumerate(invalid_states):
        sess = session_manager.create(
            name=f"capability-referent-reject-{index}",
            cwd="/tmp",
            model="m",
        )
        sid = sess["id"]
        for message in (
            {"id": "prior-user", "role": "user", "content": "topic", "events": []},
            {
                "id": "prior-assistant",
                "role": "assistant",
                "content": "unfinished answer",
                "events": [],
                **assistant_state,
            },
            {"id": "current-user", "role": "user", "content": "why", "events": []},
            {"id": "target-assistant", "role": "assistant", "content": "", "events": []},
        ):
            _append_message(sid, message)
        try:
            start_continuation_for(
                session_manager=session_manager,
                app_session_id=sid,
                prompt="must not be used",
                old_provider_sid="must-not-persist",
                reason=PROVIDER_CAPABILITIES_CHANGED_ERROR,
                target_assistant_msg_id="target-assistant",
            )
        except ValueError:
            pass
        else:
            raise AssertionError(
                f"capability restart accepted invalid state {assistant_state!r}"
            )
        fresh = session_manager.get(sid) or {}
        assert fresh.get("continuation_chain") in (None, [])

    valid = session_manager.create(
        name="capability-referent-missing-target",
        cwd="/tmp",
        model="m",
    )
    valid_sid = valid["id"]
    for message in (
        {"id": "prior-user", "role": "user", "content": "topic", "events": []},
        {
            "id": "prior-assistant",
            "role": "assistant",
            "content": "completed answer",
            "completed_at": "2026-07-25T12:00:00Z",
            "events": [],
        },
        {"id": "current-user", "role": "user", "content": "why", "events": []},
        {"id": "target-assistant", "role": "assistant", "content": "", "events": []},
    ):
        _append_message(valid_sid, message)
    try:
        start_continuation_for(
            session_manager=session_manager,
            app_session_id=valid_sid,
            prompt="must not be used",
            old_provider_sid="must-not-persist",
            reason=PROVIDER_CAPABILITIES_CHANGED_ERROR,
            target_assistant_msg_id="missing",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("capability restart accepted a missing target")
    fresh = session_manager.get(valid_sid) or {}
    assert fresh.get("continuation_chain") in (None, [])

    _append_message(valid_sid, {
        "id": "target-assistant",
        "role": "assistant",
        "content": "",
        "events": [],
    })
    try:
        start_continuation_for(
            session_manager=session_manager,
            app_session_id=valid_sid,
            prompt="must not be used",
            old_provider_sid="must-not-persist",
            reason=PROVIDER_CAPABILITIES_CHANGED_ERROR,
            target_assistant_msg_id="target-assistant",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("capability restart accepted a duplicate target")
    fresh = session_manager.get(valid_sid) or {}
    assert fresh.get("continuation_chain") in (None, [])


def test_capability_restart_context_is_bounded_and_reason_specific() -> None:
    sess = session_manager.create(name="capability-referent-bounded", cwd="/tmp", model="m")
    sid = sess["id"]
    for message in (
        {"id": "prior-user", "role": "user", "content": "u" * 10_000, "events": []},
        {
            "id": "prior-assistant",
            "role": "assistant",
            "content": "a" * 10_000,
            "completed_at": "2026-07-25T12:00:00Z",
            "events": [],
        },
        {"id": "current-user", "role": "user", "content": "current", "events": []},
        {"id": "target-assistant", "role": "assistant", "content": "", "events": []},
    ):
        _append_message(sid, message)

    capability = start_continuation_for(
        session_manager=session_manager,
        app_session_id=sid,
        prompt="stale",
        old_provider_sid=None,
        reason=PROVIDER_CAPABILITIES_CHANGED_ERROR,
        target_assistant_msg_id="target-assistant",
    )
    context_start = capability.prompt.index("Authoritative immediate conversation")
    context_end = capability.prompt.index("\n\nCurrent user message:")
    assert context_end - context_start <= 8_000
    assert capability.prompt.endswith("current")

    overflow = start_continuation_for(
        session_manager=session_manager,
        app_session_id=sid,
        prompt="ordinary overflow prompt",
        old_provider_sid=None,
    )
    assert overflow.prompt.endswith("ordinary overflow prompt")
    assert "Authoritative immediate conversation" not in overflow.prompt


def test_capability_restart_render_failure_does_not_persist_chain() -> None:
    sess = session_manager.create(
        name="capability-referent-render-failure",
        cwd="/tmp",
        model="m",
    )
    sid = sess["id"]
    for message in (
        {"id": "prior-user", "role": "user", "content": "topic", "events": []},
        {
            "id": "prior-assistant",
            "role": "assistant",
            "content": "completed answer",
            "completed_at": "2026-07-25T12:00:00Z",
            "events": [],
        },
        {"id": "current-user", "role": "user", "content": "why", "events": []},
        {"id": "target-assistant", "role": "assistant", "content": "", "events": []},
    ):
        _append_message(sid, message)

    original = continuation_flow.build_continuation_prompt

    def _fail_render(**_kwargs):
        raise RuntimeError("render failed")

    continuation_flow.build_continuation_prompt = _fail_render
    try:
        try:
            start_continuation_for(
                session_manager=session_manager,
                app_session_id=sid,
                prompt="stale",
                old_provider_sid="must-not-persist",
                reason=PROVIDER_CAPABILITIES_CHANGED_ERROR,
                target_assistant_msg_id="target-assistant",
            )
        except RuntimeError as error:
            assert str(error) == "render failed"
        else:
            raise AssertionError("capability restart ignored render failure")
    finally:
        continuation_flow.build_continuation_prompt = original
    fresh = session_manager.get(sid) or {}
    assert fresh.get("continuation_chain") in (None, [])


if __name__ == "__main__":
    try:
        test_session_manager_persists_continuation_chain()
        test_start_continuation_for_same_session()
        test_capability_restart_uses_exact_completed_prior_exchange()
        test_capability_restart_rejects_noncompleted_or_ambiguous_target()
        test_capability_restart_context_is_bounded_and_reason_specific()
        test_capability_restart_render_failure_does_not_persist_chain()
        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
