"""Regression test: `OwnedClaudeJsonlTailer._dispatch` must not block the
event loop while acquiring the session owner token.

`_dispatch` used to call `self._ensure_owner_token()` directly (bare, on
the event loop) in both the primary-agent branch and the worker-fork
branch. `_ensure_owner_token` -> `session_manager.claim_owner` ->
`with self._lock_for_root(rid):` acquires a per-root `threading.RLock`
that is ALSO held by `asyncio.to_thread`-dispatched work elsewhere (e.g.
`get_messages_since`). Live faulthandler evidence showed 3/3 samples of
a ~1.9s stall sitting exactly on `claim_owner`'s lock acquire while a
worker thread held the same root's lock doing CPU-bound snapshot work.

Fix: both `_dispatch` call sites now route `_ensure_owner_token` through
`asyncio.to_thread`, matching the `run_if_owner` calls right next to them.

This test proves:
  1. The loop stays responsive while `_ensure_owner_token` is slow (an
     injected delay standing in for real lock contention).
  2. A control case using the OLD bare (non-to_thread) call pattern
     really does freeze the loop, so a false pass above isn't hiding a
     no-op.
  3. Dispatch semantics are unchanged: `_dispatch` still returns without
     ingesting when the token is None, and still ingests when a token
     is obtained.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
from pathlib import Path

import _test_home

import pytest

pytestmark = pytest.mark.anyio
_TMP_HOME = _test_home.isolate("bc-test-dispatch-owner-token-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager  # noqa: E402
from jsonl_tailer import OwnedClaudeJsonlTailer  # noqa: E402

SLOW_TOKEN_SECONDS = 0.6

_FAKE_SESS = {
    "messages": [],
    "orchestration_mode": "manager",
    "agent_session_id": "worker-fork-sid",  # matches agent_sid -> primary branch
}


async def _count_ticks_while(coro):
    ticks = 0
    stop = False

    async def _heartbeat():
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0.02)
            ticks += 1

    hb_task = asyncio.create_task(_heartbeat())
    result = await coro
    stop = True
    await hb_task
    return ticks, result


def _make_tailer() -> OwnedClaudeJsonlTailer:
    return OwnedClaudeJsonlTailer(
        root_id="root-t",
        app_session_id="app-t",
        agent_sid="worker-fork-sid",
        jsonl_path=Path(_TMP_HOME) / "dummy.jsonl",
        start_offset=0,
    )


async def test_offloaded_token_does_not_block_loop():
    tailer = _make_tailer()

    class _FakeToken:
        sid = "worker-fork-sid"

    def _slow_ensure_token():
        time.sleep(SLOW_TOKEN_SECONDS)
        return _FakeToken()

    orig_ensure = tailer._ensure_owner_token
    orig_get_lite = manager.get_lite
    orig_run_if_owner = manager.run_if_owner
    manager.get_lite = lambda sid: dict(_FAKE_SESS)
    manager.run_if_owner = lambda token, cb: (True, cb())
    tailer._ensure_owner_token = _slow_ensure_token  # type: ignore[method-assign]
    ingested: list[dict] = []
    orig_ingest_orphan = None
    try:
        from orchs import get_strategy
        strategy = get_strategy("manager")
        orig_ingest_orphan = strategy.ingest_orphan
        strategy.ingest_orphan = lambda **kw: ingested.append(kw)

        ticks, _ = await _count_ticks_while(
            tailer._dispatch({"uuid": "u-1", "type": "assistant",
                               "message": {"content": []}})
        )
    finally:
        tailer._ensure_owner_token = orig_ensure
        manager.get_lite = orig_get_lite
        manager.run_if_owner = orig_run_if_owner
        if orig_ingest_orphan is not None:
            strategy.ingest_orphan = orig_ingest_orphan

    assert ticks >= 10, (
        f"loop froze during {SLOW_TOKEN_SECONDS}s slow token acquire: "
        f"ticks={ticks} (want >=10)"
    )
    assert len(ingested) == 1, f"expected 1 ingest, got {len(ingested)}"


async def test_direct_token_blocks_loop_control():
    """Control: prove the OLD (bare, non-to_thread) call pattern really
    does freeze the loop, so a false pass above isn't hiding a no-op."""
    tailer = _make_tailer()

    class _FakeToken:
        sid = "worker-fork-sid"

    def _slow_ensure_token():
        time.sleep(SLOW_TOKEN_SECONDS)
        return _FakeToken()

    async def _direct_dispatch():
        # Mirrors the pre-fix call site: bare call, no to_thread.
        token = tailer._ensure_owner_token()
        return token

    tailer._ensure_owner_token = _slow_ensure_token  # type: ignore[method-assign]
    ticks, _ = await _count_ticks_while(_direct_dispatch())

    assert ticks == 0, (
        f"bare (non-to_thread) acquire should freeze the loop: ticks={ticks} "
        f"(want ==0, proves slow acquire is real blocking, not a no-op)"
    )


async def test_none_token_still_returns_without_ingest():
    """Semantics unchanged: a None token still short-circuits _dispatch
    without ingesting."""
    tailer = _make_tailer()
    orig_get_lite = manager.get_lite
    manager.get_lite = lambda sid: dict(_FAKE_SESS)
    tailer._ensure_owner_token = lambda: None  # type: ignore[method-assign]
    ingested: list[dict] = []
    strategy = None
    orig_ingest_orphan = None
    try:
        from orchs import get_strategy
        strategy = get_strategy("manager")
        orig_ingest_orphan = strategy.ingest_orphan
        strategy.ingest_orphan = lambda **kw: ingested.append(kw)
        await tailer._dispatch({"uuid": "u-2", "type": "assistant",
                                 "message": {"content": []}})
    finally:
        manager.get_lite = orig_get_lite
        if orig_ingest_orphan is not None:
            strategy.ingest_orphan = orig_ingest_orphan

    assert len(ingested) == 0, f"None token should not ingest, got {len(ingested)}"


def test_dispatch_uses_to_thread_for_ensure_owner_token():
    """Static guard: both `_dispatch` call sites must route through
    `_ensure_owner_token_off_loop` (which only pays a thread hop on a
    cache miss), not call `_ensure_owner_token` bare."""
    src = inspect.getsource(OwnedClaudeJsonlTailer._dispatch)
    bare_calls = [
        line for line in src.splitlines()
        if "self._ensure_owner_token()" in line
    ]
    off_loop_calls = src.count("self._ensure_owner_token_off_loop()")
    wrapper_src = inspect.getsource(
        OwnedClaudeJsonlTailer._ensure_owner_token_off_loop,
    )
    wrapper_uses_to_thread = "asyncio.to_thread(self._ensure_owner_token)" in wrapper_src
    assert off_loop_calls == 2, (
        f"expected 2 off-loop call sites, got {off_loop_calls}"
    )
    assert not bare_calls, f"bare _ensure_owner_token calls remain: {bare_calls}"
    assert wrapper_uses_to_thread, "off-loop wrapper does not use asyncio.to_thread"
