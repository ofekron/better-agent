"""Locks the `SessionManager.get_lite()` contract:

  - Returns metadata identical to `get()` for top-level + per-msg
    non-event fields.
  - Events lists (`msg.events`, `msg.workers[*].events`) are EMPTY in
    the returned dict.
  - The live cache is UNCHANGED — `get_lite()` strip/restore is
    atomic under the per-root lock (mirrors the write path in
    `session_store._strip_volatile_from_tree`).
  - Hot callers (`_require_session`, `_ref_ctx_for_root`) get correct
    behavior with the lite snapshot.

Run with:
    cd backend && .venv/bin/python scripts/test_get_lite_skips_events.py
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-get-lite-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _ev(uid: str) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "x" * 200}]},
        },
    }


def _build_heavy(n: int) -> tuple[str, dict]:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/get-lite",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    scaffold = strategy.build_assistant_scaffold()
    scaffold["id"] = "msg-1"
    scaffold["role"] = "assistant"
    scaffold["seq"] = 1
    session_manager.append_assistant_msg(sid, scaffold)
    msg = session_manager.get_ref(sid)["messages"][-1]
    ctx = ApplyEventCtx(root_id=sid, run_id="r")
    rid = session_manager._root_id_for(sid)
    session_manager._batches[rid] = {"_phantom": True, "bump_updated_at": False}
    try:
        for i in range(n):
            strategy.apply_event(
                app_session_id=sid, msg=msg, event=_ev(f"u-{i}"),
                ctx=ctx, source_is_provider_stream=False,
            )
    finally:
        session_manager._batches.pop(rid, None)
    return sid, msg


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    N = 2000
    sid, msg = _build_heavy(N)

    # 1) get_lite has empty msg.events.
    lite = session_manager.get_lite(sid)
    assert lite is not None
    results.append(
        ("get_lite: msg.events is empty",
         lite["messages"][-1].get("events") == [],
         f"got len={len(lite['messages'][-1].get('events') or [])}"))

    # 2) Top-level metadata identical to get().
    full = session_manager.get(sid)
    assert full is not None
    keys = {"id", "name", "model", "cwd", "orchestration_mode",
            "pinned", "archived",
            "supervisor_enabled", "node_id"}
    same = all(lite.get(k) == full.get(k) for k in keys)
    results.append(
        ("get_lite top-level metadata matches get()",
         same, "diff"))

    # 3) Per-msg metadata identical except events.
    msg_keys = {"id", "role", "content", "seq", "timestamp"}
    msg_same = all(
        lite["messages"][-1].get(k) == full["messages"][-1].get(k)
        for k in msg_keys
    )
    results.append(
        ("get_lite msg metadata matches get()", msg_same, "diff"))

    # 4) Live cache unchanged — msg.events still populated.
    live = session_manager.get_ref(sid)
    assert live is not None
    results.append(
        ("live cache msg.events still has N entries after get_lite",
         len(live["messages"][-1].get("events") or []) == N,
         f"got {len(live['messages'][-1].get('events') or [])}"))

    # 5) get_lite is much faster than get() for heavy session.
    # Measure both.
    t0 = time.perf_counter()
    for _ in range(5):
        _ = session_manager.get(sid)
    t_get = (time.perf_counter() - t0) * 1000 / 5
    t0 = time.perf_counter()
    for _ in range(5):
        _ = session_manager.get_lite(sid)
    t_lite = (time.perf_counter() - t0) * 1000 / 5
    results.append(
        (f"get_lite is faster than get() ({t_lite:.1f}ms vs {t_get:.1f}ms)",
         t_lite < t_get,
         f"get_lite={t_lite:.1f}ms get={t_get:.1f}ms"))

    # 6) `_uid_idx` is stripped from get_lite output too.
    has_idx = "_uid_idx" in lite["messages"][-1]
    results.append(
        ("get_lite strips _uid_idx", not has_idx,
         f"_uid_idx present in lite output"))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def test_cold_get_lite_skips_event_hydration() -> bool:
    sid, _msg = _build_heavy(3)
    session_manager.flush_pending_persists()
    session_manager._roots.pop(sid, None)
    session_manager._event_hydrated_roots.discard(sid)

    original = session_manager._hydrate_cached_root_events
    calls: list[str] = []

    def spy(rid, root):
        calls.append(rid)
        return original(rid, root)

    session_manager._hydrate_cached_root_events = spy
    try:
        lite = session_manager.get_lite(sid)
    finally:
        session_manager._hydrate_cached_root_events = original
    ok = lite is not None and calls == []
    print(
        f"  {PASS if ok else FAIL} cold get_lite skips event hydration"
        f"{'' if ok else f' - hydrate calls={calls}'}"
    )
    return ok


def main() -> int:
    try:
        ok = _run() and test_cold_get_lite_skips_event_hydration()
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
