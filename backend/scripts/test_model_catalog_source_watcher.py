from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402


TEST_HOME = _test_home.TestHome.acquire("ba-test-model-catalog-watcher-")

from model_catalog_source_watcher import (  # noqa: E402
    CatalogSourceWatcher,
    SourceWatchSpec,
)


async def _next_change(queue: asyncio.Queue[tuple[str, ...]]) -> None:
    async with asyncio.timeout(3):
        assert await queue.get()


async def _exercise_file_lifecycle(root: Path) -> None:
    config = root / "config.toml"
    queue: asyncio.Queue[tuple[str, ...]] = asyncio.Queue()
    watcher = CatalogSourceWatcher(ready_timeout_ms=50, debounce_ms=20)
    await watcher.start(
        SourceWatchSpec.build(exact_paths=[config]),
        queue.put_nowait,
    )
    await watcher.wait_ready()

    config.write_text("one", encoding="utf-8")
    await _next_change(queue)

    replacement = root / "replacement"
    replacement.write_text("two", encoding="utf-8")
    os.replace(replacement, config)
    await _next_change(queue)

    config.unlink()
    await _next_change(queue)

    await watcher.shutdown()
    assert not watcher.running


async def _exercise_symlink_retarget_and_restart(root: Path) -> None:
    first = root / "first"
    second = root / "second"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    logical = root / "codex"
    logical.symlink_to(first)
    queue: asyncio.Queue[tuple[str, ...]] = asyncio.Queue()
    spec = SourceWatchSpec.build(exact_paths=[logical])

    watcher = CatalogSourceWatcher(ready_timeout_ms=50, debounce_ms=20)
    await watcher.start(spec, queue.put_nowait)
    await watcher.wait_ready()

    replacement = root / "codex.next"
    replacement.symlink_to(second)
    os.replace(replacement, logical)
    await _next_change(queue)
    await watcher.shutdown()

    await watcher.start(spec, queue.put_nowait)
    await watcher.wait_ready()
    logical.unlink()
    await _next_change(queue)
    await watcher.shutdown()
    assert not watcher.running


def test_native_watcher_file_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as raw:
        asyncio.run(_exercise_file_lifecycle(Path(raw)))


def test_native_watcher_symlink_retarget_restart_and_shutdown() -> None:
    with tempfile.TemporaryDirectory() as raw:
        asyncio.run(_exercise_symlink_retarget_and_restart(Path(raw)))


if __name__ == "__main__":
    test_native_watcher_file_lifecycle()
    test_native_watcher_symlink_retarget_restart_and_shutdown()
    print("PASS: native catalog source watcher lifecycle")
