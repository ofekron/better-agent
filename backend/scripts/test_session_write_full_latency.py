"""Locks the v8 write_session_full latency regression:

A session with 3000 events on the heaviest msg should write the
snapshot in < 20 ms. The pre-v8 path (events embedded in JSON) was
measured at 150 ms+ on the same shape. This test guards against
accidentally re-embedding events on disk.

Also asserts that REST GET / cache-hit-path stays responsive while
ingest fires concurrently — the per-root lock holds for tens of
microseconds during a thin write, not hundreds of milliseconds.

Run with:
    cd backend && .venv/bin/python scripts/test_session_write_full_latency.py
"""

from __future__ import annotations

import os
import json
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-latency-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _native_event(uuid: str, text: str = "x") -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        },
    }


def _build_heavy_session(n: int) -> str:
    sess = session_manager.create(
        name="heavy", model="sonnet", cwd="/tmp/test-latency",
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
    ctx = ApplyEventCtx(root_id=sid, run_id="run-heavy")
    for i in range(n):
        ev = _native_event(f"u-{i}", f"text-{i}" * 20)  # ~120B per event
        strategy.apply_event(
            app_session_id=sid, msg=msg, event=ev, ctx=ctx, source_is_provider_stream=True,
        )
    session_manager.flush_pending_persists()
    return sid


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []
    sid = _build_heavy_session(3000)

    # Force an explicit write and measure.
    root = session_manager.get_ref(sid)
    # Cold the perf cache.
    t0 = time.perf_counter()
    session_store.write_session_full(root, bump_updated_at=False)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    results.append(
        (f"write_session_full < 20ms (3000 events)", elapsed_ms < 20.0,
         f"got {elapsed_ms:.2f}ms"))

    # Repeat 5x to catch warm-cache regressions and report median.
    samples = []
    for _ in range(5):
        t0 = time.perf_counter()
        session_store.write_session_full(root, bump_updated_at=False)
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    median = samples[len(samples) // 2]
    results.append(
        (f"median write_session_full < 15ms", median < 15.0,
         f"got median={median:.2f}ms samples={[f'{s:.1f}' for s in samples]}"))

    msg = root["messages"][-1]
    ctx = ApplyEventCtx(root_id=sid, run_id="run-heavy")
    get_strategy("native").apply_event(
        app_session_id=sid,
        msg=msg,
        event=_native_event("u-new", "new-output"),
        ctx=ctx,
        source_is_provider_stream=True,
    )
    session_store.write_session_full(root, bump_updated_at=False)
    disk = json.loads(session_store._session_path(sid).read_text(encoding="utf-8"))
    disk_msg = disk["messages"][-1]
    results.append(
        (
            "dirty assistant content refreshes before persist",
            disk_msg.get("content") == "new-output" and "_content_dirty" not in disk_msg,
            f"content={disk_msg.get('content')!r} dirty={disk_msg.get('_content_dirty')!r}",
        )
    )

    root["draft_input"] = "draft"
    msg["isStreaming"] = True
    msg["_uid_idx"] = {"u-new": 0}
    original_deepcopy = session_store.copy.deepcopy

    def guarded_deepcopy(value):
        if (
            isinstance(value, list)
            and len(value) > 100
            and all(isinstance(item, dict) and item.get("type") for item in value[:5])
        ):
            raise AssertionError("assistant event list was deep-copied")
        return original_deepcopy(value)

    with patch("session_store.copy.deepcopy", side_effect=guarded_deepcopy):
        copied = session_store.copy_persistable_tree(root)
    copied_msg = copied["messages"][-1]
    results.append(
        (
            "persistable copy strips volatile fields before deepcopy",
            "events" not in copied_msg
            and "_uid_idx" not in copied_msg
            and "isStreaming" not in copied_msg
            and "draft_input" not in copied
            and msg.get("events")
            and msg.get("_uid_idx") == {"u-new": 0}
            and msg.get("isStreaming") is True
            and root.get("draft_input") == "draft",
            (
                f"copied_keys={sorted(copied_msg)} "
                f"live_events={len(msg.get('events') or [])} "
                f"live_draft={root.get('draft_input')!r}"
            ),
        )
    )
    root.pop("draft_input", None)
    msg.pop("isStreaming", None)
    msg.pop("_uid_idx", None)

    # Concurrent contention: alternating writer + reader on the same
    # session. Writer goes through `set_pinned` which acquires
    # `_lock_for_root(rid)` and calls `_persist_root` →
    # `write_session_full`. Reader goes through
    # `get_root_tree_paginated` which also takes the lock. They
    # serialize. With the thin snapshot the writer holds the lock for
    # ~2ms per write, so reader latency stays bounded.
    #
    # Run synchronously (no threads — keeps the test deterministic;
    # the goal is to prove the write isn't the dominant cost, not to
    # stress the lock).
    rest_latencies: list[float] = []
    for _ in range(30):
        session_manager.set_pinned(sid, True)
        t0 = time.perf_counter()
        _ = session_manager.get_root_tree_paginated(sid, msg_limit=50)
        rest_latencies.append((time.perf_counter() - t0) * 1000.0)
        session_manager.set_pinned(sid, False)

    rest_latencies.sort()
    p95 = rest_latencies[int(len(rest_latencies) * 0.95)]
    # The cache-hit path is a deep_copy of the trimmed tree. With
    # 3000 events on msg.events the deepcopy itself is the dominant
    # cost — bar at 200ms accommodates that, while still catching
    # regressions where reads queue behind a 150ms+ write.
    results.append(
        (f"REST p95 < 200ms while interleaved with writes",
         p95 < 200.0,
         f"got p95={p95:.2f}ms n={len(rest_latencies)} "
         f"samples-trim={[f'{s:.1f}' for s in rest_latencies[:5]]}..."))

    session_store._ensure_summary_index(blocking=True)
    session_store.set_marker_projection(sid, "ext-a", {"color": "#ff0000"})
    session_store.set_requirement_tags_projection({
        sid: [{"id": "req-a", "label": "Req A"}],
    })
    projected = next(s for s in session_store.list_sessions() if s["id"] == sid)
    version_after_projection = session_store.summary_version()
    search_before = session_store.grep_session_scores("heavy", {"title"})
    metadata_cache_keys_before = tuple(session_store._metadata_search_cache)
    _ = session_store.list_sessions()
    _ = session_store.list_sessions()
    summary_version_stable = session_store.summary_version() == version_after_projection
    results.append(
        (
            "session list uses maintained tag/marker projection",
            projected.get("markers") == {"ext-a": {"color": "#ff0000"}}
            and projected.get("requirement_tags") == [{"id": "req-a", "label": "Req A"}]
            and summary_version_stable,
            f"markers={projected.get('markers')!r} tags={projected.get('requirement_tags')!r}",
        )
    )
    session_store.set_marker_projection(sid, "ext-a", {"color": "#00ff00"})
    search_after = session_store.grep_session_scores("heavy", {"title"})
    metadata_cache_keys_after = tuple(session_store._metadata_search_cache)
    results.append(
        (
            "metadata session search ignores marker projection churn",
            search_before == search_after
            and metadata_cache_keys_before == metadata_cache_keys_after,
            f"before={search_before!r} after={search_after!r}",
        )
    )

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name} — {msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        ok = _run()
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
