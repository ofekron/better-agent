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
import event_journal  # noqa: E402
import session_manager as session_manager_module  # noqa: E402
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
    event_journal.event_journal_writer._executor.submit(
        sid, lambda: None,
    ).result(timeout=10)
    session_manager.flush_pending_persists()
    event_journal.event_journal_writer._executor.submit(
        sid, lambda: None,
    ).result(timeout=10)
    return sid


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []
    source = open(session_store.__file__, "r", encoding="utf-8").read()
    writer_start = source.index("def _get_durability_writer(")
    writer_end = source.index("def _wait_durability(", writer_start)
    writer_source = source[writer_start:writer_end]
    if "max_batch_age_s=0" not in writer_source:
        raise AssertionError("session durability writes must not wait for batch age")
    upsert_start = source.index("def _upsert_summary(")
    upsert_end = source.index("def _seen_cursor_path(", upsert_start)
    upsert_source = source[upsert_start:upsert_end]
    for timer in (
        "store.session.summary.build",
        "store.session.summary.index",
        "store.session.summary.sidecar_stat",
    ):
        if timer not in upsert_source:
            raise AssertionError(f"missing summary write timer {timer}")
    if "_schedule_summary_sidecar_write(" not in upsert_source:
        raise AssertionError("summary sidecar write must be scheduled from _upsert_summary")
    manager_source = open(session_manager_module.__file__, "r", encoding="utf-8").read()
    if "threading.Timer" in manager_source:
        raise AssertionError("persist debounce must not create per-write Timer threads")
    for expected in (
        "_persist_deadlines",
        "_persist_deadline_heap",
        "def _persist_scheduler_loop(",
        'name="session-persist-scheduler"',
    ):
        if expected not in manager_source:
            raise AssertionError(f"missing persist scheduler guard {expected}")
    persist_start = manager_source.index("def _persist_root(")
    persist_end = manager_source.index("    def _tail_persist(", persist_start)
    persist_source = manager_source[persist_start:persist_end]
    tail_start = manager_source.index("def _tail_persist(")
    tail_end = manager_source.index("    def _drop_pending_persist(", tail_start)
    tail_source = manager_source[tail_start:tail_end]
    for timer in (
        "session.tail_persist.lock_copy",
        "session.tail_persist.state",
        "session.tail_persist.copy",
        "session.tail_persist.write_full",
    ):
        if timer not in tail_source:
            raise AssertionError(f"missing tail-persist timer {timer}")

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

    version_before = session_store.summary_version()
    session_store._summary_sidecar_write_queue.join()
    session_store.write_session_full(root, bump_updated_at=False)
    session_store._summary_sidecar_write_queue.join()
    version_after = session_store.summary_version()
    results.append(
        (
            "unchanged write does not rewrite summary projection",
            version_after == version_before,
            f"version before={version_before} after={version_after}",
        )
    )
    original_touch = session_store._touch_summary_file_current
    original_write_summary = session_store._write_summary_file
    touch_mtimes: list[int | None] = []
    summary_writes: list[str] = []

    def track_touch(root_id, *, summary, root_mtime_ns=None, root_signature=None):
        touch_mtimes.append(root_mtime_ns)
        return original_touch(
            root_id,
            summary=summary,
            root_mtime_ns=root_mtime_ns,
            root_signature=root_signature,
        )

    def track_summary_write(root_id, summary, **kwargs):
        summary_writes.append(root_id)
        return original_write_summary(root_id, summary, **kwargs)

    end_to_end_started = time.perf_counter()
    session_store._summary_sidecar_write_queue.join()
    with (
        patch("session_store._touch_summary_file_current", side_effect=track_touch),
        patch("session_store._write_summary_file", side_effect=track_summary_write),
    ):
        foreground_started = time.perf_counter()
        session_store.write_session_full(root, bump_updated_at=False)
        foreground_ms = (time.perf_counter() - foreground_started) * 1000.0
        session_store._summary_sidecar_write_queue.join()
    end_to_end_ms = (time.perf_counter() - end_to_end_started) * 1000.0
    summary_path = session_store._session_path(sid).with_name(f"{sid}.summary.json")
    summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
    committed_signature = session_store._session_file_signature(
        session_store._session_path(sid)
    )
    results.append(
        (
            "unchanged summary refresh reuses write-path mtime",
            bool(touch_mtimes) and all(mtime is not None for mtime in touch_mtimes),
            f"touch_mtimes={touch_mtimes}",
        )
    )
    results.append((
        "summary refresh foreground < 20ms and end-to-end < 100ms",
        foreground_ms < 20.0
        and end_to_end_ms < 100.0
        and summary_writes == [sid]
        and summary_payload.get("_root_file_signature") == list(committed_signature or ()),
        f"foreground={foreground_ms:.2f}ms end_to_end={end_to_end_ms:.2f}ms "
        f"writes={summary_writes} embedded={summary_payload.get('_root_file_signature')}",
    ))

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
            and msg.get("events")
            and msg.get("_uid_idx") == {"u-new": 0}
            and msg.get("isStreaming") is True
            ,
            (
                f"copied_keys={sorted(copied_msg)} "
                f"live_events={len(msg.get('events') or [])}"
            ),
        )
    )
    msg.pop("isStreaming", None)
    msg.pop("_uid_idx", None)

    msg["isStreaming"] = True
    msg["_uid_idx"] = {"u-new": 0}
    msg["events"] = [_native_event("tail-live", "tail-live")]
    with patch("session_store._strip_volatile_from_tree", wraps=session_store._strip_volatile_from_tree) as strip:
        session_manager._persist_root(sid, bump=False)
        session_manager.flush_pending_persists()
    tail_disk = json.loads(session_store._session_path(sid).read_text(encoding="utf-8"))
    tail_msg = tail_disk["messages"][-1]
    results.append(
        (
            "tail persist skips duplicate strip on persistable copy",
            strip.call_count == 1
            and "events" not in tail_msg
            and "isStreaming" not in tail_msg
            and "_uid_idx" not in tail_msg
            and msg.get("isStreaming") is True
            and msg.get("_uid_idx") == {"u-new": 0}
            and msg.get("events"),
            (
                f"strip_calls={strip.call_count} "
                f"disk_root_keys={sorted(tail_disk)} "
                f"disk_msg_keys={sorted(tail_msg)}"
            ),
        )
    )
    msg.pop("isStreaming", None)
    msg.pop("_uid_idx", None)
    msg.pop("events", None)

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
    order_version_before_projection = session_store._summary_order_version
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
            and session_store._summary_order_version == order_version_before_projection
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

    opened_at = "2030-01-02T03:04:05"
    session_manager.flush_pending_persists()
    with (
        patch("session_store.write_session_full", side_effect=AssertionError("opened wrote full tree")),
        patch("session_manager.copy.deepcopy", side_effect=AssertionError("opened deep-copied session")),
    ):
        opened = session_manager.set_last_opened_at(sid, opened_at)
    returned_messages = opened.get("messages") if opened is not None else None
    if isinstance(returned_messages, list):
        returned_messages.append({"id": "mutated-return"})
    cached_after_opened = session_manager.get_ref(sid)
    projected_opened = next(s for s in session_store.list_sessions() if s["id"] == sid)
    disk_after_opened = json.loads(session_store._session_path(sid).read_text(encoding="utf-8"))
    reloaded_after_opened = session_store.get_root_tree(sid)
    results.append(
        (
            "session open uses opened sidecar instead of full tree write",
            opened is not None
            and opened.get("last_opened_at") == opened_at
            and projected_opened.get("last_opened_at") == opened_at
            and disk_after_opened.get("last_opened_at") != opened_at
            and reloaded_after_opened.get("last_opened_at") == opened_at,
            (
                f"projected={projected_opened.get('last_opened_at')!r} "
                f"disk={disk_after_opened.get('last_opened_at')!r} "
                f"reloaded={reloaded_after_opened.get('last_opened_at')!r}"
            ),
        )
    )
    results.append(
        (
            "session open returns isolated copy without deepcopy",
            opened is not None
            and all(
                msg.get("id") != "mutated-return"
                for msg in cached_after_opened.get("messages", [])
                if isinstance(msg, dict)
            ),
            f"returned_messages={len(returned_messages or [])} cached_messages={len(cached_after_opened.get('messages', []))}",
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
