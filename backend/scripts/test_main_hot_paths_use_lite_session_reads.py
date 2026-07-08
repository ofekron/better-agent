"""Regression lock for session read hot paths in main.py.

Run with:
    cd backend && .venv/bin/python scripts/test_main_hot_paths_use_lite_session_reads.py
"""

from __future__ import annotations

import ast
import os
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
MAIN = BACKEND / "main.py"

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _functions_by_name(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _session_manager_calls(node: ast.AST, method: str) -> list[int]:
    lines: list[int] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != method:
            continue
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id == "session_manager":
            lines.append(call.lineno)
    return lines


def _offloads_session_manager_call(node: ast.AST, method: str) -> bool:
    for await_node in ast.walk(node):
        if not isinstance(await_node, ast.Await):
            continue
        call = await_node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "to_thread"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        ):
            continue
        if not call.args:
            continue
        target = call.args[0]
        if not (
            isinstance(target, ast.Attribute)
            and target.attr == method
            and isinstance(target.value, ast.Name)
            and target.value.id == "session_manager"
        ):
            continue
        return True
    return False


def _run() -> bool:
    tree = ast.parse(MAIN.read_text(), filename=os.fspath(MAIN))
    funcs = _functions_by_name(tree)
    results: list[tuple[str, bool, str]] = []

    no_full_get = [
        "_delete_session_tree",
        "internal_supervisor_separate",
        "update_session_selectors",
        "project_suggestion",
        "rewind_and_retry",
        "_rewind_latest_user_for_alter",
        "internal_prompt_engineering_comment",
        "internal_prompt_engineering_result",
        "start_file_editor",
        "add_file_editor_comment",
        "_re_enqueue_queued_prompts",
        "internal_create_session",
        "internal_create_sub_session",
        "internal_force_context_overflow",
        "internal_open_file_panel",
        "internal_open_config_panel",
        "internal_schedules",
        "get_session_background",
        "kill_session_background",
        "_find_worker_by_session_name",
        "internal_register_existing_session_as_worker",
        "internal_auto_tagging",
        "_latest_message_text",
    ]
    for name in no_full_get:
        node = funcs.get(name)
        lines = _session_manager_calls(node, "get") if node else []
        results.append((
            f"{name} avoids session_manager.get",
            node is not None and not lines,
            f"lines={lines}" if node else "missing",
        ))

    # _latest_message_text must not fetch a session at all — it reads from a
    # session dict the caller already fetched off-loop. A get() here blocked
    # the event loop ~1.8s on large sessions (auto-tagging current-task).
    latest = funcs.get("_latest_message_text")
    latest_get = _session_manager_calls(latest, "get") if latest else []
    latest_lite = _session_manager_calls(latest, "get_lite") if latest else []
    results.append((
        "_latest_message_text makes no session_manager fetch",
        bool(latest and not latest_get and not latest_lite),
        f"get={latest_get} get_lite={latest_lite}" if latest else "missing",
    ))

    # internal_auto_tagging must off-load its session fetch off-loop via
    # get_lite (events not needed; cwd/eligible/messages don't read events).
    auto_tag = funcs.get("internal_auto_tagging")
    results.append((
        "internal_auto_tagging offloads session_manager.get_lite off-loop",
        bool(auto_tag and _offloads_session_manager_call(auto_tag, "get_lite")),
        "missing await asyncio.to_thread(session_manager.get_lite, ...)",
    ))

    separate = funcs.get("internal_supervisor_separate")
    results.append((
        "internal_supervisor_separate uses exists for missing-session guard",
        bool(separate and _session_manager_calls(separate, "exists")),
        "missing exists call",
    ))

    get_session = funcs.get("get_session")
    results.append((
        "get_session offloads root-id resolution",
        bool(get_session and _offloads_session_manager_call(get_session, "_root_id_for")),
        "missing await asyncio.to_thread(session_manager._root_id_for, ...)",
    ))

    lite_required = [
        "_delete_session_tree",
        "update_session_selectors",
        "project_suggestion",
        "rewind_and_retry",
        "_rewind_latest_user_for_alter",
        "internal_prompt_engineering_comment",
        "internal_prompt_engineering_result",
        "start_file_editor",
        "add_file_editor_comment",
        "_re_enqueue_queued_prompts",
        "internal_create_session",
        "internal_create_sub_session",
        "internal_force_context_overflow",
        "internal_open_file_panel",
        "internal_open_config_panel",
        "_find_worker_by_session_name",
        "internal_register_existing_session_as_worker",
    ]
    for name in lite_required:
        node = funcs.get(name)
        results.append((
            f"{name} uses get_lite",
            bool(node and _session_manager_calls(node, "get_lite")),
            "missing get_lite call" if node else "missing",
        ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' - ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    return 0 if _run() else 1


if __name__ == "__main__":
    raise SystemExit(main())
