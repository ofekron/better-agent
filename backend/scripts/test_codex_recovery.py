"""Regression tests for codex (native, one-shot) crash recovery.

1. (the fix) `CodexProvider.recover_in_flight` used to `continue` (skip)
   a still-alive detached runner, detaching it permanently until a later
   backend restart. It must now EMIT the run as `alive=True`,
   `recovered_as="live_orphan"` so `integrate_recovered_runs` re-hooks
   the live turn — see `test_live_orphan_is_emitted_not_skipped`.

2. (smoke) When that re-hooked live orphan completes, finalize replays
   Codex's native rollout JSONL through `_replay_and_apply`. This locks
   that the replay lands Codex events on the assistant message without
   using a Better-Claude-owned `session_events.jsonl`.

Run with:
    cd backend && .venv/bin/python scripts/test_codex_recovery.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import uuid
from types import SimpleNamespace
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-codex-recover-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
from runs_dir import runs_root  # noqa: E402
from provider import schedule_loop_task  # noqa: E402
from provider_codex import CodexProvider, RunState, read_codex_run_rollout_events  # noqa: E402
from codex_usage import token_usage_from_codex_usage  # noqa: E402
from event_shape import extract_output_text as _extract_output_text  # noqa: E402
from runner_codex import _normalize_mcp_tool_completed, _post_loopback_sync  # noqa: E402
import turn_manager as turn_manager_mod  # noqa: E402
from turn_manager import TurnManager, _missing_event_dicts  # noqa: E402
from run_recovery import (  # noqa: E402
    _integrate_one,
    _last_assistant,
    _replay_and_apply,
    _replay_from_codex_rollout,
)


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _make_assistant_text_event(text: str) -> dict:
    """One native Codex rollout line that normalizes to assistant text."""
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "content": [{"type": "text", "text": text}],
            "role": "assistant",
        },
    }


def _make_turn_completed_event(usage: dict | None = None) -> dict:
    return {
        "type": "turn.completed",
        "usage": usage if usage is not None else {
            "input_tokens": 10,
            "output_tokens": 7,
            "cached_input_tokens": 3,
        },
    }


def _make_turn_failed_event(message: str = "turn failed hard") -> dict:
    return {
        "type": "turn.failed",
        "error": {"message": message},
    }


def _seed_codex_run(
    *,
    app_sid: str,
    codex_sid: str,
    pid: int,
    events: list[dict],
    complete: bool,
    target_message_id: str | None = None,
    write_jsonl_path: bool = True,
    run_id: str | None = None,
) -> str:
    """Synthesize a codex run dir: native rollout jsonl + codex_stderr.log
    (NOT gemini_stderr.log) + state/backend_state. `pid` is stamped as
    runner_pid; `complete` controls whether complete.json exists."""
    run_id = run_id or str(uuid.uuid4())
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    events_path = run_dir / "codex-rollout.jsonl"
    with events_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "old turn"}],
            },
        }) + "\n")
        start_byte = f.tell()
        for e in events:
            f.write(json.dumps(e) + "\n")
    (run_dir / "codex_stderr.log").write_text("", encoding="utf-8")

    state = {
        "run_id": run_id,
        "mode": "native",
        "runner_pid": pid,
        "app_session_id": app_sid,
        "session_id": codex_sid,
        "pre_query_byte_offset": start_byte,
        "complete": complete,
    }
    if write_jsonl_path:
        state["jsonl_path"] = str(events_path)
        state["rollout_path"] = str(events_path)
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    backend_state = {
        "run_id": run_id,
        "app_session_id": app_sid,
        "mode": "native",
        "runner_pid": pid,
        "session_id": codex_sid,
        "processed_line": 0,
        "processed_byte_offset": events_path.stat().st_size if complete else start_byte,
        "cancelled": False,
        "provider_id": "codex-test",
        "target_message_id": target_message_id,
    }
    if write_jsonl_path:
        backend_state["jsonl_path"] = str(events_path)
    (run_dir / "backend_state.json").write_text(json.dumps(backend_state), encoding="utf-8")
    (run_dir / "pid").write_text(str(pid))
    if complete:
        (run_dir / "complete.json").write_text(json.dumps({
            "success": True, "session_id": codex_sid, "error": None,
            "token_usage": None,
        }), encoding="utf-8")
    return run_id


def _seed_session_with_streaming_assistant() -> tuple[str, str]:
    sess = session_manager.create(
        name="t", model="gpt-5.5", cwd="/tmp", orchestration_mode="native",
    )
    sid = sess["id"]
    session_manager.append_user_msg(sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "do a thing",
        "events": [], "isStreaming": False,
    })
    asst_id = str(uuid.uuid4())
    session_manager.append_assistant_msg(sid, {
        "id": asst_id, "role": "assistant", "content": "",
        "events": [], "isStreaming": True,
    })
    return sid, asst_id


def test_live_orphan_is_emitted_not_skipped() -> bool:
    """Bug 1: a still-alive codex runner must surface from
    recover_in_flight as alive/live_orphan (previously `continue`d)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    try:
        codex_sid = str(uuid.uuid4())
        _seed_codex_run(
            app_sid="sess-x", codex_sid=codex_sid, pid=proc.pid,
            events=[_make_assistant_text_event("partial")], complete=False,
        )
        recovered = CodexProvider({"id": "codex-test"}).recover_in_flight()
        if len(recovered) != 1:
            print(f"  expected 1 descriptor, got {len(recovered)}")
            return False
        desc = recovered[0]
        if desc.get("alive") is not True:
            print(f"  expected alive=True, got {desc.get('alive')!r}")
            return False
        if desc.get("recovered_as") != "live_orphan":
            print(f"  expected recovered_as=live_orphan, got {desc.get('recovered_as')!r}")
            return False
        if desc.get("has_complete_json") is not False:
            print(f"  expected has_complete_json=False, got {desc.get('has_complete_json')!r}")
            return False
        if str(desc.get("jsonl_path") or "").endswith("session_events.jsonl"):
            print(f"  codex recovery must not use session_events.jsonl: {desc.get('jsonl_path')!r}")
            return False
        if not isinstance(desc.get("processed_byte_offset"), int):
            print(f"  missing processed_byte_offset: {desc!r}")
            return False
        # A live orphan must NOT get a synthesized complete.json.
        if (runs_root() / desc["run_id"] / "complete.json").exists():
            print("  live orphan wrongly got a synthesized complete.json")
            return False
        return True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_codex_replay_reads_native_rollout_jsonl() -> bool:
    """Smoke: _replay_and_apply lands a codex run's native rollout events
    on the assistant message without reading session_events.jsonl."""
    app_sid, asst_id = _seed_session_with_streaming_assistant()
    codex_sid = str(uuid.uuid4())
    events = [
        _make_assistant_text_event("Hello"),
        _make_assistant_text_event("world"),
    ]
    run_id = _seed_codex_run(
        app_sid=app_sid, codex_sid=codex_sid, pid=0, events=events, complete=True,
    )

    sess = session_manager.get(app_sid)
    last_asst = _last_assistant(sess)
    _replay_and_apply(
        persist_sid=app_sid,
        run_id=run_id,
        mode="native",
        claude_sid=codex_sid,
        sess=sess,
        last_asst=last_asst,
        msg_id=last_asst["id"],
    )

    sess = session_manager.get(app_sid)
    asst = next((m for m in sess["messages"] if m["id"] == asst_id), None)
    if asst is None:
        print("  assistant message disappeared")
        return False
    evs = asst.get("events") or []
    if len(evs) != len(events):
        print(f"  expected {len(events)} events, got {len(evs)}")
        return False
    for e in evs:
        if e.get("type") != "agent_message":
            print(f"  expected agent_message envelope, got {e.get('type')!r}")
            return False
    content = asst.get("content") or ""
    if "old turn" in content:
        print(f"  replay ignored byte offset and included old turn: {content!r}")
        return False
    # Both events applied. Content reflects the last replayed assistant
    # message — separate complete messages replace rather than concatenate.
    if "world" not in content:
        print(f"  expected replayed text in content, got {content!r}")
        return False
    return True


def test_live_recovery_streams_rollout_events_before_complete() -> bool:
    class _TurnManager:
        def __init__(self) -> None:
            self.active_run_ids = {}

        def run_state_add(self, app_sid, *, run_id, kind, target_message_id, pid):
            self.active_run_ids.setdefault(app_sid, []).append(run_id)

        def run_state_remove(self, app_sid, run_id):
            if run_id in self.active_run_ids.get(app_sid, []):
                self.active_run_ids[app_sid].remove(run_id)

        async def emit_run_state(self, app_sid):
            return None

    class _Coordinator:
        def __init__(self) -> None:
            self.turn_manager = _TurnManager()

    async def _run() -> bool:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
        )
        try:
            app_sid, asst_id = _seed_session_with_streaming_assistant()
            codex_sid = str(uuid.uuid4())
            run_id = _seed_codex_run(
                app_sid=app_sid,
                codex_sid=codex_sid,
                pid=proc.pid,
                events=[_make_assistant_text_event("live after restart")],
                complete=False,
                target_message_id=asst_id,
            )
            provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})
            desc = next(
                item for item in provider.recover_in_flight()
                if item.get("run_id") == run_id
            )
            before_offset = desc.get("processed_byte_offset")
            await _integrate_one(_Coordinator(), provider, desc)

            deadline = asyncio.get_running_loop().time() + 3.0
            rendered = ""
            while asyncio.get_running_loop().time() < deadline:
                sess = session_manager.get(app_sid) or {}
                asst = next((m for m in sess.get("messages", []) if m.get("id") == asst_id), {})
                rendered = json.dumps(asst.get("events") or [])
                if "live after restart" in rendered:
                    break
                await asyncio.sleep(0.05)
            if "live after restart" not in rendered:
                print(f"  recovered live event was not rendered before complete: {rendered!r}")
                return False

            run_dir = runs_root() / run_id
            backend_state = json.loads((run_dir / "backend_state.json").read_text(encoding="utf-8"))
            if backend_state.get("processed_byte_offset", 0) <= before_offset:
                print(f"  cursor did not advance: before={before_offset} state={backend_state!r}")
                return False
            if "processed_byte" in backend_state:
                print(f"  codex state wrote wrong cursor key: {backend_state!r}")
                return False

            (run_dir / "complete.json").write_text(json.dumps({
                "success": True,
                "session_id": codex_sid,
                "error": None,
                "token_usage": None,
            }), encoding="utf-8")
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                if run_id not in provider._runs:
                    break
                await asyncio.sleep(0.05)
            if run_id in provider._runs:
                print("  recovered live run did not clean up after complete")
                return False
            return True
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    return asyncio.run(_run())


def test_live_recovery_waits_for_child_setup_before_complete() -> bool:
    class _TurnManager:
        def __init__(self) -> None:
            self.active_run_ids = {}

        def run_state_add(self, app_sid, *, run_id, kind, target_message_id, pid):
            self.active_run_ids.setdefault(app_sid, []).append(run_id)

        def run_state_remove(self, app_sid, run_id):
            if run_id in self.active_run_ids.get(app_sid, []):
                self.active_run_ids[app_sid].remove(run_id)

        async def emit_run_state(self, app_sid):
            return None

    class _Coordinator:
        def __init__(self) -> None:
            self.turn_manager = _TurnManager()

    async def _run() -> bool:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
        )
        try:
            app_sid, asst_id = _seed_session_with_streaming_assistant()
            codex_sid = str(uuid.uuid4())
            run_id = _seed_codex_run(
                app_sid=app_sid,
                codex_sid=codex_sid,
                pid=proc.pid,
                events=[_make_assistant_text_event("parent live")],
                complete=False,
                target_message_id=asst_id,
            )
            run_dir = runs_root() / run_id
            child_sid = str(uuid.uuid4())
            child_path = run_dir / "child-rollout.jsonl"
            with child_path.open("wb") as f:
                f.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "child prompt"},
                }).encode() + b"\n")
                child_start = f.tell()
                f.write(json.dumps(_make_assistant_text_event("child live after restart")).encode() + b"\n")
            backend_state_path = run_dir / "backend_state.json"
            backend_state = json.loads(backend_state_path.read_text(encoding="utf-8"))
            source_key = f"call_agent_{child_sid}"
            backend_state["child_sources"] = {
                source_key: {
                    "agent_id": child_sid,
                    "source_key": source_key,
                    "parent_tool_use_id": "call_agent",
                    "jsonl_path": str(child_path),
                    "start_byte": child_start,
                    "processed_byte_offset": child_start,
                    "delegation_id": f"codex_subagent_{source_key}",
                }
            }
            backend_state_path.write_text(json.dumps(backend_state), encoding="utf-8")

            provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})
            original_ensure_child = provider._ensure_child_tailer

            async def delayed_ensure_child(*args, **kwargs):
                await asyncio.sleep(0.35)
                return await original_ensure_child(*args, **kwargs)

            provider._ensure_child_tailer = delayed_ensure_child
            desc = next(
                item for item in provider.recover_in_flight()
                if item.get("run_id") == run_id
            )
            await _integrate_one(_Coordinator(), provider, desc)
            (run_dir / "complete.json").write_text(json.dumps({
                "success": True,
                "session_id": codex_sid,
                "error": None,
                "token_usage": None,
            }), encoding="utf-8")

            deadline = asyncio.get_running_loop().time() + 4.0
            child_offset = child_start
            while asyncio.get_running_loop().time() < deadline:
                state = json.loads(backend_state_path.read_text(encoding="utf-8"))
                child_offset = (
                    state.get("child_sources", {})
                    .get(source_key, {})
                    .get("processed_byte_offset", child_start)
                )
                if child_offset > child_start and run_id not in provider._runs:
                    break
                await asyncio.sleep(0.05)
            if child_offset <= child_start:
                print(f"  child cursor did not advance before cleanup: {child_offset} <= {child_start}")
                return False
            if run_id in provider._runs:
                print("  recovered live run did not clean up after child setup")
                return False
            return True
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    return asyncio.run(_run())


def test_dead_wrapper_uses_rollout_terminal_complete() -> bool:
    app_sid, asst_id = _seed_session_with_streaming_assistant()
    codex_sid = str(uuid.uuid4())
    run_id = _seed_codex_run(
        app_sid=app_sid,
        codex_sid=codex_sid,
        pid=0,
        events=[
            _make_assistant_text_event("completed before wrapper wrote file"),
            _make_turn_completed_event(),
        ],
        complete=False,
        target_message_id=asst_id,
    )

    recovered = CodexProvider({"id": "codex-test"}).recover_in_flight()
    desc = next((item for item in recovered if item.get("run_id") == run_id), None)
    if desc is None:
        print("  recovered descriptor missing")
        return False
    if desc.get("recovered_as") != "completed_from_rollout":
        print(f"  expected completed_from_rollout, got {desc.get('recovered_as')!r}")
        return False
    complete = json.loads((runs_root() / run_id / "complete.json").read_text(encoding="utf-8"))
    if complete.get("success") is not True:
        print(f"  expected success=True, got {complete!r}")
        return False
    usage = complete.get("token_usage") or {}
    if usage.get("total_tokens") != 17 or usage.get("cache_read_input_tokens") != 3:
        print(f"  unexpected token usage: {usage!r}")
        return False
    return True


def test_dead_wrapper_resolves_missing_jsonl_path() -> bool:
    codex_sid = str(uuid.uuid4())
    run_id = _seed_codex_run(
        app_sid="sess-resolve",
        codex_sid=codex_sid,
        pid=0,
        events=[_make_turn_completed_event()],
        complete=False,
        write_jsonl_path=False,
    )
    rollout_path = runs_root() / run_id / "codex-rollout.jsonl"

    import codex_native

    original_resolve = codex_native.resolve_rollout_path
    try:
        codex_native.resolve_rollout_path = lambda sid: rollout_path if sid == codex_sid else None
        CodexProvider({"id": "codex-test"}).recover_in_flight()
    finally:
        codex_native.resolve_rollout_path = original_resolve

    complete = json.loads((runs_root() / run_id / "complete.json").read_text(encoding="utf-8"))
    if complete.get("success") is not True:
        print(f"  expected success=True, got {complete!r}")
        return False
    return True


def test_dead_wrapper_ignores_malformed_usage_values() -> bool:
    codex_sid = str(uuid.uuid4())
    run_id = _seed_codex_run(
        app_sid="sess-malformed-usage",
        codex_sid=codex_sid,
        pid=0,
        events=[_make_turn_completed_event({
            "input_tokens": True,
            "output_tokens": -5,
            "cached_input_tokens": "9",
        })],
        complete=False,
    )

    CodexProvider({"id": "codex-test"}).recover_in_flight()
    complete = json.loads((runs_root() / run_id / "complete.json").read_text(encoding="utf-8"))
    usage = complete.get("token_usage") or {}
    expected = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": 0,
    }
    if usage != expected:
        print(f"  expected malformed usage to zero, got {usage!r}")
        return False
    return True


def test_codex_usage_normalizer_zeros_malformed_live_values() -> bool:
    usage = token_usage_from_codex_usage({
        "input_tokens": True,
        "output_tokens": -5,
        "cached_input_tokens": "9",
    })
    expected = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": 0,
    }
    if usage != expected:
        print(f"  expected malformed live usage to zero, got {usage!r}")
        return False
    return True


def test_dead_wrapper_uses_rollout_terminal_failure() -> bool:
    codex_sid = str(uuid.uuid4())
    run_id = _seed_codex_run(
        app_sid="sess-failed",
        codex_sid=codex_sid,
        pid=0,
        events=[_make_turn_failed_event("model context window exceeded")],
        complete=False,
        write_jsonl_path=False,
    )
    rollout_path = runs_root() / run_id / "codex-rollout.jsonl"

    import codex_native

    original_resolve = codex_native.resolve_rollout_path
    try:
        codex_native.resolve_rollout_path = lambda sid: rollout_path if sid == codex_sid else None
        CodexProvider({"id": "codex-test"}).recover_in_flight()
    finally:
        codex_native.resolve_rollout_path = original_resolve

    complete = json.loads((runs_root() / run_id / "complete.json").read_text(encoding="utf-8"))
    if complete.get("success") is not False:
        print(f"  expected success=False, got {complete!r}")
        return False
    if not complete.get("error"):
        print(f"  expected preserved failure error, got {complete!r}")
        return False
    if complete.get("error") == "runner died before completion (recovered at startup)":
        print(f"  terminal turn.failed was ignored: {complete!r}")
        return False
    return True


def test_dead_wrapper_without_terminal_still_fails_closed() -> bool:
    codex_sid = str(uuid.uuid4())
    run_id = _seed_codex_run(
        app_sid="sess-no-terminal",
        codex_sid=codex_sid,
        pid=0,
        events=[_make_assistant_text_event("partial")],
        complete=False,
    )

    CodexProvider({"id": "codex-test"}).recover_in_flight()
    complete = json.loads((runs_root() / run_id / "complete.json").read_text(encoding="utf-8"))
    if complete.get("success") is not False:
        print(f"  expected success=False, got {complete!r}")
        return False
    if complete.get("error") != "runner died before completion (recovered at startup)":
        print(f"  unexpected error: {complete!r}")
        return False
    return True


def test_emit_complete_recovers_missing_complete_from_rollout() -> bool:
    codex_sid = str(uuid.uuid4())
    run_id = _seed_codex_run(
        app_sid="sess-emit",
        codex_sid=codex_sid,
        pid=0,
        events=[_make_turn_completed_event()],
        complete=False,
    )

    async def _run() -> bool:
        queue: asyncio.Queue = asyncio.Queue()
        provider = CodexProvider({"id": "codex-test"})
        rs = SimpleNamespace(
            run_id=run_id,
            run_dir=runs_root() / run_id,
            session_id=codex_sid,
            queue=queue,
            tailer=None,
        )
        await provider._emit_complete_from_file(rs, runs_root() / run_id / "complete.json")
        event = queue.get_nowait()
        payload = event.data
        if payload.get("success") is not True:
            print(f"  expected success=True, got {payload!r}")
            return False
        return True

    return asyncio.run(_run())


def test_loopback_post_retries_transient_reset() -> bool:
    import runner_codex

    calls = 0

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"ok": true}'

    original_urlopen = runner_codex.urllib.request.urlopen
    original_sleep = runner_codex.time.sleep

    def fake_urlopen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.URLError(ConnectionResetError(54, "reset"))
        return _Resp()

    try:
        runner_codex.urllib.request.urlopen = fake_urlopen
        runner_codex.time.sleep = lambda *_args, **_kwargs: None
        res = _post_loopback_sync(
            {"x": 1},
            backend_url="http://127.0.0.1:8000",
            internal_token="token",
            url_path="/api/internal/ask",
            timeout_s=10,
        )
    finally:
        runner_codex.urllib.request.urlopen = original_urlopen
        runner_codex.time.sleep = original_sleep

    if res != {"ok": True}:
        print(f"  expected ok response, got {res!r}")
        return False
    if calls != 2:
        print(f"  expected retry once, got {calls} calls")
        return False
    return True


def test_schedule_loop_task_from_worker_thread() -> bool:
    async def main() -> bool:
        loop = asyncio.get_running_loop()
        done = threading.Event()

        async def marker() -> None:
            done.set()

        await asyncio.to_thread(
            schedule_loop_task,
            loop,
            marker(),
            name="test-schedule-loop-task-worker",
        )
        if not done.wait(timeout=2.0):
            print("  scheduled coro never ran on the loop")
            return False
        return True

    return asyncio.run(main())


def test_schedule_loop_task_no_block_under_loop_lag() -> bool:
    """Regression: scheduling the bootstrap coro from a worker thread must
    NOT synchronously wait for the event loop. The old create_loop_task did
    future.result(timeout=5) and raised TimeoutError — killing the whole
    turn — whenever the loop could not service a call_soon within 5s. With
    the loop deliberately held, scheduling must still return immediately.
    """
    loop = asyncio.new_event_loop()
    hold = threading.Event()      # freed by the test to release the loop
    holding = threading.Event()   # set once the loop has entered the hold
    scheduled = threading.Event()  # set by the scheduled coro when it runs

    def occupy_loop() -> None:
        holding.set()
        hold.wait(10.0)

    def run_loop() -> None:
        loop.call_soon(occupy_loop)
        loop.run_forever()

    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()

    async def marker() -> None:
        scheduled.set()

    result: dict = {}

    def schedule_from_worker() -> None:
        # Hard happens-before: confirm the loop is actually held before
        # scheduling, closing the call_soon vs call_soon_threadsafe race.
        if not holding.wait(timeout=2.0):
            result["error"] = "loop never entered hold"
            return
        start = time.monotonic()
        try:
            schedule_loop_task(
                loop, marker(), name="test-schedule-under-lag",
            )
        except BaseException as exc:  # noqa: BLE001 — surface pre-fix TimeoutError
            result["error"] = repr(exc)
            return
        result["elapsed"] = time.monotonic() - start

    worker = threading.Thread(target=schedule_from_worker)
    worker.start()
    worker.join(timeout=3.0)
    try:
        if worker.is_alive():
            print("  scheduling worker blocked on the held loop (pre-fix hang)")
            return False
        if "error" in result:
            print(f"  scheduling raised (pre-fix behavior): {result['error']}")
            return False
        elapsed = result.get("elapsed", 99.0)
        if elapsed >= 1.0:
            print(f"  scheduling blocked worker {elapsed:.3f}s; expected immediate return")
            return False
        hold.set()
        if not scheduled.wait(timeout=2.0):
            print("  scheduled coro never ran after loop released")
            return False
        return True
    finally:
        hold.set()  # release occupy_loop so the loop can service stop
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2.0)
        loop.close()


def test_codex_mcp_string_error_normalizes() -> bool:
    event = _normalize_mcp_tool_completed(
        {"id": "tool-1", "error": "connection reset"},
        "parent-1",
    )
    content = event.get("message", {}).get("content", [])
    text = ((content[0] or {}).get("content") if content else "")
    if text != "Error: connection reset":
        print(f"  expected string error content, got {text!r}")
        return False
    return True


def test_codex_dead_runner_replay_preserves_tool_result_structure() -> bool:
    app_sid, asst_id = _seed_session_with_streaming_assistant()
    codex_sid = str(uuid.uuid4())
    run_id = _seed_codex_run(
        app_sid=app_sid,
        codex_sid=codex_sid,
        pid=0,
        events=[
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call_shell",
                    "arguments": "{\"cmd\":\"printf secret-tool-output\"}",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_shell",
                    "output": "secret-tool-output",
                },
            },
            _make_assistant_text_event("final answer only"),
        ],
        complete=True,
        target_message_id=asst_id,
    )

    events = read_codex_run_rollout_events(runs_root() / run_id)
    if not any(
        block.get("type") == "tool_result"
        for event in events
        for block in ((event.get("data") or {}).get("message") or {}).get("content", [])
        if isinstance(block, dict)
    ):
        print("  replay did not preserve tool_result blocks")
        return False
    output = _extract_output_text(events)
    if "secret-tool-output" in output:
        print(f"  tool result leaked into assistant output text: {output!r}")
        return False
    if "final answer only" not in output:
        print(f"  assistant text missing from replay output: {output!r}")
        return False
    return True


def test_codex_replay_dedup_allows_mutated_same_uuid() -> bool:
    partial = {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "uuid": "same-uuid",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "partial"}]},
        },
    }
    exact_duplicate = json.loads(json.dumps(partial))
    updated = {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "uuid": "same-uuid",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "final"}]},
        },
    }

    missing = _missing_event_dicts([partial], [exact_duplicate, updated])
    if missing != [updated]:
        print(f"  expected only mutated same-uuid update, got {missing!r}")
        return False
    return True


def test_turn_manager_dead_runner_replays_codex_rollout_events() -> bool:
    class _UserPromptManager:
        def get_in_flight_lifecycle_msg_id(self, _sid):
            return None

    class _FakeCodexProvider:
        KIND = "codex"
        id = "codex-test"

        def __init__(self, app_sid: str, codex_sid: str) -> None:
            self._runs = {}
            self.app_sid = app_sid
            self.codex_sid = codex_sid

        def start_run(self, **kwargs) -> None:
            _seed_codex_run(
                app_sid=self.app_sid,
                codex_sid=self.codex_sid,
                pid=0,
                events=[
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "call_shell",
                            "arguments": "{\"cmd\":\"printf hidden-tool-output\"}",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call_shell",
                            "output": "hidden-tool-output",
                        },
                    },
                    _make_assistant_text_event("visible final answer"),
                    _make_turn_completed_event(),
                ],
                complete=True,
                run_id=kwargs["run_id"],
            )

        def is_running(self, _run_id: str) -> bool:
            return False

    class _Coordinator:
        def __init__(self, provider) -> None:
            self.internal_token = "token"
            self.user_prompt_manager = _UserPromptManager()
            self._session_cancelled = {}
            self._provider = provider

        def provider_for_run(self, *_args, **_kwargs):
            return self._provider

        def provider_for_session(self, *_args, **_kwargs):
            return self._provider

    async def _run() -> bool:
        sess = session_manager.create(
            name="dead-runner-replay",
            model="gpt-5.5",
            cwd="/tmp",
            orchestration_mode="native",
        )
        app_sid = sess["id"]
        codex_sid = str(uuid.uuid4())
        provider = _FakeCodexProvider(app_sid, codex_sid)
        tm = TurnManager(_Coordinator(provider))
        ws_events: list[dict] = []

        async def ws_callback(event: dict) -> None:
            ws_events.append(event)

        original_runtime = turn_manager_mod.runtime_skill_contexts
        original_audit = turn_manager_mod.extension_audit_context
        original_instructions = turn_manager_mod.extension_user_instruction_contexts
        turn_manager_mod.runtime_skill_contexts = lambda *_args, **_kwargs: []
        turn_manager_mod.extension_audit_context = lambda *_args, **_kwargs: []
        turn_manager_mod.extension_user_instruction_contexts = lambda *_args, **_kwargs: []
        try:
            result = await tm._drive_cli_run(
                prompt="do it",
                cwd="/tmp",
                model="gpt-5.5",
                session_id=codex_sid,
                ws_callback=ws_callback,
                app_session_id=app_sid,
                cancel_event=asyncio.Event(),
                session_id_field="agent_session_id",
                mode="native",
                turn_run_id=str(uuid.uuid4()),
            )
        finally:
            turn_manager_mod.runtime_skill_contexts = original_runtime
            turn_manager_mod.extension_audit_context = original_audit
            turn_manager_mod.extension_user_instruction_contexts = original_instructions
        events = result.get("events") or []
        if result.get("success") is not True:
            print(f"  expected success result, got {result!r}")
            return False
        if not any(
            block.get("type") == "tool_result"
            for event in events
            for block in ((event.get("data") or {}).get("message") or {}).get("content", [])
            if isinstance(block, dict)
        ):
            print(f"  result events missing structured tool_result: {events!r}")
            return False
        output = _extract_output_text(events)
        if "hidden-tool-output" in output or "visible final answer" not in output:
            print(f"  bad extracted output: {output!r}")
            return False
        if not any(event.get("type") == "agent_message" for event in ws_events):
            print(f"  replayed events were not emitted through ws_callback: {ws_events!r}")
            return False
        return True

    return asyncio.run(_run())


def test_codex_replay_includes_child_subagent_panel_events() -> bool:
    app_sid, asst_id = _seed_session_with_streaming_assistant()
    parent_sid = str(uuid.uuid4())
    child_sid = str(uuid.uuid4())
    run_id = _seed_codex_run(
        app_sid=app_sid,
        codex_sid=parent_sid,
        pid=os.getpid(),
        events=[
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "call_agent",
                    "arguments": "{\"message\":\"review\"}",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_agent",
                    "output": json.dumps({"agent_id": child_sid}),
                },
            },
        ],
        complete=True,
        target_message_id=asst_id,
    )
    run_dir = runs_root() / run_id
    child_path = run_dir / "child-rollout.jsonl"
    with child_path.open("wb") as f:
        f.write(json.dumps(_make_assistant_text_event("parent history")).encode() + b"\n")
        f.write(json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "child prompt"},
        }).encode() + b"\n")
        child_start = f.tell()
        f.write(json.dumps(_make_assistant_text_event("child answer")).encode() + b"\n")
    backend_state_path = run_dir / "backend_state.json"
    backend_state = json.loads(backend_state_path.read_text(encoding="utf-8"))
    backend_state["child_sources"] = {
        child_sid: {
            "agent_id": child_sid,
            "jsonl_path": str(child_path),
            "start_byte": child_start,
            "processed_byte_offset": child_path.stat().st_size,
            "delegation_id": f"codex_subagent_{child_sid}",
        }
    }
    backend_state_path.write_text(json.dumps(backend_state), encoding="utf-8")

    events, _ = _replay_from_codex_rollout(run_dir)
    if not any(e.get("type") == "worker_start" for e in events):
        print("  missing worker_start")
        return False
    if not any(e.get("type") == "worker_event" for e in events):
        print("  missing worker_event")
        return False

    sess = session_manager.get(app_sid) or {}
    last_asst = next(m for m in sess.get("messages", []) if m.get("id") == asst_id)
    _replay_and_apply(
        persist_sid=app_sid,
        run_id=run_id,
        mode="native",
        claude_sid=parent_sid,
        sess=sess,
        last_asst=last_asst,
        msg_id=asst_id,
    )
    hydrated = session_manager.get(app_sid) or {}
    msg = next(m for m in hydrated.get("messages", []) if m.get("id") == asst_id)
    panels = msg.get("workers") or []
    panel = next(
        (p for p in panels if p.get("delegation_id") == f"codex_subagent_{child_sid}"),
        None,
    )
    parent_text = json.dumps(msg.get("events") or [])
    child_text = json.dumps((panel or {}).get("events") or [])
    ok = (
        panel is not None
        and "child answer" in child_text
        and "parent history" not in child_text
        and "child answer" not in parent_text
    )
    if not ok:
        print(f"  panel={panel!r} parent_text={parent_text[:200]} child_text={child_text[:200]}")
    return ok


def test_codex_replay_derives_missing_child_sources_from_actual_wait_shape() -> bool:
    app_sid, asst_id = _seed_session_with_streaming_assistant()
    parent_sid = str(uuid.uuid4())
    child_sid = "019eea6e-18bb-74f2-9e6c-2446ec215861"
    run_id = _seed_codex_run(
        app_sid=app_sid,
        codex_sid=parent_sid,
        pid=os.getpid(),
        events=[
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "wait_agent",
                    "call_id": "call_yq2iLLugCLpXkKvlXu6EL0qz",
                    "arguments": json.dumps({
                        "targets": [child_sid],
                        "timeout_ms": 300000,
                    }),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_yq2iLLugCLpXkKvlXu6EL0qz",
                    "output": json.dumps({
                        "status": {
                            child_sid: {
                                "completed": "`backend/native_files_manager.py`: SEND BACK."
                            },
                        },
                        "timed_out": False,
                    }),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": (
                            "<subagent_notification>\n"
                            + json.dumps({
                                "agent_path": child_sid,
                                "status": {
                                    "completed": "`backend/native_files_manager.py`: SEND BACK."
                                },
                            })
                            + "\n</subagent_notification>"
                        ),
                    }],
                },
            },
        ],
        complete=True,
        target_message_id=asst_id,
    )
    run_dir = runs_root() / run_id
    child_path = run_dir / "child-rollout.jsonl"
    with child_path.open("wb") as f:
        f.write(json.dumps(_make_assistant_text_event("parent history")).encode() + b"\n")
        f.write(json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "child prompt"},
        }).encode() + b"\n")
        f.write(json.dumps(_make_assistant_text_event("child answer")).encode() + b"\n")
    backend_state_path = run_dir / "backend_state.json"
    backend_state = json.loads(backend_state_path.read_text(encoding="utf-8"))
    backend_state.pop("child_sources", None)
    backend_state_path.write_text(json.dumps(backend_state), encoding="utf-8")

    import codex_native

    orig_resolve = codex_native.resolve_rollout_path
    codex_native.resolve_rollout_path = lambda sid: child_path if sid == child_sid else None  # type: ignore
    try:
        events, _ = _replay_from_codex_rollout(run_dir)
    finally:
        codex_native.resolve_rollout_path = orig_resolve  # type: ignore
    delegation_id = f"codex_subagent_call_yq2iLLugCLpXkKvlXu6EL0qz_{child_sid}"
    worker_starts = [
        e for e in events
        if e.get("type") == "worker_start"
        and (e.get("data") or {}).get("delegation_id") == delegation_id
    ]
    if len(worker_starts) != 1:
        print(f"  worker_starts={worker_starts!r}")
        return False
    worker_events = [
        e for e in events
        if e.get("type") == "worker_event"
        and (e.get("data") or {}).get("delegation_id") == delegation_id
    ]
    if not worker_events:
        print("  missing derived worker events")
        return False

    sess = session_manager.get(app_sid) or {}
    last_asst = next(m for m in sess.get("messages", []) if m.get("id") == asst_id)
    orig_resolve = codex_native.resolve_rollout_path
    codex_native.resolve_rollout_path = lambda sid: child_path if sid == child_sid else None  # type: ignore
    try:
        _replay_and_apply(
            persist_sid=app_sid,
            run_id=run_id,
            mode="native",
            claude_sid=parent_sid,
            sess=sess,
            last_asst=last_asst,
            msg_id=asst_id,
        )
    finally:
        codex_native.resolve_rollout_path = orig_resolve  # type: ignore
    hydrated = session_manager.get(app_sid) or {}
    msg = next(m for m in hydrated.get("messages", []) if m.get("id") == asst_id)
    panels = [
        p for p in (msg.get("workers") or [])
        if p.get("delegation_id") == delegation_id
    ]
    child_text = json.dumps((panels[0] if panels else {}).get("events") or [])
    parent_text = json.dumps(msg.get("events") or [])
    ok = (
        len(panels) == 1
        and "child answer" in child_text
        and "child answer" not in parent_text
        and "parent history" not in child_text
    )
    if not ok:
        print(f"  panels={panels!r} parent_text={parent_text[:200]} child_text={child_text[:200]}")
    return ok


def test_codex_replay_splits_reused_child_by_parent_tool_call() -> bool:
    app_sid, asst_id = _seed_session_with_streaming_assistant()
    parent_sid = str(uuid.uuid4())
    child_sid = str(uuid.uuid4())
    run_id = _seed_codex_run(
        app_sid=app_sid,
        codex_sid=parent_sid,
        pid=os.getpid(),
        events=[
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "wait_agent",
                    "call_id": "call_first",
                    "arguments": json.dumps({"targets": [child_sid]}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_first",
                    "output": json.dumps({
                        "status": {child_sid: {"completed": "first done"}},
                        "timed_out": False,
                    }),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "wait_agent",
                    "call_id": "call_second",
                    "arguments": json.dumps({"targets": [child_sid]}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_second",
                    "output": json.dumps({
                        "status": {child_sid: {"completed": "second done"}},
                        "timed_out": False,
                    }),
                },
            },
        ],
        complete=True,
        target_message_id=asst_id,
    )
    run_dir = runs_root() / run_id
    child_path = run_dir / "child-rollout.jsonl"
    with child_path.open("wb") as f:
        f.write(json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "child prompt"},
        }).encode() + b"\n")
        f.write(json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "first child answer"}],
                "parent_call_id": "call_first",
            },
        }).encode() + b"\n")
        f.write(json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "second child answer"}],
                "parent_call_id": "call_second",
            },
        }).encode() + b"\n")
    backend_state_path = run_dir / "backend_state.json"
    backend_state = json.loads(backend_state_path.read_text(encoding="utf-8"))
    backend_state.pop("child_sources", None)
    backend_state_path.write_text(json.dumps(backend_state), encoding="utf-8")

    import codex_native

    orig_resolve = codex_native.resolve_rollout_path
    codex_native.resolve_rollout_path = lambda sid: child_path if sid == child_sid else None  # type: ignore
    try:
        events, _ = _replay_from_codex_rollout(run_dir)
        worker_starts = [e for e in events if e.get("type") == "worker_start"]
        if len(worker_starts) != 2:
            print(f"  worker_starts={worker_starts!r}")
            return False
        sess = session_manager.get(app_sid) or {}
        last_asst = next(m for m in sess.get("messages", []) if m.get("id") == asst_id)
        _replay_and_apply(
            persist_sid=app_sid,
            run_id=run_id,
            mode="native",
            claude_sid=parent_sid,
            sess=sess,
            last_asst=last_asst,
            msg_id=asst_id,
        )
    finally:
        codex_native.resolve_rollout_path = orig_resolve  # type: ignore

    hydrated = session_manager.get(app_sid) or {}
    msg = next(m for m in hydrated.get("messages", []) if m.get("id") == asst_id)
    panels = {p.get("delegation_id"): p for p in (msg.get("workers") or [])}
    first = panels.get(f"codex_subagent_call_first_{child_sid}") or {}
    second = panels.get(f"codex_subagent_call_second_{child_sid}") or {}
    first_text = json.dumps(first.get("events") or [])
    second_text = json.dumps(second.get("events") or [])
    ok = (
        "first child answer" in first_text
        and "second child answer" not in first_text
        and "second child answer" in second_text
        and "first child answer" not in second_text
    )
    if not ok:
        print(f"  panels={panels!r}")
    return ok


def test_codex_provider_child_setup_persists_source_and_starts_panel() -> bool:
    async def _run() -> bool:
        child_sid = str(uuid.uuid4())
        run_dir = runs_root() / str(uuid.uuid4())
        run_dir.mkdir(parents=True, exist_ok=True)
        child_path = run_dir / "child-rollout.jsonl"
        with child_path.open("wb") as f:
            f.write(json.dumps({
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "child prompt"},
            }).encode() + b"\n")
            start_byte = f.tell()
            f.write(json.dumps(_make_assistant_text_event("child answer")).encode() + b"\n")
        queue: asyncio.Queue = asyncio.Queue()
        rs = RunState(
            run_id=run_dir.name,
            run_dir=run_dir,
            popen=SimpleNamespace(pid=os.getpid()),
            mode="native",
            app_session_id="app",
            queue=queue,
        )
        source_key = f"call_agent_{child_sid}"
        delegation_id = f"codex_subagent_{source_key}"
        rs.child_sources[source_key] = {
            "agent_id": child_sid,
            "source_key": source_key,
            "parent_tool_use_id": "call_agent",
            "jsonl_path": str(child_path),
            "start_byte": start_byte,
            "processed_byte_offset": start_byte,
            "delegation_id": delegation_id,
            "insert_at": 3,
        }
        provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})
        await provider._ensure_child_tailer(
            rs,
            source_key,
            child_sid,
            rs.child_sources[source_key],
            {"type": "user"},
        )
        try:
            first = queue.get_nowait()
        except asyncio.QueueEmpty:
            print("  missing worker_start queue event")
            return False
        ok = (
            first.type == "worker_start"
            and first.data.get("delegation_id") == delegation_id
            and first.data.get("insert_at") == 3
            and source_key in rs.child_sources
            and rs.child_sources[source_key].get("jsonl_path") == str(child_path)
            and rs.child_sources[source_key].get("insert_at") == 3
        )
        for tailer in rs.child_tailers.values():
            tailer.stop()
        for task in rs.child_tailer_tasks.values():
            task.cancel()
        await asyncio.gather(*rs.child_tailer_tasks.values(), return_exceptions=True)
        if not ok:
            print(f"  first={first!r} child_sources={rs.child_sources!r}")
        return ok

    return asyncio.run(_run())


def test_codex_provider_starts_child_panel_from_spawn_result() -> bool:
    async def _run() -> bool:
        parent_sid = str(uuid.uuid4())
        child_sid = str(uuid.uuid4())
        run_dir = runs_root() / str(uuid.uuid4())
        run_dir.mkdir(parents=True, exist_ok=True)
        parent_path = run_dir / "parent-rollout.jsonl"
        child_path = run_dir / "child-rollout.jsonl"
        with parent_path.open("wb") as f:
            f.write(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "call_agent",
                    "arguments": "{\"message\":\"review\"}",
                },
            }).encode() + b"\n")
            f.write(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_agent",
                    "output": json.dumps({"agent_id": child_sid}),
                },
            }).encode() + b"\n")
        with child_path.open("wb") as f:
            f.write(json.dumps({
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "child prompt"},
            }).encode() + b"\n")
            f.write(json.dumps(_make_assistant_text_event("child answer")).encode() + b"\n")
        (run_dir / "state.json").write_text(json.dumps({
            "session_id": parent_sid,
            "jsonl_path": str(parent_path),
            "pre_query_byte_offset": 0,
        }), encoding="utf-8")

        class _Popen:
            pid = os.getpid()

            def poll(self):
                return None

        queue: asyncio.Queue = asyncio.Queue()
        provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})
        rs = RunState(
            run_id=run_dir.name,
            run_dir=run_dir,
            popen=_Popen(),
            mode="native",
            app_session_id="app",
            queue=queue,
        )
        provider._runs[run_dir.name] = rs

        import codex_native

        original_resolve = codex_native.resolve_rollout_path_polled
        async def fake_resolve(thread_id: str, **_kwargs):
            return child_path if thread_id == child_sid else parent_path
        codex_native.resolve_rollout_path_polled = fake_resolve
        try:
            task = asyncio.create_task(provider._bootstrap_run(rs))
            saw_panel = False
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if event.type == "worker_start":
                    saw_panel = True
                    break
            if rs.tailer is not None:
                rs.tailer.stop()
            for tailer in rs.child_tailers.values():
                tailer.stop()
            task.cancel()
            await asyncio.gather(task, *(rs.child_tailer_tasks.values()), return_exceptions=True)
        finally:
            codex_native.resolve_rollout_path_polled = original_resolve
            provider._cleanup_run(run_dir.name)
        if not saw_panel:
            print("  spawn_agent result did not start a child panel")
        return saw_panel

    return asyncio.run(_run())


TESTS = [
    ("codex live orphan is emitted (not skipped)", test_live_orphan_is_emitted_not_skipped),
    ("codex replay reads native rollout jsonl", test_codex_replay_reads_native_rollout_jsonl),
    ("codex live recovery streams rollout events before complete", test_live_recovery_streams_rollout_events_before_complete),
    ("codex live recovery waits for child setup before complete", test_live_recovery_waits_for_child_setup_before_complete),
    ("dead codex wrapper uses rollout terminal complete", test_dead_wrapper_uses_rollout_terminal_complete),
    ("dead codex wrapper resolves missing jsonl path", test_dead_wrapper_resolves_missing_jsonl_path),
    ("dead codex wrapper ignores malformed usage values", test_dead_wrapper_ignores_malformed_usage_values),
    ("codex usage normalizer zeros malformed live values", test_codex_usage_normalizer_zeros_malformed_live_values),
    ("dead codex wrapper uses rollout terminal failure", test_dead_wrapper_uses_rollout_terminal_failure),
    ("dead codex wrapper without terminal fails closed", test_dead_wrapper_without_terminal_still_fails_closed),
    ("codex complete emit recovers missing complete from rollout", test_emit_complete_recovers_missing_complete_from_rollout),
    ("codex loopback POST retries transient reset", test_loopback_post_retries_transient_reset),
    ("provider bootstrap task schedules from worker thread", test_schedule_loop_task_from_worker_thread),
    ("provider bootstrap schedule does not block under loop lag", test_schedule_loop_task_no_block_under_loop_lag),
    ("codex MCP string error normalizes", test_codex_mcp_string_error_normalizes),
    ("codex dead-runner replay preserves tool result structure", test_codex_dead_runner_replay_preserves_tool_result_structure),
    ("codex replay dedup allows mutated same uuid", test_codex_replay_dedup_allows_mutated_same_uuid),
    ("turn manager dead runner replays codex rollout events", test_turn_manager_dead_runner_replays_codex_rollout_events),
    ("codex replay includes child subagent panel events", test_codex_replay_includes_child_subagent_panel_events),
    ("codex replay derives missing child sources from actual wait shape", test_codex_replay_derives_missing_child_sources_from_actual_wait_shape),
    ("codex replay splits reused child by parent tool call", test_codex_replay_splits_reused_child_by_parent_tool_call),
    ("codex provider child setup persists source and starts panel", test_codex_provider_child_setup_persists_source_and_starts_panel),
    ("codex provider starts child panel from spawn result", test_codex_provider_starts_child_panel_from_spawn_result),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    print(f"all {len(TESTS)} tests passed" if not failed
          else f"{failed} of {len(TESTS)} test(s) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
