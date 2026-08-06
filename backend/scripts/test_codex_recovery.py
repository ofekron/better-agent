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
import concurrent.futures
import io
import json
import logging
import os
import shutil
import subprocess
import sys
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

from scripts.codex_execution_test_support import runner_authority  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from codex_execution_contract import build_codex_execution_contract  # noqa: E402
from codex_execution_runtime import codex_provider_contract  # noqa: E402
from execution_template import prepare_execution  # noqa: E402
from provider_execution_contract import provider_family_contract  # noqa: E402
from provider_runner_launch import capture_runner_launch  # noqa: E402
from runs_dir import runs_root  # noqa: E402
from provider import schedule_loop_task  # noqa: E402
from event_bus import EventBus  # noqa: E402
from lifecycle_command_engine import LifecycleCommandEngine  # noqa: E402
from provider_codex import CodexProvider, RunState, read_codex_run_rollout_events  # noqa: E402
from process_identity import process_identity_to_dict  # noqa: E402
from codex_usage import token_usage_from_codex_usage  # noqa: E402
from event_shape import extract_output_text as _extract_output_text  # noqa: E402
from codex_normalize import _normalize_mcp_tool_completed  # noqa: E402
from runner_codex import _post_loopback_sync  # noqa: E402
from codex_native import CodexRolloutNormalizer  # noqa: E402
import turn_manager as turn_manager_mod  # noqa: E402
from turn_manager import TurnManager, _missing_event_dicts  # noqa: E402
from run_recovery import (  # noqa: E402
    _codex_replay_bound,
    _finalize_sync,
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


def _make_task_complete_event() -> dict:
    return {
        "type": "event_msg",
        "payload": {"type": "task_complete"},
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
    family_contract: bool = False,
) -> str:
    """Synthesize a codex run dir: native rollout jsonl + codex_stderr.log
    (NOT gemini_stderr.log) + state/backend_state. `pid` is stamped as
    runner_pid; `complete` controls whether complete.json exists.

    `family_contract=True` freezes a `provider_contract.type == "openai"`
    envelope onto the (still `provider_kind="codex"`) artifact, matching
    what `better_agent_runner`'s openai-family delegation actually
    persists via `provider_family_execution_runtime.prepare_family_execution`
    (see `provider_execution_contract.provider_family_contract`) — as
    opposed to native codex execution, whose envelope type is "codex"."""
    run_id = run_id or str(uuid.uuid4())
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    authority_root = Path(_TMP_HOME) / "codex-execution-authority"
    config_root = authority_root / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    config_path = config_root / "config.toml"
    if not config_path.exists():
        config_path.write_text("", encoding="utf-8")
    launcher = authority_root / "codex"
    if not launcher.exists():
        os.link(Path(sys.executable).resolve(), launcher)
    provider_record = {
        "id": "codex-test",
        "kind": "codex",
        "generation": "e5ef524a-58ae-44cf-bfee-80be44e5da9e",
        "revision": 1,
        "execution_revision": 1,
        "mode": "subscription",
        "config_dir": str(config_root),
    }
    contract = build_codex_execution_contract(
        provider_record,
        launcher_path=str(launcher),
        environment_selectors={"CODEX_HOME": str(config_root)},
        config_paths=(
            str(config_path),
            str(config_root / "auth.json"),
        ),
    )
    provider_contract = (
        provider_family_contract(
            {**provider_record, "kind": "openai"},
            payload={},
        )
        if family_contract
        else codex_provider_contract(contract)
    )
    runtime_policy = {
        "context_strategy": None,
        "disabled_runtime_skills": [],
        "permission": {},
        "request_user_input_enabled": False,
        "run_policy": {},
        "runtime_agent_manifest": None,
        "worker_working_mode": None,
        "working_mode": None,
        "runner_launch": capture_runner_launch(
            run_dir=run_dir,
            executable_path=sys.executable,
            runner_entry=Path(_BACKEND) / "runner_codex.py",
            runner_kind="codex",
            runner_module="runner_codex",
            frozen=False,
        ).to_dict(),
    }
    execution = prepare_execution(
        provider_record,
        runtime_policy=runtime_policy,
        provider_contract=provider_contract,
        run_id=run_id,
        prompt="recover",
        cwd="/tmp",
        model="gpt-5.5",
        reasoning_effort="high",
        session_id=codex_sid,
        mode="native",
        app_session_id=app_sid,
        target_message_id=target_message_id,
    )
    (run_dir / "execution.json").write_text(
        json.dumps(execution.artifact.to_dict()),
        encoding="utf-8",
    )

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
        "runner_identity": process_identity_to_dict(pid),
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


def test_codex_replay_reads_native_rollout_jsonl() -> None:
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

    last_asst = _last_assistant(session_manager.get(app_sid))
    _replay_and_apply(
        persist_sid=app_sid,
        run_id=run_id,
        mode="native",
        claude_sid=codex_sid,
        msg_id=last_asst["id"],
    )

    sess = session_manager.get(app_sid)
    asst = next((m for m in sess["messages"] if m["id"] == asst_id), None)
    assert asst is not None, "assistant message disappeared"
    evs = asst.get("events") or []
    assert len(evs) == len(events), f"expected {len(events)} events, got {len(evs)}"
    for e in evs:
        assert e.get("type") == "agent_message", (
            f"expected agent_message envelope, got {e.get('type')!r}"
        )
    content = asst.get("content") or ""
    assert "old turn" not in content, (
        f"replay ignored byte offset and included old turn: {content!r}"
    )
    # Both events applied. Content reflects the last replayed assistant
    # message — separate complete messages replace rather than concatenate.
    assert "world" in content, f"expected replayed text in content, got {content!r}"


def test_live_recovery_streams_rollout_events_before_complete() -> None:
    class _TurnManager:
        def __init__(self) -> None:
            self.active_run_ids = {}

        def run_state_add(
            self, app_sid, *, run_id, kind, target_message_id, pid,
            foreground_status=None, background_work_ids=None,
            activity_revision=None, turn_id=None, lifecycle_msg_id=None,
            **_kwargs,
        ):
            self.active_run_ids.setdefault(app_sid, []).append(run_id)

        def run_state_remove(self, app_sid, run_id):
            if run_id in self.active_run_ids.get(app_sid, []):
                self.active_run_ids[app_sid].remove(run_id)

        async def emit_run_state(self, app_sid):
            return None

        def register_recovered_turn_creator(self, *_args):
            return None

        def run_state_mark_provider_submitted(self, *_args):
            return None

    class _Coordinator:
        def __init__(self) -> None:
            self.turn_manager = _TurnManager()

    async def _run() -> None:
        import event_bus_subscribers
        from event_journal import bind_event_journal_loop

        bind_event_journal_loop(asyncio.get_running_loop())
        event_bus_subscribers.bind_event_journal_writer()
        event_bus_subscribers.bind_session_content_projection()
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
            watermark_before = session_manager.get_fold_watermark(app_sid)
            await _integrate_one(_Coordinator(), provider, desc)

            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                rs = provider._runs.get(run_id)
                if rs is not None and rs.tailer is not None:
                    await rs.tailer.drain_available()
                    break
                await asyncio.sleep(0)
            while asyncio.get_running_loop().time() < deadline:
                await event_bus_subscribers.await_session_content_projection(
                    app_sid,
                )
                if (
                    session_manager.get_fold_watermark(app_sid)
                    > watermark_before
                ):
                    break
                await asyncio.sleep(0)
            rendered = ""
            while asyncio.get_running_loop().time() < deadline:
                sess = session_manager.get(app_sid) or {}
                asst = next((m for m in sess.get("messages", []) if m.get("id") == asst_id), {})
                rendered = json.dumps(asst.get("events") or [])
                if "live after restart" in rendered:
                    break
                await asyncio.sleep(0.05)
            assert "live after restart" in rendered, (
                f"recovered live event was not rendered before complete: {rendered!r}"
            )

            run_dir = runs_root() / run_id
            backend_state = json.loads((run_dir / "backend_state.json").read_text(encoding="utf-8"))
            assert backend_state.get("processed_byte_offset", 0) > before_offset, (
                f"cursor did not advance: before={before_offset} state={backend_state!r}"
            )
            assert "processed_byte" not in backend_state, (
                f"codex state wrote wrong cursor key: {backend_state!r}"
            )

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
            assert run_id not in provider._runs, (
                "recovered live run did not clean up after complete"
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    asyncio.run(_run())


def test_live_recovery_waits_for_child_setup_before_complete() -> None:
    class _TurnManager:
        def __init__(self) -> None:
            self.active_run_ids = {}

        def run_state_add(
            self, app_sid, *, run_id, kind, target_message_id, pid,
            foreground_status=None, background_work_ids=None,
            activity_revision=None, turn_id=None, lifecycle_msg_id=None,
            **_kwargs,
        ):
            self.active_run_ids.setdefault(app_sid, []).append(run_id)

        def run_state_remove(self, app_sid, run_id):
            if run_id in self.active_run_ids.get(app_sid, []):
                self.active_run_ids[app_sid].remove(run_id)

        async def emit_run_state(self, app_sid):
            return None

        def register_recovered_turn_creator(self, *_args):
            return None

        def run_state_mark_provider_submitted(self, *_args):
            return None

    class _Coordinator:
        def __init__(self) -> None:
            self.turn_manager = _TurnManager()

    async def _run() -> None:
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
                f.write(json.dumps(_make_task_complete_event()).encode() + b"\n")
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
            assert child_offset > child_start, (
                f"child cursor did not advance before cleanup: {child_offset} <= {child_start}"
            )
            assert run_id not in provider._runs, (
                "recovered live run did not clean up after child setup"
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    asyncio.run(_run())


def test_dead_wrapper_uses_rollout_terminal_complete() -> None:
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
    assert desc is not None, "recovered descriptor missing"
    assert desc.get("recovered_as") == "completed_from_rollout", (
        f"expected completed_from_rollout, got {desc.get('recovered_as')!r}"
    )
    complete = json.loads((runs_root() / run_id / "complete.json").read_text(encoding="utf-8"))
    assert complete.get("success") is True, f"expected success=True, got {complete!r}"
    usage = complete.get("token_usage") or {}
    assert usage.get("total_tokens") == 17 and usage.get("cache_read_input_tokens") == 3, (
        f"unexpected token usage: {usage!r}"
    )


def test_terminal_family_delegated_run_recovers_without_authority_error() -> None:
    """Bug: a `provider_kind="codex"` run whose `provider_contract.type`
    is "openai" (better_agent_runner openai-family delegation — a
    legitimate shape, not corruption; see `family_execution_kind`) used to
    permanently fail `recover_in_flight` on every restart once it already
    had a terminal `complete.json`, because `codex_contract_from_artifact`
    was called unconditionally and raises on any non-"codex" contract type,
    even though the terminal branch never reads `contract` at all."""
    app_sid, asst_id = _seed_session_with_streaming_assistant()
    codex_sid = str(uuid.uuid4())
    run_id = _seed_codex_run(
        app_sid=app_sid,
        codex_sid=codex_sid,
        pid=0,
        events=[_make_assistant_text_event("already finished")],
        complete=True,
        target_message_id=asst_id,
        family_contract=True,
    )

    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        recovered = CodexProvider({"id": "codex-test"}).recover_in_flight()
    finally:
        root_logger.removeHandler(handler)

    log_text = log_stream.getvalue()
    assert "invalid execution authority" not in log_text, (
        f"unexpected authority-error log: {log_text!r}"
    )
    desc = next((item for item in recovered if item.get("run_id") == run_id), None)
    assert desc is not None, (
        "terminal family-delegated run was skipped by recover_in_flight"
    )
    assert desc.get("has_complete_json") is True, (
        f"expected has_complete_json=True, got {desc.get('has_complete_json')!r}"
    )
    assert desc.get("provider_kind") == "codex", (
        f"expected provider_kind=codex, got {desc.get('provider_kind')!r}"
    )


def test_dead_wrapper_resolves_missing_jsonl_path() -> None:
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
    assert complete.get("success") is True, f"expected success=True, got {complete!r}"


def test_dead_wrapper_ignores_malformed_usage_values() -> None:
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
    assert usage == expected, f"expected malformed usage to zero, got {usage!r}"


def test_codex_usage_normalizer_zeros_malformed_live_values() -> None:
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
    assert usage == expected, f"expected malformed live usage to zero, got {usage!r}"


def test_dead_wrapper_uses_rollout_terminal_failure() -> None:
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
    assert complete.get("success") is False, f"expected success=False, got {complete!r}"
    assert complete.get("error"), f"expected preserved failure error, got {complete!r}"
    assert complete.get("error") != "runner died before completion (recovered at startup)", (
        f"terminal turn.failed was ignored: {complete!r}"
    )


def test_dead_wrapper_without_terminal_still_fails_closed() -> None:
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
    assert complete.get("success") is False, f"expected success=False, got {complete!r}"
    assert (
        complete.get("outcome") == "recoverable_partial"
        and complete.get("recoverable") is True
        and complete.get("cause") == "runner died before completion (recovered at startup)"
    ), f"unexpected recoverable partial: {complete!r}"


def test_emit_complete_recovers_missing_complete_from_rollout() -> None:
    codex_sid = str(uuid.uuid4())
    run_id = _seed_codex_run(
        app_sid="sess-emit",
        codex_sid=codex_sid,
        pid=0,
        events=[_make_turn_completed_event()],
        complete=False,
    )

    async def _run() -> None:
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
        assert payload.get("success") is True, f"expected success=True, got {payload!r}"

    asyncio.run(_run())


def test_codex_ambient_cancel_preserves_recoverable_app_server() -> None:
    import runner_codex

    calls: list[str] = []
    codex_sid = str(uuid.uuid4())
    rollout = runs_root() / "ambient-cancel-rollout.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(
        json.dumps(_make_assistant_text_event("still working")) + "\n",
        encoding="utf-8",
    )

    class _Stdout:
        def __init__(self) -> None:
            self.rows = [
                {"type": "thread.started", "thread_id": codex_sid},
                {"type": "turn.started", "turn_id": "turn-ambient"},
            ]

        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            await asyncio.sleep(0)
            if self.rows:
                return (json.dumps(self.rows.pop(0)) + "\n").encode("utf-8")
            raise asyncio.CancelledError()

    class _Proc:
        pid = 43210
        process_group_id = 87654
        returncode = None
        stdout = _Stdout()
        thread_id = codex_sid
        turn_id = "turn-ambient"
        _pending_tool_calls: set = set()
        _stderr_task: asyncio.Task
        _mapped: asyncio.Queue

        def __init__(self) -> None:
            self._mapped = asyncio.Queue()
            self._stderr_task = asyncio.create_task(asyncio.sleep(999))

        async def request(self, *_args, **_kwargs):
            calls.append("request")

        async def close_input(self) -> None:
            calls.append("close")

        async def wait(self) -> int:
            calls.append("wait")
            return 0

    class _Control:
        def signal_owned_group(self, group_id: int) -> None:
            assert group_id == _Proc.process_group_id
            calls.append("signal")

        def force_kill_owned_group(self, group_id: int) -> None:
            assert group_id == _Proc.process_group_id
            calls.append("kill")

    async def _run() -> None:
        import codex_native

        run_dir = runs_root() / str(uuid.uuid4())
        run_dir.mkdir(parents=True)
        original_start = runner_codex._start_app_server
        original_resolve_rollout = codex_native.resolve_rollout_path
        original_resolve_rollout_polled = codex_native.resolve_rollout_path_polled
        original_control = runner_codex._process_control
        original_bridge = runner_codex._bridge_extension_mcp_dynamic_tools
        try:
            async def _fake_start(*_args, **_kwargs) -> _Proc:
                return _Proc()

            async def _resolve_rollout_polled(_sid, timeout=5.0):
                del timeout
                return rollout

            runner_codex._start_app_server = _fake_start  # type: ignore[assignment]
            codex_native.resolve_rollout_path = lambda _sid: rollout  # type: ignore[assignment]
            codex_native.resolve_rollout_path_polled = _resolve_rollout_polled  # type: ignore[assignment]
            runner_codex._process_control = lambda: _Control()  # type: ignore[assignment]

            async def _bridge_noop(**_kwargs):
                return None

            runner_codex._bridge_extension_mcp_dynamic_tools = _bridge_noop  # type: ignore[assignment]
            contract, launch = runner_authority(run_dir)
            code = await runner_codex._run(run_dir, {
                "prompt": "continue",
                "provider_kind": "codex",
                "cwd": "/tmp",
                "mode": "native",
                "session_id": codex_sid,
                "app_session_id": "app-ambient",
                "permission": {"approval_policy": "never", "sandbox": "danger-full-access"},
                "bare_config": True,
                "provider_run_config": {},
            }, contract, launch)
        finally:
            runner_codex._start_app_server = original_start  # type: ignore[assignment]
            codex_native.resolve_rollout_path = original_resolve_rollout  # type: ignore[assignment]
            codex_native.resolve_rollout_path_polled = original_resolve_rollout_polled  # type: ignore[assignment]
            runner_codex._process_control = original_control  # type: ignore[assignment]
            runner_codex._bridge_extension_mcp_dynamic_tools = original_bridge  # type: ignore[assignment]

        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        ok = (
            code == 130
            and not (run_dir / "complete.json").exists()
            and state.get("complete") is False
            and state.get("session_id") == codex_sid
            and state.get("cli_pid") == 43210
            and state.get("rollout_path") == str(rollout)
            and "signal" not in calls
            and "kill" not in calls
        )
        if not ok:
            print(f"  code={code} calls={calls!r} state={state!r}")
        assert ok

    asyncio.run(_run())


def test_codex_explicit_cancel_still_stops_app_server() -> None:
    import runner_codex

    calls: list[str] = []

    class _Proc:
        pid = 54321
        process_group_id = 54321
        returncode = None

        async def close_input(self) -> None:
            calls.append("close")

        async def wait(self) -> int:
            calls.append("wait")
            return 0

    class _Control:
        def signal_owned_group(self, _group_id: int) -> None:
            calls.append("signal")

        def force_kill_owned_group(self, _group_id: int) -> None:
            calls.append("kill")

    async def _run() -> None:
        original_control = runner_codex._process_control
        try:
            runner_codex._process_control = lambda: _Control()  # type: ignore[assignment]
            await runner_codex._settle_app_server_process(
                _Proc(),
                rollout_terminal_completion=False,
                stop_requested=True,
                log=runner_codex.logging.getLogger("test"),
            )
        finally:
            runner_codex._process_control = original_control  # type: ignore[assignment]
        assert calls == ["signal", "wait", "kill"], f"calls={calls!r}"

    asyncio.run(_run())


def test_codex_fallback_rollout_completion_settles_app_server() -> None:
    import runner_codex

    calls: list[str] = []
    codex_sid = str(uuid.uuid4())
    rollout = runs_root() / "fallback-terminal-rollout.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(
        json.dumps(_make_assistant_text_event("done")) + "\n"
        + json.dumps(_make_turn_completed_event()) + "\n",
        encoding="utf-8",
    )

    class _Stdout:
        def __init__(self) -> None:
            self.rows = [
                {"type": "thread.started", "thread_id": codex_sid},
                {"type": "turn.started", "turn_id": "turn-fallback"},
            ]

        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            await asyncio.sleep(0)
            if self.rows:
                return (json.dumps(self.rows.pop(0)) + "\n").encode("utf-8")
            raise StopAsyncIteration

    class _Proc:
        pid = 65432
        process_group_id = 65432
        returncode = None
        stdout = _Stdout()
        thread_id = codex_sid
        turn_id = "turn-fallback"
        _pending_tool_calls: set = set()
        _stderr_task: asyncio.Task
        _mapped: asyncio.Queue

        def __init__(self) -> None:
            self._mapped = asyncio.Queue()
            self._stderr_task = asyncio.create_task(asyncio.sleep(999))

        async def request(self, *_args, **_kwargs):
            calls.append("request")

        async def close_input(self) -> None:
            calls.append("close")
            self.returncode = 0

        async def wait(self) -> int:
            calls.append("wait")
            return 0

    class _Control:
        def signal_owned_group(self, _group_id: int) -> None:
            calls.append("signal")

        def force_kill_owned_group(self, _group_id: int) -> None:
            calls.append("kill")

    async def _run() -> None:
        import codex_native

        run_dir = runs_root() / str(uuid.uuid4())
        run_dir.mkdir(parents=True)
        original_start = runner_codex._start_app_server
        original_resolve_rollout = codex_native.resolve_rollout_path
        original_resolve_rollout_polled = codex_native.resolve_rollout_path_polled
        original_bridge = runner_codex._bridge_extension_mcp_dynamic_tools
        original_wait = runner_codex._wait_rollout_terminal_state
        original_control = runner_codex._process_control
        try:
            async def _fake_start(*_args, **_kwargs) -> _Proc:
                return _Proc()

            async def _resolve_rollout_polled(_sid, timeout=5.0):
                del timeout
                return rollout

            async def _wait_terminal(*_args, **_kwargs):
                return True, {"input_tokens": 1}, True, None

            async def _bridge_noop(**_kwargs):
                return None

            runner_codex._start_app_server = _fake_start  # type: ignore[assignment]
            codex_native.resolve_rollout_path = lambda _sid: rollout  # type: ignore[assignment]
            codex_native.resolve_rollout_path_polled = _resolve_rollout_polled  # type: ignore[assignment]
            runner_codex._bridge_extension_mcp_dynamic_tools = _bridge_noop  # type: ignore[assignment]
            runner_codex._wait_rollout_terminal_state = _wait_terminal  # type: ignore[assignment]
            runner_codex._process_control = lambda: _Control()  # type: ignore[assignment]
            contract, launch = runner_authority(run_dir)
            code = await runner_codex._run(run_dir, {
                "prompt": "continue",
                "provider_kind": "codex",
                "cwd": "/tmp",
                "mode": "native",
                "session_id": codex_sid,
                "app_session_id": "app-fallback",
                "permission": {"approval_policy": "never", "sandbox": "danger-full-access"},
                "bare_config": True,
                "provider_run_config": {},
            }, contract, launch)
        finally:
            runner_codex._start_app_server = original_start  # type: ignore[assignment]
            codex_native.resolve_rollout_path = original_resolve_rollout  # type: ignore[assignment]
            codex_native.resolve_rollout_path_polled = original_resolve_rollout_polled  # type: ignore[assignment]
            runner_codex._bridge_extension_mcp_dynamic_tools = original_bridge  # type: ignore[assignment]
            runner_codex._wait_rollout_terminal_state = original_wait  # type: ignore[assignment]
            runner_codex._process_control = original_control  # type: ignore[assignment]

        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        ok = code == 0 and complete.get("success") is True and calls == ["close", "wait", "kill"]
        if not ok:
            print(f"  code={code} calls={calls!r} complete={complete!r}")
        assert ok

    asyncio.run(_run())


def test_codex_pre_thread_ambient_cancel_cleans_unrecoverable_app_server() -> None:
    import runner_codex

    calls: list[str] = []

    class _Stdout:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            raise asyncio.CancelledError()

    class _Proc:
        pid = 76543
        process_group_id = 76543
        returncode = None
        stdout = _Stdout()
        thread_id = None
        turn_id = None
        _pending_tool_calls: set = set()
        _stderr_task: asyncio.Task
        _mapped: asyncio.Queue

        def __init__(self) -> None:
            self._mapped = asyncio.Queue()
            self._stderr_task = asyncio.create_task(asyncio.sleep(999))

        async def request(self, *_args, **_kwargs):
            calls.append("request")

        async def close_input(self) -> None:
            calls.append("close")

        async def wait(self) -> int:
            calls.append("wait")
            return 0

    class _Control:
        def signal_owned_group(self, _group_id: int) -> None:
            calls.append("signal")

        def force_kill_owned_group(self, _group_id: int) -> None:
            calls.append("kill")

    async def _run() -> None:
        import codex_native

        run_dir = runs_root() / str(uuid.uuid4())
        run_dir.mkdir(parents=True)
        original_start = runner_codex._start_app_server
        original_control = runner_codex._process_control
        original_resolve_rollout = codex_native.resolve_rollout_path
        try:
            async def _fake_start(*_args, **_kwargs) -> _Proc:
                return _Proc()

            runner_codex._start_app_server = _fake_start  # type: ignore[assignment]
            runner_codex._process_control = lambda: _Control()  # type: ignore[assignment]
            codex_native.resolve_rollout_path = lambda _sid: None  # type: ignore[assignment]
            contract, launch = runner_authority(run_dir)
            code = await runner_codex._run(run_dir, {
                "prompt": "start",
                "provider_kind": "codex",
                "cwd": "/tmp",
                "mode": "native",
                "app_session_id": "app-pre-thread",
                "permission": {"approval_policy": "never", "sandbox": "danger-full-access"},
                "bare_config": True,
                "provider_run_config": {},
            }, contract, launch)
        finally:
            runner_codex._start_app_server = original_start  # type: ignore[assignment]
            runner_codex._process_control = original_control  # type: ignore[assignment]
            codex_native.resolve_rollout_path = original_resolve_rollout  # type: ignore[assignment]

        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        ok = (
            code == 1
            and complete.get("success") is False
            and complete.get("error") == "cancelled before Codex thread started"
            and calls == ["signal", "wait", "kill"]
        )
        if not ok:
            print(f"  code={code} calls={calls!r} complete={complete!r}")
        assert ok

    asyncio.run(_run())


def test_loopback_post_retries_transient_reset() -> None:
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

    assert res == {"ok": True}, f"expected ok response, got {res!r}"
    assert calls == 2, f"expected retry once, got {calls} calls"


def test_loopback_post_does_not_reread_ambient_token_after_forbidden() -> None:
    import runner_codex

    seen_tokens: list[str | None] = []

    original_urlopen = runner_codex.urllib.request.urlopen

    def fake_urlopen(req, *_args, **_kwargs):
        token = req.headers.get("X-internal-token")
        seen_tokens.append(token)
        if token == "spawn-token":
            raise urllib.error.HTTPError(
                req.full_url,
                403,
                "Forbidden",
                hdrs=None,
                fp=None,
            )
        raise AssertionError("loopback retried with ambient authority")

    try:
        runner_codex.urllib.request.urlopen = fake_urlopen
        try:
            _post_loopback_sync(
                {"x": 1},
                backend_url="http://127.0.0.1:8000",
                internal_token="spawn-token",
                url_path="/api/internal/ask",
                timeout_s=10,
            )
        except RuntimeError as exc:
            if "HTTP 403" not in str(exc):
                raise
        else:
            raise AssertionError("forbidden loopback must fail closed")
    finally:
        runner_codex.urllib.request.urlopen = original_urlopen

    assert seen_tokens == ["spawn-token"], (
        f"expected only prepared token, got {seen_tokens!r}"
    )


def test_loopback_post_surfaces_http_error_detail() -> bool:
    import runner_codex

    original_urlopen = runner_codex.urllib.request.urlopen

    def fake_urlopen(req, *_args, **_kwargs):
        raise urllib.error.HTTPError(
            req.full_url,
            409,
            "Conflict",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":"no idle worker in target_worker_pool"}'),
        )

    try:
        runner_codex.urllib.request.urlopen = fake_urlopen
        try:
            _post_loopback_sync(
                {"x": 1},
                backend_url="http://127.0.0.1:8000",
                internal_token="token",
                url_path="/api/internal/ask",
                timeout_s=10,
            )
        except RuntimeError as exc:
            if str(exc) != "HTTP 409: no idle worker in target_worker_pool":
                print(f"  unexpected error: {exc!r}")
                return False
            return True
        print("  expected HTTP error detail to be surfaced")
        return False
    finally:
        runner_codex.urllib.request.urlopen = original_urlopen


def test_schedule_loop_task_from_worker_thread() -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        done = asyncio.Event()

        async def marker() -> None:
            done.set()

        await asyncio.to_thread(
            schedule_loop_task,
            loop,
            marker(),
            name="test-schedule-loop-task-worker",
        )
        await done.wait()

    asyncio.run(main())


def test_schedule_loop_task_no_block_under_loop_lag() -> None:
    """Regression: scheduling the bootstrap coro from a worker thread must
    NOT synchronously wait for the event loop. The old create_loop_task did
    future.result(timeout=5) and raised TimeoutError — killing the whole
    turn — whenever the loop could not service a call_soon within 5s. With
    the loop deliberately held, scheduling must still return immediately.
    """
    callbacks = []

    async def marker() -> None:
        pass

    class NonServicingLoop:
        def call_soon_threadsafe(self, callback) -> None:
            callbacks.append(callback)

    def reject_synchronous_wait(
        _future: concurrent.futures.Future,
        timeout=None,
    ):
        del timeout
        raise AssertionError("scheduling waited for loop-owned completion")

    loop = NonServicingLoop()
    coro = marker()
    original_result = concurrent.futures.Future.result
    concurrent.futures.Future.result = reject_synchronous_wait
    try:
        schedule_loop_task(
            loop, coro, name="test-schedule-under-lag",
        )
    finally:
        concurrent.futures.Future.result = original_result
        coro.close()

    assert len(callbacks) == 1, (
        f"expected one loop admission callback, got {len(callbacks)}"
    )


def test_codex_mcp_string_error_normalizes() -> None:
    event = _normalize_mcp_tool_completed(
        {"id": "tool-1", "error": "connection reset"},
        "parent-1",
    )
    content = event.get("message", {}).get("content", [])
    text = ((content[0] or {}).get("content") if content else "")
    assert text == "Error: connection reset", (
        f"expected string error content, got {text!r}"
    )


def test_codex_dead_runner_replay_preserves_tool_result_structure() -> None:
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
    assert any(
        block.get("type") == "tool_result"
        for event in events
        for block in ((event.get("data") or {}).get("message") or {}).get("content", [])
        if isinstance(block, dict)
    ), "replay did not preserve tool_result blocks"
    output = _extract_output_text(events)
    assert "secret-tool-output" not in output, (
        f"tool result leaked into assistant output text: {output!r}"
    )
    assert "final answer only" in output, (
        f"assistant text missing from replay output: {output!r}"
    )


def test_codex_replay_dedup_allows_mutated_same_uuid() -> None:
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
    assert missing == [updated], (
        f"expected only mutated same-uuid update, got {missing!r}"
    )


def test_turn_manager_dead_runner_replays_codex_rollout_events() -> None:
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

        def prepare_run(self, **kwargs):
            return prepare_execution(
                {
                    "id": self.id,
                    "kind": self.KIND,
                    "generation": "e5ef524a-58ae-44cf-bfee-80be44e5da9e",
                    "revision": 1,
                    "execution_revision": 1,
                },
                **kwargs,
            )

        def start_run(self, *, execution, loop, queue) -> bool:
            del loop, queue
            if not execution._try_commit_spawn():
                return False
            kwargs = execution.start_arguments()
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
            execution._mark_spawn_completed()
            return True

        def is_running(self, _run_id: str) -> bool:
            return False

        async def is_running_off_loop(self, _run_id: str) -> bool:
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

        async def broadcast_session(self, *_args, **_kwargs) -> None:
            return None

    async def _run() -> None:
        sess = session_manager.create(
            name="dead-runner-replay",
            model="gpt-5.5",
            cwd="/tmp",
            orchestration_mode="native",
        )
        app_sid = sess["id"]
        codex_sid = str(uuid.uuid4())
        provider = _FakeCodexProvider(app_sid, codex_sid)
        coordinator = _Coordinator(provider)
        lifecycle_commands = LifecycleCommandEngine(EventBus())
        coordinator.lifecycle_commands = lifecycle_commands
        tm = TurnManager(coordinator)
        await lifecycle_commands.bind()
        await tm.lifecycle.bind()
        ws_events: list[dict] = []

        async def ws_callback(event: dict) -> None:
            ws_events.append(event)

        original_runtime = turn_manager_mod.runtime_skill_projection
        original_audit = turn_manager_mod.extension_audit_context
        turn_manager_mod.runtime_skill_projection = lambda *_args, **_kwargs: ([], [])
        turn_manager_mod.extension_audit_context = lambda *_args, **_kwargs: []
        try:
            result = await tm._drive_cli_run(
                prompt="do it",
                cwd="/tmp",
                model="gpt-5.5",
                session_id=None,
                ws_callback=ws_callback,
                app_session_id=app_sid,
                cancel_event=asyncio.Event(),
                session_id_field="agent_session_id",
                mode="native",
                turn_run_id=str(uuid.uuid4()),
                lifecycle_message_id="lifecycle-codex-recovery",
            )
        finally:
            turn_manager_mod.runtime_skill_projection = original_runtime
            turn_manager_mod.extension_audit_context = original_audit
            try:
                await tm.lifecycle.close()
            finally:
                await lifecycle_commands.close()
        events = result.get("events") or []
        assert result.get("success") is True, f"expected success result, got {result!r}"
        assert any(
            block.get("type") == "tool_result"
            for event in events
            for block in ((event.get("data") or {}).get("message") or {}).get("content", [])
            if isinstance(block, dict)
        ), f"result events missing structured tool_result: {events!r}"
        output = _extract_output_text(events)
        assert "hidden-tool-output" not in output and "visible final answer" in output, (
            f"bad extracted output: {output!r}"
        )
        assert any(event.get("type") == "agent_message" for event in ws_events), (
            f"replayed events were not emitted through ws_callback: {ws_events!r}"
        )

    asyncio.run(_run())


def test_codex_replay_includes_child_subagent_panel_events() -> None:
    app_sid, asst_id = _seed_session_with_streaming_assistant()
    parent_sid = str(uuid.uuid4())
    child_sid = str(uuid.uuid4())
    unresolved_child_sid = str(uuid.uuid4())
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
    unresolved_child_path = run_dir / "unresolved-child-rollout.jsonl"
    with child_path.open("wb") as f:
        agent_path = "/root/reviewer"
        f.write(json.dumps({
            "type": "session_meta",
            "payload": {"source": {"subagent": {"thread_spawn": {
                "agent_path": agent_path,
            }}}},
        }).encode() + b"\n")
        f.write(json.dumps(_make_assistant_text_event("parent history")).encode() + b"\n")
        f.write(json.dumps({
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": ["parent reasoning"]},
        }).encode() + b"\n")
        f.write(json.dumps({
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "recipient": agent_path,
                "content": "child prompt",
            },
        }).encode() + b"\n")
        child_start = f.tell()
        f.write(json.dumps(_make_assistant_text_event("child answer")).encode() + b"\n")
        f.write(json.dumps(_make_task_complete_event()).encode() + b"\n")
    with unresolved_child_path.open("wb") as f:
        f.write(json.dumps({
            "type": "session_meta",
            "payload": {"source": {"subagent": {"thread_spawn": {
                "agent_path": "/root/unresolved",
            }}}},
        }).encode() + b"\n")
        f.write(json.dumps({
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "recipient": "/root/unresolved",
                "content": "unresolved child prompt",
            },
        }).encode() + b"\n")
        unresolved_child_start = f.tell()
        f.write(json.dumps(
            _make_assistant_text_event("unresolved child partial")
        ).encode() + b"\n")
    backend_state_path = run_dir / "backend_state.json"
    backend_state = json.loads(backend_state_path.read_text(encoding="utf-8"))
    backend_state["child_sources"] = {
        child_sid: {
            "agent_id": child_sid,
            "jsonl_path": str(child_path),
            "start_byte": child_start,
            "processed_byte_offset": child_path.stat().st_size,
            "delegation_id": f"codex_subagent_{child_sid}",
        },
        unresolved_child_sid: {
            "agent_id": unresolved_child_sid,
            "jsonl_path": str(unresolved_child_path),
            "start_byte": unresolved_child_start,
            "processed_byte_offset": unresolved_child_path.stat().st_size,
            "delegation_id": f"codex_subagent_{unresolved_child_sid}",
        }
    }
    backend_state_path.write_text(json.dumps(backend_state), encoding="utf-8")

    events, _ = _replay_from_codex_rollout(run_dir)
    assert any(e.get("type") == "worker_start" for e in events), "missing worker_start"
    assert any(e.get("type") == "worker_event" for e in events), "missing worker_event"
    child_completions = {
        (e.get("data") or {}).get("delegation_id"): (e.get("data") or {}).get("success")
        for e in events
        if e.get("type") == "worker_complete"
    }
    assert (
        child_completions.get(f"codex_subagent_{child_sid}") is True
        and child_completions.get(f"codex_subagent_{unresolved_child_sid}") is False
    ), f"child_completions={child_completions!r}"

    _finalize_sync(
        persist_sid=app_sid,
        run_id=run_id,
        mode="native",
        claude_sid=parent_sid,
        msg_id=asst_id,
        cancelled=False,
    )
    hydrated = session_manager.get(app_sid) or {}
    msg = next(m for m in hydrated.get("messages", []) if m.get("id") == asst_id)
    panels = msg.get("workers") or []
    panels_by_id = {panel.get("delegation_id"): panel for panel in panels}
    panel = panels_by_id.get(f"codex_subagent_{child_sid}")
    unresolved_panel = panels_by_id.get(f"codex_subagent_{unresolved_child_sid}")
    parent_text = json.dumps(msg.get("events") or [])
    child_text = json.dumps((panel or {}).get("events") or [])
    ok = (
        panel is not None
        and panel.get("success") is True
        and unresolved_panel is not None
        and unresolved_panel.get("success") is False
        and "child answer" in child_text
        and "parent history" not in child_text
        and "child answer" not in parent_text
    )
    if not ok:
        print(f"  panel={panel!r} parent_text={parent_text[:200]} child_text={child_text[:200]}")
    assert ok


def test_codex_replay_derives_missing_child_sources_from_v2_activity() -> None:
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
                    "name": "spawn_agent",
                    "call_id": "call_agent",
                    "arguments": json.dumps({"message": "review"}),
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "sub_agent_activity",
                    "event_id": "call_agent",
                    "agent_thread_id": child_sid,
                    "agent_path": "/root/reviewer",
                    "kind": "started",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_agent",
                    "output": json.dumps({"task_name": "/root/reviewer"}),
                },
            },
        ],
        complete=True,
        target_message_id=asst_id,
    )
    run_dir = runs_root() / run_id
    child_path = run_dir / "child-rollout.jsonl"
    with child_path.open("wb") as f:
        agent_path = "/root/reviewer"
        f.write(json.dumps({
            "type": "session_meta",
            "payload": {"source": {"subagent": {"thread_spawn": {
                "agent_path": agent_path,
            }}}},
        }).encode() + b"\n")
        f.write(json.dumps(_make_assistant_text_event("parent history")).encode() + b"\n")
        f.write(json.dumps({
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "recipient": agent_path,
                "content": "child prompt",
            },
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
    delegation_id = f"codex_subagent_call_agent_{child_sid}"
    worker_starts = [
        e for e in events
        if e.get("type") == "worker_start"
        and (e.get("data") or {}).get("delegation_id") == delegation_id
    ]
    assert len(worker_starts) == 1, f"worker_starts={worker_starts!r}"
    worker_events = [
        e for e in events
        if e.get("type") == "worker_event"
        and (e.get("data") or {}).get("delegation_id") == delegation_id
    ]
    assert worker_events, "missing derived worker events"

    orig_resolve = codex_native.resolve_rollout_path
    codex_native.resolve_rollout_path = lambda sid: child_path if sid == child_sid else None  # type: ignore
    try:
        _replay_and_apply(
            persist_sid=app_sid,
            run_id=run_id,
            mode="native",
            claude_sid=parent_sid,
            msg_id=asst_id,
        )
        _replay_and_apply(
            persist_sid=app_sid,
            run_id=run_id,
            mode="native",
            claude_sid=parent_sid,
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
    assert ok


def test_codex_replay_splits_reused_child_by_parent_tool_call() -> None:
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
        agent_path = "/root/reused"
        f.write(json.dumps({
            "type": "session_meta",
            "payload": {"source": {"subagent": {"thread_spawn": {
                "agent_path": agent_path,
            }}}},
        }).encode() + b"\n")
        f.write(json.dumps({
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "recipient": agent_path,
                "content": "child prompt",
            },
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
        assert len(worker_starts) == 2, f"worker_starts={worker_starts!r}"
        _replay_and_apply(
            persist_sid=app_sid,
            run_id=run_id,
            mode="native",
            claude_sid=parent_sid,
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
    assert ok


def test_codex_provider_child_setup_persists_source_and_starts_panel() -> None:
    async def _run() -> None:
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
            raise AssertionError("missing worker_start queue event")
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
        assert ok

    asyncio.run(_run())


def test_codex_provider_starts_child_panel_from_v2_activity() -> None:
    async def _run() -> None:
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
                "type": "event_msg",
                "payload": {
                    "type": "sub_agent_activity",
                    "event_id": "call_agent",
                    "agent_thread_id": child_sid,
                    "agent_path": "/root/reviewer",
                    "kind": "started",
                },
            }).encode() + b"\n")
            f.write(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_agent",
                    "output": json.dumps({"task_name": "/root/reviewer"}),
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
            saw_child_event = False
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if event.type == "worker_start":
                    saw_panel = True
                if event.type == "worker_event" and "child answer" in json.dumps(event.data):
                    saw_child_event = True
                if saw_panel and saw_child_event:
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
        source_key = f"call_agent_{child_sid}"
        source = rs.child_sources.get(source_key) or {}
        ok = (
            saw_panel
            and saw_child_event
            and len(rs.child_sources) == 1
            and source.get("parent_tool_use_id") == "call_agent"
            and source.get("agent_id") == child_sid
        )
        if not ok:
            print(f"  panel={saw_panel} child={saw_child_event} sources={rs.child_sources!r}")
        assert ok

    asyncio.run(_run())


def test_codex_provider_waits_for_child_terminal_before_complete() -> None:
    async def _run() -> None:
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

        class _Popen:
            pid = os.getpid()

            def poll(self):
                return None

        queue: asyncio.Queue = asyncio.Queue()
        rs = RunState(
            run_id=run_dir.name,
            run_dir=run_dir,
            popen=_Popen(),
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
            "insert_at": 1,
        }
        provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})
        provider._runs[run_dir.name] = rs
        await provider._ensure_child_tailer(
            rs,
            source_key,
            child_sid,
            rs.child_sources[source_key],
            {"type": "user"},
        )
        watch = asyncio.create_task(provider._watch_complete(rs))

        async def append_child_terminal() -> None:
            await asyncio.sleep(0.35)
            with child_path.open("ab") as f:
                f.write(json.dumps(_make_assistant_text_event("late child final")).encode() + b"\n")
                f.write(json.dumps(_make_task_complete_event()).encode() + b"\n")
                f.write(json.dumps(_make_task_complete_event()).encode() + b"\n")

        append_task = asyncio.create_task(append_child_terminal())
        (run_dir / "complete.json").write_text(json.dumps({
            "success": True,
            "session_id": "parent",
            "error": None,
            "token_usage": None,
        }), encoding="utf-8")
        events: list = []
        try:
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
                events.append(event)
                if event.type == "complete":
                    break
            await asyncio.wait_for(watch, timeout=3)
            await append_task
        finally:
            append_task.cancel()
            for tailer in rs.child_tailers.values():
                tailer.stop()
            await asyncio.gather(*(rs.child_tailer_tasks.values()), return_exceptions=True)
            provider._cleanup_run(run_dir.name)

        complete_index = next(
            (index for index, event in enumerate(events) if event.type == "complete"),
            -1,
        )
        worker_index = next(
            (
                index
                for index, event in enumerate(events)
                if event.type == "worker_event"
                and "late child final" in json.dumps(event.data)
            ),
            -1,
        )
        worker_complete_indexes = [
            index
            for index, event in enumerate(events)
            if event.type == "worker_complete"
            and event.data.get("delegation_id") == delegation_id
        ]
        ok = (
            worker_index >= 0
            and len(worker_complete_indexes) == 1
            and worker_complete_indexes[0] > worker_index
            and complete_index > worker_complete_indexes[0]
            and events[worker_complete_indexes[0]].data.get("success") is True
        )
        if not ok:
            print(f"  events={events!r}")
        assert ok

    asyncio.run(_run())


def test_codex_provider_reuses_processed_child_terminal_on_complete() -> None:
    async def _run() -> None:
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
            f.write(json.dumps(_make_assistant_text_event("processed child final")).encode() + b"\n")
            f.write(json.dumps(_make_task_complete_event()).encode() + b"\n")
            processed_byte = f.tell()

        class _Popen:
            pid = os.getpid()

            def poll(self):
                return None

        queue: asyncio.Queue = asyncio.Queue()
        rs = RunState(
            run_id=run_dir.name,
            run_dir=run_dir,
            popen=_Popen(),
            mode="native",
            app_session_id="app",
            queue=queue,
        )
        source_key = f"call_agent_{child_sid}"
        rs.child_sources[source_key] = {
            "agent_id": child_sid,
            "source_key": source_key,
            "parent_tool_use_id": "call_agent",
            "jsonl_path": str(child_path),
            "start_byte": start_byte,
            "processed_byte_offset": processed_byte,
            "delegation_id": f"codex_subagent_{source_key}",
            "insert_at": 1,
        }
        provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})
        provider._runs[run_dir.name] = rs
        await provider._ensure_child_tailer(
            rs,
            source_key,
            child_sid,
            rs.child_sources[source_key],
            {"type": "user"},
        )
        watch = asyncio.create_task(provider._watch_complete(rs))
        (run_dir / "complete.json").write_text(json.dumps({
            "success": True,
            "session_id": "parent",
            "error": None,
            "token_usage": None,
        }), encoding="utf-8")
        events = []
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1)
            events.append(event)
            while event.type != "complete":
                event = await asyncio.wait_for(queue.get(), timeout=1)
                events.append(event)
            await asyncio.wait_for(watch, timeout=1)
        finally:
            for tailer in rs.child_tailers.values():
                tailer.stop()
            await asyncio.gather(*(rs.child_tailer_tasks.values()), return_exceptions=True)
            provider._cleanup_run(run_dir.name)
        event_types = [event.type for event in events]
        child_completions = [
            event for event in events
            if event.type == "worker_complete"
            and event.data.get("delegation_id") == f"codex_subagent_{source_key}"
        ]
        ok = (
            len(child_completions) == 1
            and child_completions[0].data.get("success") is True
            and event_types.index("worker_start") < event_types.index("worker_complete")
            < event_types.index("complete")
        )
        assert ok, events

    asyncio.run(_run())


def test_codex_provider_parent_failure_does_not_wait_for_child_terminal() -> None:
    async def _run() -> None:
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

        class _Popen:
            pid = os.getpid()

            def poll(self):
                return None

        queue: asyncio.Queue = asyncio.Queue()
        rs = RunState(
            run_id=run_dir.name,
            run_dir=run_dir,
            popen=_Popen(),
            mode="native",
            app_session_id="app",
            queue=queue,
        )
        source_key = f"call_agent_{child_sid}"
        rs.child_sources[source_key] = {
            "agent_id": child_sid,
            "source_key": source_key,
            "parent_tool_use_id": "call_agent",
            "jsonl_path": str(child_path),
            "start_byte": start_byte,
            "processed_byte_offset": start_byte,
            "delegation_id": f"codex_subagent_{source_key}",
            "insert_at": 1,
        }
        provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})
        provider._runs[run_dir.name] = rs
        await provider._ensure_child_tailer(
            rs,
            source_key,
            child_sid,
            rs.child_sources[source_key],
            {"type": "user"},
        )
        watch = asyncio.create_task(provider._watch_complete(rs))
        (run_dir / "complete.json").write_text(json.dumps({
            "success": False,
            "session_id": "parent",
            "error": "parent failed",
            "token_usage": None,
        }), encoding="utf-8")
        events = []
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1)
            events.append(event)
            while event.type != "complete":
                event = await asyncio.wait_for(queue.get(), timeout=1)
                events.append(event)
            await asyncio.wait_for(watch, timeout=1)
        finally:
            for tailer in rs.child_tailers.values():
                tailer.stop()
            await asyncio.gather(*(rs.child_tailer_tasks.values()), return_exceptions=True)
            provider._cleanup_run(run_dir.name)
        child_completions = [
            queued for queued in events
            if queued.type == "worker_complete"
            and queued.data.get("delegation_id") == f"codex_subagent_{source_key}"
        ]
        ok = (
            event.data.get("success") is False
            and len(child_completions) == 1
            and child_completions[0].data.get("success") is False
            and events.index(child_completions[0]) < events.index(event)
        )
        assert ok, events

    asyncio.run(_run())


def test_codex_provider_cancel_unblocks_child_join() -> None:
    async def _run() -> None:
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

        class _Popen:
            pid = os.getpid()

            def poll(self):
                return 0

        queue: asyncio.Queue = asyncio.Queue()
        rs = RunState(
            run_id=run_dir.name,
            run_dir=run_dir,
            popen=_Popen(),
            mode="native",
            app_session_id="app",
            queue=queue,
        )
        source_key = f"call_agent_{child_sid}"
        rs.child_sources[source_key] = {
            "agent_id": child_sid,
            "source_key": source_key,
            "parent_tool_use_id": "call_agent",
            "jsonl_path": str(child_path),
            "start_byte": start_byte,
            "processed_byte_offset": start_byte,
            "delegation_id": f"codex_subagent_{source_key}",
            "insert_at": 1,
        }
        provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})
        provider._runs[run_dir.name] = rs
        await provider._ensure_child_tailer(
            rs,
            source_key,
            child_sid,
            rs.child_sources[source_key],
            {"type": "user"},
        )
        watch = asyncio.create_task(provider._watch_complete(rs))
        (run_dir / "complete.json").write_text(json.dumps({
            "success": True,
            "session_id": "parent",
            "error": None,
            "token_usage": None,
        }), encoding="utf-8")

        async def cancel_later() -> None:
            await asyncio.sleep(0.35)
            provider.cancel_run(run_dir.name)

        cancel_task = asyncio.create_task(cancel_later())
        try:
            event = await asyncio.wait_for(queue.get(), timeout=2)
            while event.type != "complete":
                event = await asyncio.wait_for(queue.get(), timeout=2)
            await asyncio.wait_for(watch, timeout=2)
            await cancel_task
        finally:
            cancel_task.cancel()
            for tailer in rs.child_tailers.values():
                tailer.stop()
            await asyncio.gather(*(rs.child_tailer_tasks.values()), return_exceptions=True)
            provider._cleanup_run(run_dir.name)
        assert event.data.get("success") is False, (
            f"expected cancelled failure, got {event.data!r}"
        )
        assert event.data.get("error") == "cancelled", (
            f"expected cancelled error, got {event.data!r}"
        )

    asyncio.run(_run())


def test_codex_event_msg_agent_reasoning_renders_as_thinking() -> None:
    # Real Codex rollouts carry the reasoning body under `text`
    # (`delta` while streaming); an unmatched field falls through to the
    # raw native JSON dump instead of a thinking block.
    cases = [
        ({"type": "agent_reasoning", "text": "Need inspect before editing."},
         "Need inspect before editing."),
        ({"type": "agent_reasoning_delta", "delta": "Need inspect"}, "Need inspect"),
    ]
    for payload, expected in cases:
        normalizer = CodexRolloutNormalizer(namespace="test")
        events = normalizer.normalize_event({"type": "event_msg", "payload": payload})
        assert len(events) == 1, f"expected one event for {payload!r}, got {events!r}"
        content = ((events[0].get("message") or {}).get("content") or [])
        block = content[0] if content and isinstance(content[0], dict) else {}
        assert (
            events[0].get("type") == "assistant"
            and block.get("type") == "thinking"
            and block.get("thinking") == expected
            and "text" not in block
        ), f"bad reasoning event for {payload!r}: {events!r}"
    empty = CodexRolloutNormalizer(namespace="test").normalize_event({
        "type": "event_msg",
        "payload": {"type": "agent_reasoning", "text": ""},
    })
    assert not empty, f"empty reasoning should render nothing, got {empty!r}"


def test_codex_reasoning_streamed_and_finalized_render_once() -> None:
    # With reasoning summaries enabled Codex emits the same body twice:
    # streamed as event_msg.agent_reasoning, then re-emitted verbatim as
    # response_item.reasoning's summary. Only the streamed copy renders.
    body = "**Waiting for requirements**\n\nI need to wait for the requirements."
    normalizer = CodexRolloutNormalizer(namespace="test")
    rows = normalizer.normalize_event({
        "type": "event_msg",
        "payload": {"type": "agent_reasoning", "text": body},
    })
    rows += normalizer.normalize_event({
        "type": "response_item",
        "payload": {"type": "reasoning", "id": "rs_1", "summary": [{"text": body}]},
    })
    thinking = [
        block.get("thinking")
        for row in rows
        for block in ((row.get("message") or {}).get("content") or [])
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]
    assert thinking == [body], (
        f"expected one thinking card, got {thinking!r} from {rows!r}"
    )
    # A different body in the same turn still renders.
    other = normalizer.normalize_event({
        "type": "event_msg",
        "payload": {"type": "agent_reasoning", "text": "**Second thought**"},
    })
    assert len(other) == 1, f"distinct reasoning should render, got {other!r}"
    # The claim is per turn: the next turn re-renders identical text.
    normalizer.normalize_event({"type": "turn_context", "payload": {}})
    again = normalizer.normalize_event({
        "type": "event_msg",
        "payload": {"type": "agent_reasoning", "text": body},
    })
    assert len(again) == 1, f"reasoning should re-render in a new turn, got {again!r}"


def test_codex_nonlatest_replay_bound_is_safe() -> bool:
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    first_dir = runs_root() / first_id
    second_dir = runs_root() / second_id
    try:
        first_dir.mkdir(parents=True)
        second_dir.mkdir(parents=True)
        rollout = first_dir / "shared.jsonl"
        first_line = json.dumps(_make_assistant_text_event("first")) + "\n"
        second_line = json.dumps(_make_assistant_text_event("second")) + "\n"
        rollout.write_text(first_line + second_line, encoding="utf-8")
        boundary = len(first_line.encode())
        for run_dir, start in ((first_dir, 0), (second_dir, boundary)):
            (run_dir / "state.json").write_text(json.dumps({
                "jsonl_path": str(rollout),
                "pre_query_byte_offset": start,
            }), encoding="utf-8")
        first = {"run_id": first_id, "provider_kind": "codex"}
        second = {"run_id": second_id, "provider_kind": "codex"}
        if _codex_replay_bound(first, second) != boundary:
            return False
        (second_dir / "state.json").write_text(json.dumps({
            "jsonl_path": str(rollout),
            "pre_query_byte_offset": str(boundary),
        }), encoding="utf-8")
        if _codex_replay_bound(first, second) is not None:
            return False
        (second_dir / "state.json").write_text("[]", encoding="utf-8")
        if _codex_replay_bound(first, second) is not None:
            return False
        other = second_dir / "other.jsonl"
        other.write_text(second_line, encoding="utf-8")
        (second_dir / "state.json").write_text(json.dumps({
            "jsonl_path": str(other),
            "pre_query_byte_offset": 0,
        }), encoding="utf-8")
        return _codex_replay_bound(first, second) is None
    finally:
        shutil.rmtree(first_dir, ignore_errors=True)
        shutil.rmtree(second_dir, ignore_errors=True)


def test_codex_provider_recovers_nested_child_sources_from_processed_history() -> None:
    async def _run() -> None:
        child_sid = str(uuid.uuid4())
        grandchild_sid = str(uuid.uuid4())
        run_dir = runs_root() / str(uuid.uuid4())
        run_dir.mkdir(parents=True, exist_ok=True)
        child_path = run_dir / "child-rollout.jsonl"
        grandchild_path = run_dir / "grandchild-rollout.jsonl"
        with child_path.open("wb") as f:
            f.write(json.dumps({
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "child prompt"},
            }).encode() + b"\n")
            child_start = f.tell()
            f.write(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "call_grandchild",
                    "arguments": json.dumps({"message": "nested review"}),
                },
            }).encode() + b"\n")
            f.write(json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "sub_agent_activity",
                    "event_id": "call_grandchild",
                    "agent_thread_id": grandchild_sid,
                    "agent_path": "/root/child/grandchild",
                    "kind": "started",
                },
            }).encode() + b"\n")
            f.write(json.dumps(_make_task_complete_event()).encode() + b"\n")
        with grandchild_path.open("wb") as f:
            f.write(json.dumps({
                "type": "session_meta",
                "payload": {"source": {"subagent": {"thread_spawn": {
                    "agent_path": "/root/child/grandchild",
                }}}},
            }).encode() + b"\n")
            f.write(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "recipient": "/root/child/grandchild",
                    "content": "nested prompt",
                },
            }).encode() + b"\n")
            grandchild_start = f.tell()
            f.write(json.dumps(_make_assistant_text_event("nested answer")).encode() + b"\n")
            f.write(json.dumps(_make_task_complete_event()).encode() + b"\n")

        queue: asyncio.Queue = asyncio.Queue()
        rs = RunState(
            run_id=run_dir.name,
            run_dir=run_dir,
            popen=SimpleNamespace(pid=os.getpid()),
            mode="native",
            app_session_id="app",
            queue=queue,
        )
        source_key = f"call_child_{child_sid}"
        rs.child_sources[source_key] = {
            "agent_id": child_sid,
            "source_key": source_key,
            "parent_tool_use_id": "call_child",
            "jsonl_path": str(child_path),
            "start_byte": child_start,
            "processed_byte_offset": child_path.stat().st_size,
            "delegation_id": f"codex_subagent_{source_key}",
            "insert_at": 1,
        }
        provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})
        import codex_native
        original_resolve = codex_native.resolve_rollout_path_polled

        async def fake_resolve(thread_id: str, **_kwargs):
            if thread_id == grandchild_sid:
                return grandchild_path
            return child_path if thread_id == child_sid else None

        codex_native.resolve_rollout_path_polled = fake_resolve
        try:
            await provider._ensure_child_tailer(
                rs,
                source_key,
                child_sid,
                rs.child_sources[source_key],
                None,
            )
            await provider._wait_child_setup(rs)
            for tailer in rs.child_tailers.values():
                await tailer.drain_available()
            queued = []
            while not queue.empty():
                queued.append(queue.get_nowait())
        finally:
            codex_native.resolve_rollout_path_polled = original_resolve
            for tailer in rs.child_tailers.values():
                tailer.stop()
            for task in rs.child_tailer_tasks.values():
                task.cancel()
            await asyncio.gather(*rs.child_tailer_tasks.values(), return_exceptions=True)

        nested_sources = [
            source for source in rs.child_sources.values()
            if source.get("agent_id") == grandchild_sid
        ]
        nested_text = json.dumps([event.data for event in queued])
        completion_by_delegation = {
            event.data.get("delegation_id"): event.data.get("success")
            for event in queued
            if event.type == "worker_complete"
        }
        ok = (
            len(nested_sources) == 1
            and nested_sources[0].get("parent_source_key") == source_key
            and nested_sources[0].get("start_byte") == grandchild_start
            and "nested answer" in nested_text
            and completion_by_delegation.get(f"codex_subagent_{source_key}") is True
            and completion_by_delegation.get(
                nested_sources[0].get("delegation_id")
            ) is True
        )
        if not ok:
            print(f"  sources={rs.child_sources!r} queued={nested_text[:500]}")
        assert ok

    asyncio.run(_run())


TESTS = [
    ("codex nonlatest replay bound is safe", test_codex_nonlatest_replay_bound_is_safe),
    ("codex live orphan is emitted (not skipped)", test_live_orphan_is_emitted_not_skipped),
    ("codex replay reads native rollout jsonl", test_codex_replay_reads_native_rollout_jsonl),
    ("codex live recovery streams rollout events before complete", test_live_recovery_streams_rollout_events_before_complete),
    ("codex live recovery waits for child setup before complete", test_live_recovery_waits_for_child_setup_before_complete),
    ("dead codex wrapper uses rollout terminal complete", test_dead_wrapper_uses_rollout_terminal_complete),
    ("terminal family-delegated codex run recovers without authority error", test_terminal_family_delegated_run_recovers_without_authority_error),
    ("dead codex wrapper resolves missing jsonl path", test_dead_wrapper_resolves_missing_jsonl_path),
    ("dead codex wrapper ignores malformed usage values", test_dead_wrapper_ignores_malformed_usage_values),
    ("codex usage normalizer zeros malformed live values", test_codex_usage_normalizer_zeros_malformed_live_values),
    ("dead codex wrapper uses rollout terminal failure", test_dead_wrapper_uses_rollout_terminal_failure),
    ("dead codex wrapper without terminal fails closed", test_dead_wrapper_without_terminal_still_fails_closed),
    ("codex complete emit recovers missing complete from rollout", test_emit_complete_recovers_missing_complete_from_rollout),
    ("codex ambient cancel preserves recoverable app-server", test_codex_ambient_cancel_preserves_recoverable_app_server),
    ("codex explicit cancel still stops app-server", test_codex_explicit_cancel_still_stops_app_server),
    ("codex fallback rollout completion settles app-server", test_codex_fallback_rollout_completion_settles_app_server),
    ("codex pre-thread ambient cancel cleans unrecoverable app-server", test_codex_pre_thread_ambient_cancel_cleans_unrecoverable_app_server),
    ("codex loopback POST retries transient reset", test_loopback_post_retries_transient_reset),
    ("codex loopback POST rejects ambient token after forbidden", test_loopback_post_does_not_reread_ambient_token_after_forbidden),
    ("codex loopback POST surfaces HTTP error detail", test_loopback_post_surfaces_http_error_detail),
    ("provider bootstrap task schedules from worker thread", test_schedule_loop_task_from_worker_thread),
    ("provider bootstrap schedule does not block under loop lag", test_schedule_loop_task_no_block_under_loop_lag),
    ("codex MCP string error normalizes", test_codex_mcp_string_error_normalizes),
    ("codex dead-runner replay preserves tool result structure", test_codex_dead_runner_replay_preserves_tool_result_structure),
    ("codex replay dedup allows mutated same uuid", test_codex_replay_dedup_allows_mutated_same_uuid),
    ("turn manager dead runner replays codex rollout events", test_turn_manager_dead_runner_replays_codex_rollout_events),
    ("codex replay includes child subagent panel events", test_codex_replay_includes_child_subagent_panel_events),
    ("codex replay derives missing child sources from v2 activity", test_codex_replay_derives_missing_child_sources_from_v2_activity),
    ("codex replay splits reused child by parent tool call", test_codex_replay_splits_reused_child_by_parent_tool_call),
    ("codex provider child setup persists source and starts panel", test_codex_provider_child_setup_persists_source_and_starts_panel),
    ("codex provider starts child panel from v2 activity", test_codex_provider_starts_child_panel_from_v2_activity),
    ("codex provider waits for child terminal before complete", test_codex_provider_waits_for_child_terminal_before_complete),
    ("codex provider reuses processed child terminal on complete", test_codex_provider_reuses_processed_child_terminal_on_complete),
    ("codex provider parent failure does not wait for child terminal", test_codex_provider_parent_failure_does_not_wait_for_child_terminal),
    ("codex provider cancel unblocks child join", test_codex_provider_cancel_unblocks_child_join),
    ("codex provider recovers nested child sources from processed history", test_codex_provider_recovers_nested_child_sources_from_processed_history),
    ("codex event_msg.agent_reasoning renders as thinking", test_codex_event_msg_agent_reasoning_renders_as_thinking),
    ("codex streamed and finalized reasoning render once", test_codex_reasoning_streamed_and_finalized_render_once),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                fn()
            except Exception as e:
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
                print(f"{FAIL}  {name}")
                continue
            print(f"{PASS}  {name}")
    finally:
        session_manager.flush_pending_persists()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    print(f"all {len(TESTS)} tests passed" if not failed
          else f"{failed} of {len(TESTS)} test(s) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
