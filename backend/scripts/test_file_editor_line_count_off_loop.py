from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
import shutil
import tempfile
import threading


_STATE_HOME = tempfile.mkdtemp(prefix="ba-file-editor-line-count-")
os.environ["BETTER_AGENT_HOME"] = _STATE_HOME

import file_editor
from orchs import jsonl_helpers


async def _scenario() -> None:
    original = file_editor._base_line_count
    entered = threading.Event()
    release = threading.Event()

    def blocking_line_count(*_args, **_kwargs) -> int:
        entered.set()
        if not release.wait(timeout=1.0):
            raise AssertionError("line-count worker was not released")
        return 17

    file_editor._base_line_count = blocking_line_count
    try:
        task = asyncio.create_task(
            file_editor._base_line_count_async("/project", {}, "agent-sid")
        )
        assert await asyncio.to_thread(entered.wait, 1.0)
        loop_advanced = asyncio.Event()
        asyncio.get_running_loop().call_soon(loop_advanced.set)
        await asyncio.wait_for(loop_advanced.wait(), timeout=0.1)
        release.set()
        assert await task == 17
    finally:
        release.set()
        file_editor._base_line_count = original

    loop_thread = threading.get_ident()
    resolver_threads: list[int] = []
    counter_threads: list[int] = []
    original_resolver = jsonl_helpers.compute_jsonl_read_path
    original_counter = jsonl_helpers.count_jsonl_lines

    def resolve_path(*_args, **_kwargs) -> Path:
        resolver_threads.append(threading.get_ident())
        return Path("/tmp/fake.jsonl")

    def count_lines(_path: Path) -> int:
        counter_threads.append(threading.get_ident())
        return 23

    jsonl_helpers.compute_jsonl_read_path = resolve_path
    jsonl_helpers.count_jsonl_lines = count_lines
    try:
        assert await file_editor._base_line_count_async("/project", {}, "agent-sid") == 23
    finally:
        jsonl_helpers.compute_jsonl_read_path = original_resolver
        jsonl_helpers.count_jsonl_lines = original_counter

    assert resolver_threads and resolver_threads[0] != loop_thread
    assert counter_threads and counter_threads[0] != loop_thread
    assert "await _base_line_count_async(" in inspect.getsource(
        file_editor._create_interactive_fork
    )


def main() -> None:
    try:
        asyncio.run(_scenario())
        print("PASS: file-editor line count runs off the event loop")
    finally:
        shutil.rmtree(_STATE_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
