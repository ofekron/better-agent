from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("ba-test-codex-agent-tree-")

import codex_agent_tree as tree  # noqa: E402
from codex_agent_tree import (  # noqa: E402
    _process_returncode,
    _read_complete_rows,
    _turn_started,
    resolve_rollout_path_for_join,
    wait_for_agent_tree_terminal,
)


def _event(payload: dict) -> str:
    return json.dumps({"type": "event_msg", "payload": payload}) + "\n"


def _sub_agent_event(child_id: str) -> str:
    return json.dumps({
        "type": "event_msg",
        "payload": {
            "type": "sub_agent_activity",
            "event_id": "spawn",
            "agent_thread_id": child_id,
            "agent_path": f"/root/{child_id}",
            "kind": "started",
        },
    }) + "\n"


# --- _process_returncode ---------------------------------------------------


def test_process_returncode_prefers_proc_returncode() -> None:
    class Inner:
        returncode = 7

    class Proc:
        _proc = Inner()
        returncode = 9

    assert _process_returncode(Proc()) == 7


def test_process_returncode_falls_back_to_top_returncode() -> None:
    class Proc:
        returncode = 3

    assert _process_returncode(Proc()) == 3


def test_process_returncode_none_when_absent() -> None:
    class Proc:
        pass

    assert _process_returncode(Proc()) is None


# --- _read_complete_rows ---------------------------------------------------


def test_read_complete_rows_returns_full_lines(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b'{"a":1}\n{"b":2}\npartial')
    rows, cursor = _read_complete_rows(path, 0)
    assert rows == [b'{"a":1}\n', b'{"b":2}\n']
    assert cursor == len(b'{"a":1}\n{"b":2}\n')


def test_read_complete_rows_respects_start_byte(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    body = b'{"a":1}\n{"b":2}\n'
    path.write_bytes(body)
    rows, cursor = _read_complete_rows(path, len(b'{"a":1}\n'))
    assert rows == [b'{"b":2}\n']
    assert cursor == len(body)


def test_read_complete_rows_oserror_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "nope.jsonl"
    rows, cursor = _read_complete_rows(missing, 0)
    assert rows == []
    assert cursor == 0


# --- _turn_started ---------------------------------------------------------


def test_turn_started_top_level_type() -> None:
    assert _turn_started({"type": "task_started"}) is True


def test_turn_started_event_msg_payload() -> None:
    assert _turn_started({"type": "event_msg", "payload": {"type": "task_started"}}) is True


def test_turn_started_negative_cases() -> None:
    assert _turn_started({"type": "event_msg", "payload": {"type": "task_complete"}}) is False
    assert _turn_started({"type": "event_msg", "payload": "str"}) is False
    assert _turn_started({"type": "other"}) is False


# --- resolve_rollout_path_for_join -----------------------------------------


def test_resolve_rollout_path_for_join_returns_path_once_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel = tmp_path / "cancel"
    target = tmp_path / "rollout.jsonl"
    attempts = {"n": 0}

    def resolve(thread_id: str) -> Path | None:
        attempts["n"] += 1
        return target if attempts["n"] >= 2 else None

    monkeypatch.setattr(tree, "resolve_rollout_path", resolve)

    class Proc:
        returncode = None

    path = asyncio.run(
        resolve_rollout_path_for_join("tid", cancel_path=cancel, proc=Proc())
    )
    assert path == target
    assert attempts["n"] == 2


def test_resolve_rollout_path_for_join_raises_on_proc_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tree, "resolve_rollout_path", lambda tid: None)
    cancel = tmp_path / "cancel"

    class Proc:
        returncode = 0

    with pytest.raises(RuntimeError, match="exited before rollout"):
        asyncio.run(
            resolve_rollout_path_for_join("tid", cancel_path=cancel, proc=Proc())
        )


def test_resolve_rollout_path_for_join_cancel_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tree, "resolve_rollout_path", lambda tid: None)
    cancel = tmp_path / "cancel"
    cancel.touch()

    class Proc:
        returncode = None

    async def run() -> bool:
        try:
            await resolve_rollout_path_for_join("tid", cancel_path=cancel, proc=Proc())
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(run()) is True


# --- wait_for_agent_tree_terminal ------------------------------------------


def test_wait_terminal_skips_malformed_and_nondict_rows(tmp_path: Path) -> None:
    # Root-only completion: rows that fail to parse or aren't dicts must be
    # skipped without spawning children or aborting the wait.
    root = tmp_path / "root.jsonl"
    root.write_bytes(b"not valid json\n[1, 2, 3]\n\"a string\"\n")
    asyncio.run(wait_for_agent_tree_terminal(root, start_byte=0))


def test_wait_terminal_cancel_mid_wait(tmp_path: Path) -> None:
    root = tmp_path / "root.jsonl"
    root.write_text(_sub_agent_event("c1"), encoding="utf-8")
    cancel = tmp_path / "cancel"

    async def driver() -> bool:
        task = asyncio.create_task(
            wait_for_agent_tree_terminal(
                root,
                start_byte=0,
                resolve_path=lambda tid: None,  # unresolved child -> spins
                cancel_path=cancel,
            )
        )
        await asyncio.sleep(0.15)
        cancel.touch()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(driver()) is True


def test_wait_terminal_raises_on_proc_exit(tmp_path: Path) -> None:
    root = tmp_path / "root.jsonl"
    root.write_text(_sub_agent_event("c1"), encoding="utf-8")

    class Proc:
        returncode = None

    proc = Proc()

    async def driver() -> None:
        task = asyncio.create_task(
            wait_for_agent_tree_terminal(
                root,
                start_byte=0,
                resolve_path=lambda tid: None,
                proc=proc,
            )
        )
        await asyncio.sleep(0.15)
        proc.returncode = 2
        await task

    with pytest.raises(RuntimeError, match="exited before agent tree"):
        asyncio.run(driver())


def test_wait_terminal_ignores_already_known_subagent(tmp_path: Path) -> None:
    # A child rollout re-emitting a subagent whose id is already a known node
    # (here "__root__") must not re-queue it for resolution.
    root = tmp_path / "root.jsonl"
    child = tmp_path / "child.jsonl"
    root.write_text(_sub_agent_event("c1"), encoding="utf-8")
    child.write_text(
        _sub_agent_event("__root__")
        + _event({"type": "task_started"})
        + _event({"type": "task_complete"}),
        encoding="utf-8",
    )
    paths = {"c1": child}

    asyncio.run(
        wait_for_agent_tree_terminal(
            root, start_byte=0, resolve_path=lambda tid: paths.get(tid)
        )
    )


def test_wait_terminal_awaits_awaitable_resolver_result(tmp_path: Path) -> None:
    # When a non-coroutine resolver returns an awaitable, the loop must await
    # it before treating the value as a resolved path.
    root = tmp_path / "root.jsonl"
    child = tmp_path / "child.jsonl"
    root.write_text(_sub_agent_event("c1"), encoding="utf-8")
    child.write_text(
        _event({"type": "task_started"}) + _event({"type": "task_complete"}),
        encoding="utf-8",
    )
    paths = {"c1": child}

    async def awaitable_value(thread_id: str) -> Path | None:
        return paths.get(thread_id)

    def resolve_path(thread_id: str):  # not a coroutine function
        return awaitable_value(thread_id)

    asyncio.run(
        wait_for_agent_tree_terminal(
            root, start_byte=0, resolve_path=resolve_path
        )
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
