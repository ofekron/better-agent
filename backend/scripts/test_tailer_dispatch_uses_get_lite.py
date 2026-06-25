"""Proves review claim #3: `OwnedClaudeJsonlTailer._dispatch` calls
`session_manager.get()` on every enriched line (jsonl_tailer.py:818).
`get()` deep-copies the ENTIRE session tree including all events lists
(200-800 ms on a hydrated session per `get_lite`'s own docstring),
while `_dispatch` only reads top-level metadata
(`*_agent_session_id`, `orchestration_mode`, `messages[].isStreaming`,
`messages[].id`) — none of which live in the events lists. `get_lite()`
exists precisely to skip that copy.

The test spies on both accessors, drives one real `_dispatch`, and
asserts the desired contract: the hot path uses `get_lite()` and never
`get()`. It FAILS on current code (get called once, get_lite zero) and
PASSES once `_dispatch` switches to `get_lite()`.

Run with:
    cd backend && .venv/bin/python scripts/test_tailer_dispatch_uses_get_lite.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-tailer-getlite-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager  # noqa: E402
from event_bus import bus  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from event_journal import event_journal_writer  # noqa: E402
from jsonl_tailer import OwnedClaudeJsonlTailer  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

# Non-primary worker-fork session: no *_agent_session_id matches the
# tailer's agent_sid, so _dispatch takes the simplest branch after the
# single get/get_lite call at line 818. Empty messages -> msg_id None.
_FAKE_SESS = {
    "messages": [],
    "orchestration_mode": "manager",
}


async def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    counts = {"get": 0, "get_lite": 0}

    def spy_get(sid):
        counts["get"] += 1
        return dict(_FAKE_SESS)

    def spy_get_lite(sid):
        counts["get_lite"] += 1
        return dict(_FAKE_SESS)

    # Stub the downstream ingest so the ONLY session accessor call we
    # measure is _dispatch's own line-818 lookup. (event_ingester.ingest
    # resolves cwd via session_manager and would otherwise add an
    # incidental accessor call that masks which one _dispatch uses.)
    orig_ingest = event_ingester.ingest
    event_ingester.ingest = lambda *a, **k: 0

    orig_get = manager.get
    orig_get_lite = manager.get_lite
    manager.get = spy_get
    manager.get_lite = spy_get_lite
    try:
        event_journal_writer.register(bus)
        tailer = OwnedClaudeJsonlTailer(
            root_id="root-t",
            app_session_id="app-t",
            agent_sid="worker-fork-sid",   # not a primary agent sid
            jsonl_path=Path(_TMP_HOME) / "dummy.jsonl",
            start_offset=0,
        )
        await tailer._dispatch({"uuid": "u-1", "type": "assistant",
                                "message": {"content": []}})
    finally:
        manager.get = orig_get
        manager.get_lite = orig_get_lite
        event_ingester.ingest = orig_ingest

    # Sanity: exactly one accessor call happened per dispatched line.
    results.append((
        "exactly one session accessor call per dispatched line",
        counts["get"] + counts["get_lite"] == 1,
        f"get={counts['get']} get_lite={counts['get_lite']}",
    ))

    # Desired contract: _dispatch uses the cheap get_lite()...
    results.append((
        "_dispatch uses get_lite() (the cheap accessor)",
        counts["get_lite"] == 1,
        f"get_lite called {counts['get_lite']}x (expected 1)",
    ))

    # ...and never the deepcopy-heavy get().
    results.append((
        "_dispatch does NOT call get() (the deepcopy accessor)",
        counts["get"] == 0,
        f"get() called {counts['get']}x on the hot path (expected 0)",
    ))

    cursor_tailer = OwnedClaudeJsonlTailer(
        root_id="root-t",
        app_session_id="app-t",
        agent_sid="worker-fork-sid",
        jsonl_path=Path(_TMP_HOME) / "dummy.jsonl",
        start_offset=0,
    )
    persisted: list[int] = []
    cursor_tailer._persist_cursor = persisted.append  # type: ignore[method-assign]

    # `_on_cursor` now hands persistence to the global cursor_ledger_worker,
    # which processes one write per key at a time on its own thread. Force
    # that worker into a controlled in-flight state on this tailer's key
    # BEFORE bursting cursor advances, so "coalesced" is a deterministic
    # assertion instead of a race against the worker thread's own
    # scheduling latency.
    import threading
    from cursor_ledger_worker import worker as cursor_ledger_worker
    block_started = threading.Event()
    release_block = threading.Event()

    def _block_write() -> None:
        block_started.set()
        release_block.wait(2.0)

    cursor_ledger_worker.note(cursor_tailer._cursor_key, _block_write)
    block_started.wait(2.0)

    for n in range(1, 10):
        cursor_tailer._on_cursor(n)
    results.append((
        "small cursor advances are coalesced",
        persisted == [],
        f"persisted={persisted!r}",
    ))
    release_block.set()

    class _FakeTailer:
        def stop(self) -> None:
            return None

    cursor_tailer._refcount = 1
    cursor_tailer._tailer = _FakeTailer()  # type: ignore[assignment]
    cursor_tailer._task = asyncio.create_task(asyncio.sleep(0))
    task = cursor_tailer.release()
    if task is not None:
        await task
    results.append((
        "release flushes pending cursor",
        persisted == [9],
        f"persisted={persisted!r}",
    ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        ok = asyncio.run(_run())
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
