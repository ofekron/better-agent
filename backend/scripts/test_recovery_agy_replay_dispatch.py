"""Integration test: session-events-family runs must route through the
session-events parser during recovery based on the provider kind from
config_store, rather than relying on fragile file-presence sniffing or
stderr filenames.

Also verifies rate limit detection and graceful handling when session_events.jsonl
is missing due to an early crash.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-agy-recovery-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
from provider_claude import _runs_root  # noqa: E402
from run_recovery import _replay_and_apply, _should_retry_rate_limit  # noqa: E402
import config_store  # noqa: E402
from scripts import _runtime_profile_test_helpers  # noqa: E402

def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    assert ok, name


def _seed_config_store() -> str:
    provider = config_store.add_provider({
        "name": "Test Agy",
        "kind": "agy",
        "mode": "subscription",
        "default_model": "agy-test",
        "custom_models": ["agy-test"],
    })
    _runtime_profile_test_helpers.activate_provider(provider["id"])
    return provider["id"]


def _seed_session_with_streaming_assistant(
    provider_id: str,
) -> tuple[str, str, str]:
    sess = session_manager.create(
        name="test-session", model="agy-test", cwd="/tmp", orchestration_mode="native",
        provider_id=provider_id,
    )
    sid = sess["id"]
    user_msg = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "do a thing",
        "events": [],
        "isStreaming": False,
    }
    asst_msg = {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": "",
        "events": [],
        "isStreaming": True,
    }
    session_manager.append_user_msg(sid, user_msg)
    session_manager.append_assistant_msg(sid, asst_msg)
    return sid, user_msg["id"], asst_msg["id"]


def _seed_agy_run(
    app_sid: str,
    target_msg_id: str,
    *,
    provider_id: str,
    events_content: list[dict] | None = None,
    write_events_file: bool = True,
) -> str:
    run_id = str(uuid.uuid4())
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if write_events_file:
        events_path = run_dir / "session_events.jsonl"
        with events_path.open("w", encoding="utf-8") as f:
            if events_content:
                for ev in events_content:
                    f.write(json.dumps(ev) + "\n")

    (run_dir / "input.json").write_text(json.dumps({
        "prompt": "do a thing",
        "cwd": "/tmp",
        "model": "agy-test",
        "session_id": "test-agent-sess",
        "mode": "native",
        "app_session_id": app_sid,
        "fork": False,
    }))

    (run_dir / "backend_state.json").write_text(json.dumps({
        "run_id": run_id,
        "app_session_id": app_sid,
        "persist_to": app_sid,
        "mode": "native",
        "runner_pid": 0,
        "started_at": "2026-06-24T12:00:00.000Z",
        "session_id": "test-agent-sess",
        "jsonl_path": str(run_dir / "session_events.jsonl"),
        "processed_line": 0,
        "cancelled": False,
        "target_message_id": target_msg_id,
        "provider_id": provider_id,
        "provider_kind": "agy",
    }))
    (run_dir / "pid").write_text("0")
    return run_id


def test_agy_recovery_with_events() -> None:
    provider_id = _seed_config_store()
    app_sid, _, asst_id = _seed_session_with_streaming_assistant(provider_id)
    raw_events = [
        {
            "type": "agent_message",
            "data": {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi from agy"}],
                    "model": "agy-test"
                },
                "uuid": "u1",
                "timestamp": "2026-06-24T12:00:00Z"
            }
        },
        {
            "type": "agent_message",
            "data": {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi from agy resource_exhausted in query"}],
                    "model": "agy-test"
                },
                "uuid": "u1",
                "timestamp": "2026-06-24T12:00:01Z"
            }
        }
    ]

    run_id = _seed_agy_run(
        app_sid,
        asst_id,
        provider_id=provider_id,
        events_content=raw_events,
    )
    run_dir = _runs_root() / run_id

    # 1. Test _replay_and_apply
    _replay_and_apply(
        persist_sid=app_sid,
        run_id=run_id,
        mode="native",
        claude_sid="test-agent-sess",
        msg_id=asst_id
    )

    updated_sess = session_manager.get(app_sid)
    updated_asst = updated_sess["messages"][-1]
    print("DEBUG content:", repr(updated_asst.get("content")))
    check("recovers correct number of events", len(updated_asst.get("events") or []) == 1)
    check("event contents recovered", "Hi from agy" in (updated_asst.get("content") or ""))

    # 2. Test _should_retry_rate_limit
    (run_dir / "complete.json").write_text(json.dumps({
        "success": False, "session_id": "test-agent-sess", "error": "RESOURCE_EXHAUSTED", "token_usage": None
    }))
    check("detects rate limit from session-events", _should_retry_rate_limit(run_dir) is True)


def test_agy_recovery_missing_events_file() -> None:
    provider_id = _seed_config_store()
    app_sid, _, asst_id = _seed_session_with_streaming_assistant(provider_id)

    # Seed a run without writing the session_events.jsonl file (represents an early crash)
    run_id = _seed_agy_run(
        app_sid,
        asst_id,
        provider_id=provider_id,
        write_events_file=False,
    )
    run_dir = _runs_root() / run_id

    # Replay should execute without throwing FileNotFoundError or fallback to Claude jsonl parser
    try:
        _replay_and_apply(
            persist_sid=app_sid,
            run_id=run_id,
            mode="native",
            claude_sid="test-agent-sess",
            msg_id=asst_id
        )
        passed = True
    except Exception as e:
        print(f"  Failed with exception: {e}")
        passed = False

    check("handles missing events file gracefully without crashing", passed)
    updated_sess = session_manager.get(app_sid)
    updated_asst = updated_sess["messages"][-1]
    check("no events recovered when file missing", len(updated_asst.get("events") or []) == 0)


def main() -> None:
    try:
        test_agy_recovery_with_events()
        test_agy_recovery_missing_events_file()
    finally:
        session_manager.flush_pending_persists()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    print("\nAll integration checks passed")


if __name__ == "__main__":
    main()
