"""Integration test: agy/gemini-family runs must route through the
gemini-family parser during recovery based on the provider kind from
config_store, rather than relying on fragile file-presence sniffing or
stderr filenames.

Also verifies rate limit detection and graceful handling when session_events.jsonl
is missing due to an early crash.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-agy-recovery-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
from provider_claude import _runs_root  # noqa: E402
from run_recovery import _replay_and_apply, _should_retry_rate_limit  # noqa: E402
import paths  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    if not ok:
        failures.append(name)


def _seed_config_store() -> None:
    # Directly write configuration to config.json in the sandboxed home
    cfg_path = paths.ba_home() / "config.json"
    cfg = {
        "default_provider_id": "test-gemini-prov",
        "providers": [
            {
                "id": "test-gemini-prov",
                "name": "Test Gemini",
                "kind": "gemini",
                "model": "gemini-test"
            }
        ]
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")


def _seed_session_with_streaming_assistant() -> tuple[str, str, str]:
    sess = session_manager.create(
        name="test-session", model="gemini-test", cwd="/tmp", orchestration_mode="native",
        provider_id="test-gemini-prov"
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
        "model": "gemini-test",
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
        "provider_id": "test-gemini-prov"
    }))
    (run_dir / "pid").write_text("0")
    return run_id


def test_agy_recovery_with_events() -> None:
    _seed_config_store()
    app_sid, _, asst_id = _seed_session_with_streaming_assistant()
    raw_events = [
        {
            "type": "agent_message",
            "data": {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi from agy/gemini"}],
                    "model": "gemini-test"
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
                    "content": [{"type": "text", "text": "Hi from agy/gemini resource_exhausted in query"}],
                    "model": "gemini-test"
                },
                "uuid": "u1",
                "timestamp": "2026-06-24T12:00:01Z"
            }
        }
    ]

    run_id = _seed_agy_run(app_sid, asst_id, events_content=raw_events)
    run_dir = _runs_root() / run_id

    # 1. Test _replay_and_apply
    session = session_manager.get(app_sid)
    last_asst = session["messages"][-1]
    _replay_and_apply(
        persist_sid=app_sid,
        run_id=run_id,
        mode="native",
        claude_sid="test-agent-sess",
        sess=session,
        last_asst=last_asst,
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
    check("detects rate limit from Gemini events", _should_retry_rate_limit(run_dir) is True)


def test_agy_recovery_missing_events_file() -> None:
    _seed_config_store()
    app_sid, _, asst_id = _seed_session_with_streaming_assistant()

    # Seed a run without writing the session_events.jsonl file (represents an early crash)
    run_id = _seed_agy_run(app_sid, asst_id, write_events_file=False)
    run_dir = _runs_root() / run_id

    session = session_manager.get(app_sid)
    last_asst = session["messages"][-1]

    # Replay should execute without throwing FileNotFoundError or fallback to Claude jsonl parser
    try:
        _replay_and_apply(
            persist_sid=app_sid,
            run_id=run_id,
            mode="native",
            claude_sid="test-agent-sess",
            sess=session,
            last_asst=last_asst,
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
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    if failures:
        print(f"\nFAILED: {len(failures)} check(s)")
        sys.exit(1)
    print("\nAll integration checks passed")


if __name__ == "__main__":
    main()
