from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-file-edit-empty-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import file_editor  # noqa: E402
import synthetic_messages  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


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


def test_empty_session_then_file_join_same_session() -> bool:
    d = _project("join")
    empty = asyncio.run(file_editor.start_empty(cwd=str(d), persistent=True))
    target = _write(d, "target.txt", "hello\n")
    joined = asyncio.run(file_editor.start(str(target), cwd=str(d)))
    if joined["session_id"] != empty["session_id"]:
        print("  expected selected file to join the empty cwd session")
        return False
    resolved = str(target.resolve())
    if joined["file_paths"] != [resolved]:
        print(f"  expected file_paths=[{resolved!r}], got {joined['file_paths']!r}")
        return False
    if joined["original_contents"].get(resolved) != "hello\n":
        print(f"  missing baseline: {joined['original_contents']!r}")
        return False
    if resolved not in (joined.get("meta_prompt") or ""):
        print(f"  add-file prompt should mention {resolved}")
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
    if msg.get("source") != "file_editor":
        print(f"  wrong source: {msg.get('source')!r}")
        return False
    return True


def test_empty_session_resume_no_duplicate_prompt() -> bool:
    d = _project("resume")
    first = asyncio.run(file_editor.start_empty(cwd=str(d), persistent=True))
    second = asyncio.run(file_editor.start_empty(cwd=str(d), persistent=True))
    if second["session_id"] != first["session_id"]:
        print("  expected empty start to resume same cwd session")
        return False
    if second.get("meta_prompt") is not None:
        print(f"  resume should not inject a model prompt, got {second.get('meta_prompt')!r}")
        return False
    if second.get("user_ask") is not None:
        print(f"  resume should not inject another user ask, got {second.get('user_ask')!r}")
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
        test_empty_session_then_file_join_same_session,
        test_empty_ask_appends_visible_assistant_message,
        test_empty_session_resume_no_duplicate_prompt,
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
