from __future__ import annotations

import asyncio
import os
import shutil
import time
import threading
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-file-edit-empty-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import file_editor  # noqa: E402
import main as main_api  # noqa: E402
import render_stub  # noqa: E402
import synthetic_messages  # noqa: E402
import working_mode  # noqa: E402
from event_journal import event_journal_reader  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


_FAKE_BASES: dict[tuple[str, str, str, str], str] = {}
_FAKE_BASES_LOCK = threading.Lock()


async def _fake_ensure_file_edit_base(cfg):
    key = (cfg.cwd, cfg.provider_id, cfg.model, cfg.node_id)
    with _FAKE_BASES_LOCK:
        sid = _FAKE_BASES.get(key)
        if sid and session_manager.get(sid):
            return sid
        base = session_manager.create(
            name="file-editing-base",
            model=cfg.model,
            cwd=cfg.cwd,
            orchestration_mode="native",
            source="internal",
            provider_id=cfg.provider_id,
            reasoning_effort=cfg.reasoning_effort or None,
            node_id=cfg.node_id,
            bare_config=False,
            worker_creation_policy="deny",
        )
        fake_agent_sid = f"fake-empty-base-sid-{len(_FAKE_BASES)}"
        session_manager._run(
            base["id"],
            lambda s: s.__setitem__("agent_session_id", fake_agent_sid),
            {"kind": "test_agent_sid_set"},
        )
        working_mode.mark_working_mode(
            base["id"],
            mode=file_editor.BASE_MODE,
            meta={
                "cwd": cfg.cwd,
                "provider_id": cfg.provider_id,
                "model": cfg.model,
                "machine_completion": False,
                "version": file_editor.FILE_EDIT_BASE_SPEC.version,
                "node_id": cfg.node_id,
                "provisioned_at": time.time(),
            },
        )
        _FAKE_BASES[key] = base["id"]
        return base["id"]


file_editor._ensure_file_edit_base = _fake_ensure_file_edit_base  # type: ignore[assignment]

_FAKE_PROVIDER = {
    "id": "test-provider",
    "name": "Test Provider",
    "default_model": "test-model",
    "supports_reasoning_effort": False,
}


def _fake_provider_record(provider_id: str | None = None) -> dict:
    return dict(_FAKE_PROVIDER)


file_editor._provider_record = _fake_provider_record  # type: ignore[assignment]
file_editor._require_fork_support = lambda provider_id: None  # type: ignore[assignment]
main_api._provider_for_required_model = lambda provider_id: dict(_FAKE_PROVIDER)  # type: ignore[assignment]
main_api._provider_reasoning_effort = lambda provider_id, requested: ""  # type: ignore[assignment]
main_api._provider_permission = lambda provider_id, requested: requested or {}  # type: ignore[assignment]


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _project(label: str) -> Path:
    d = Path(_TMP_HOME) / "proj" / label
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(d: Path, name: str, content: str) -> Path:
    p = d / name
    p.write_text(content)
    return p


def test_empty_session_has_no_required_file() -> bool:
    d = _project("empty")
    result = asyncio.run(file_editor.start_empty(cwd=str(d), persistent=True))
    session = session_manager.get(result["session_id"]) or {}
    meta = session.get("working_mode_meta") or {}
    if meta.get("project_cwd") != str(d.resolve()):
        print(f"  wrong project_cwd: {meta.get('project_cwd')!r}")
        return False
    if meta.get("file_paths") != []:
        print(f"  expected empty file_paths, got {meta.get('file_paths')!r}")
        return False
    if meta.get("original_contents") != {}:
        print(f"  expected empty original_contents, got {meta.get('original_contents')!r}")
        return False
    if meta.get("persistent") is not True:
        print(f"  expected persistent=True, got {meta.get('persistent')!r}")
        return False
    if result.get("meta_prompt") is not None:
        print(f"  empty session should not send a model prompt, got {result.get('meta_prompt')!r}")
        return False
    ask = result.get("user_ask") or ""
    if "Which file or files do you want to edit?" not in ask:
        print(f"  missing user-facing ask: {ask!r}")
        return False
    return True


def test_empty_session_then_file_creates_new_session_same_base() -> bool:
    d = _project("join")
    empty = asyncio.run(file_editor.start_empty(cwd=str(d), persistent=True))
    target = _write(d, "target.txt", "hello\n")
    joined = asyncio.run(file_editor.start(str(target), cwd=str(d)))
    if joined["session_id"] == empty["session_id"]:
        print("  selected file should create a fresh session, not join the empty one")
        return False
    resolved = str(target.resolve())
    if joined["file_paths"] != [resolved]:
        print(f"  expected file_paths=[{resolved!r}], got {joined['file_paths']!r}")
        return False
    if joined["original_contents"].get(resolved) != "hello\n":
        print(f"  missing baseline: {joined['original_contents']!r}")
        return False
    if resolved not in (joined.get("meta_prompt") or ""):
        print(f"  bootstrap prompt should mention {resolved}")
        return False
    empty_meta = (session_manager.get(empty["session_id"]) or {}).get("working_mode_meta") or {}
    joined_meta = (session_manager.get(joined["session_id"]) or {}).get("working_mode_meta") or {}
    if empty_meta.get("base_session_id") != joined_meta.get("base_session_id"):
        print("  same cwd/model/provider should reuse the warm base")
        return False
    return True


def test_empty_ask_appends_visible_assistant_message() -> bool:
    d = _project("visible_ask")
    result = asyncio.run(file_editor.start_empty(cwd=str(d), persistent=True))
    ask = result.get("user_ask") or ""
    asyncio.run(
        synthetic_messages.append_assistant_message(
            result["session_id"],
            content=ask,
            source="file_editor",
        )
    )
    session = session_manager.get(result["session_id"]) or {}
    messages = session.get("messages") or []
    if session.get("queued_prompts"):
        print(f"  user-facing ask should not create queued prompts: {session.get('queued_prompts')!r}")
        return False
    if len(messages) != 1:
        print(f"  expected exactly one visible message, got {len(messages)}")
        return False
    msg = messages[0]
    if msg.get("role") != "assistant":
        print(f"  expected assistant message, got {msg.get('role')!r}")
        return False
    if msg.get("content") != ask:
        print(f"  wrong message content: {msg.get('content')!r}")
        return False
    if render_stub.message_output_text(msg) != ask:
        print("  ask must be derivable from render events")
        return False
    if msg.get("source") != "file_editor":
        print(f"  wrong source: {msg.get('source')!r}")
        return False
    return True


def test_create_session_returns_visible_empty_ask() -> bool:
    d = _project("create_session")
    session = asyncio.run(
        main_api.create_session(
            {
                "name": "",
                "model": "test-model",
                "cwd": str(d),
                "orchestration_mode": "native",
                "provider_id": "test-provider",
                "file_edit_enabled": True,
                "node_id": "primary",
            }
        )
    )
    messages = session.get("messages") or []
    if len(messages) != 1:
        print(f"  expected returned session to include one ask message, got {len(messages)}")
        return False
    msg = messages[0]
    if msg.get("role") != "assistant":
        print(f"  expected assistant message, got {msg.get('role')!r}")
        return False
    if msg.get("source") != "file_editor":
        print(f"  wrong source: {msg.get('source')!r}")
        return False
    if "Which file or files do you want to edit?" not in (msg.get("content") or ""):
        print(f"  missing ask content: {msg.get('content')!r}")
        return False
    journal_events = event_journal_reader.read_frontend_events(
        session["id"],
        message_id=msg["id"],
    )
    if "Which file or files do you want to edit?" not in render_stub.message_output_text({"events": journal_events}):
        print("  ask must be written to the event journal")
        return False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    cold = session_manager.get(session["id"]) or {}
    cold_msg = ((cold.get("messages") or []) or [{}])[0]
    if "Which file or files do you want to edit?" not in (cold_msg.get("content") or ""):
        print(f"  cold reload lost ask content: {cold_msg.get('content')!r}")
        return False
    return True


def test_empty_session_reopen_creates_fresh_prompt() -> bool:
    d = _project("resume")
    first = asyncio.run(file_editor.start_empty(cwd=str(d), persistent=True))
    second = asyncio.run(file_editor.start_empty(cwd=str(d), persistent=True))
    if second["session_id"] == first["session_id"]:
        print("  expected empty start to create a fresh session")
        return False
    if second.get("meta_prompt") is not None:
        print(f"  empty session should not inject a model prompt, got {second.get('meta_prompt')!r}")
        return False
    if second.get("user_ask") is None:
        print("  fresh empty session should show the user ask")
        return False
    meta1 = (session_manager.get(first["session_id"]) or {}).get("working_mode_meta") or {}
    meta2 = (session_manager.get(second["session_id"]) or {}).get("working_mode_meta") or {}
    if meta1.get("base_session_id") != meta2.get("base_session_id"):
        print("  empty sessions for same cwd should share the warm base")
        return False
    return True


def test_empty_session_requires_cwd() -> bool:
    try:
        asyncio.run(file_editor.start_empty(cwd=""))
    except ValueError:
        return True
    print("  missing cwd should raise ValueError")
    return False


def main() -> int:
    tests = [
        test_empty_session_has_no_required_file,
        test_empty_session_then_file_creates_new_session_same_base,
        test_empty_ask_appends_visible_assistant_message,
        test_create_session_returns_visible_empty_ask,
        test_empty_session_reopen_creates_fresh_prompt,
        test_empty_session_requires_cwd,
    ]
    failures = 0
    try:
        for test in tests:
            ok = test()
            print(f"{PASS if ok else FAIL} {test.__name__}")
            failures += 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
