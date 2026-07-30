"""Locks the v8 write_session_full payload regression:

A session with 3000 events on the heaviest msg must serialize to the
exact same snapshot bytes as the otherwise-identical persistable tree.
The pre-v8 path embedded events in JSON and took 150 ms+ on the same
shape. Exact payload equality catches that regression without treating
filesystem durability latency as application work.

Also keeps a generous REST cache-hit smoke metric and the persistence
correctness checks that protect the thin-snapshot path.

Run with:
    cd backend && .venv/bin/python scripts/test_session_write_full_latency.py
"""

from __future__ import annotations

import os
import json
import shutil
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-latency-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
import session_manager as session_manager_module  # noqa: E402
import messages_delta_compaction  # noqa: E402
import main as backend_main  # noqa: E402
import session_detail_api  # noqa: E402
from grouped_durability_writer import DurabilityReceipt  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _CaptureWriter:
    def __init__(self) -> None:
        self.generation = 0
        self.writes: list[tuple[Path, bytes]] = []

    def replace(self, target: Path, payload: bytes) -> DurabilityReceipt:
        self.generation += 1
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        self.writes.append((target, payload))
        future: Future[int] = Future()
        future.set_result(self.generation)
        return DurabilityReceipt(self.generation, future)


_VOLATILE_KEYS = {
    "events",
    "_uid_idx",
    "_has_final",
    "_panel_anchor_cache",
    "isStreaming",
    "draft_input",
    "draft_input_seq",
    "draft_images",
    "last_opened_at",
    "_content_dirty",
    messages_delta_compaction.PRECOMPUTED_REVISION_KEY,
}


def _volatile_paths(value: object, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in _VOLATILE_KEYS:
                found.append(child_path)
            found.extend(_volatile_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_volatile_paths(child, f"{path}[{index}]"))
    return found


def _without_volatile(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_volatile(child)
            for key, child in value.items()
            if key not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_without_volatile(child) for child in value]
    return value


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


def _capture_thin_snapshot_invariant(root: dict, sid: str) -> tuple[bool, str]:
    msg = root["messages"][-1]
    original_workers = msg.get("workers")
    original_forks = root.get("forks")
    msg["workers"] = [
        {
            "id": "volatile-worker",
            "events": [_native_event("worker-volatile")],
            "_uid_idx": {"worker-volatile": 0},
            "_has_final": True,
        }
    ]
    root["forks"] = [
        {
            "id": "volatile-fork",
            "parent_session_id": sid,
            "forks": [],
            "messages": [
                {
                    "id": "volatile-fork-message",
                    "role": "assistant",
                    "content": "nested",
                    "events": [_native_event("fork-volatile")],
                    "_uid_idx": {"fork-volatile": 0},
                    "_panel_anchor_cache": {"cached": True},
                    "isStreaming": True,
                }
            ],
            "draft_input": "volatile",
            "last_opened_at": "2030-01-02T03:04:05",
        }
    ]
    baseline = _without_volatile(root)
    assert isinstance(baseline, dict)
    capture = _CaptureWriter()
    session_path = session_store._session_path(sid)
    owner_thread = threading.get_ident()
    real_writer = session_store._get_durability_writer

    def writer_for_current_thread():
        if threading.get_ident() == owner_thread:
            return capture
        return real_writer()

    try:
        with patch(
            "session_store._get_durability_writer",
            side_effect=writer_for_current_thread,
        ):
            session_store.write_session_full(
                baseline,
                bump_updated_at=False,
                already_persistable=True,
            )
            session_store.write_session_full(root, bump_updated_at=False)
    finally:
        if original_workers is None:
            msg.pop("workers", None)
        else:
            msg["workers"] = original_workers
        if original_forks is None:
            root.pop("forks", None)
        else:
            root["forks"] = original_forks
        session_store.write_session_full(root, bump_updated_at=False)
    session_payloads = [
        payload for target, payload in capture.writes
        if target == session_path
    ]
    decoded_payload = (
        json.loads(session_payloads[-1])
        if session_payloads
        else {}
    )
    volatile_paths = _volatile_paths(decoded_payload)
    ok = (
        len(session_payloads) == 2
        and session_payloads[0] == session_payloads[1]
        and not volatile_paths
        and len(msg.get("events") or []) == 3000
    )
    detail = (
        f"payloads={len(session_payloads)} "
        f"bytes={[len(payload) for payload in session_payloads]} "
        f"volatile_paths={volatile_paths}"
    )
    return ok, detail


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []
    source = open(session_store.__file__, "r", encoding="utf-8").read()
    upsert_start = source.index("def _upsert_summary(")
    upsert_end = source.index("def _drafts_path(", upsert_start)
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
    for timer in (
        "session.persist_root.draft_store",
        "session.persist_root.draft_dirty",
        "session.persist_root.draft_write",
    ):
        if timer not in persist_source:
            raise AssertionError(f"missing persist-root timer {timer}")
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

    # Capture the exact persistence payload with and without the live-only
    # event state. The capture boundary acknowledges immediately but still
    # exercises the real indexing, stripping, encoding, signature, summary,
    # and projection path.
    root = session_manager.get_ref(sid)
    thin_snapshot_ok, thin_snapshot_detail = _capture_thin_snapshot_invariant(
        root, sid,
    )
    results.append(
        (
            "3000 live events produce the exact thin snapshot bytes",
            thin_snapshot_ok,
            thin_snapshot_detail,
        )
    )

    version_before = session_store.summary_version()

    def fail_summary_rewrite(_root_id, _summary):
        raise AssertionError("unchanged summary sidecar was rewritten")

    with patch("session_store._write_summary_file", side_effect=fail_summary_rewrite):
        session_store.write_session_full(root, bump_updated_at=False)
    version_after = session_store.summary_version()
    results.append(
        (
            "unchanged write does not rewrite summary projection",
            version_after == version_before,
            f"version before={version_before} after={version_after}",
        )
    )
    original_touch = session_store._touch_summary_file_current
    touch_mtimes: list[int | None] = []

    def track_touch(root_id, *, root_mtime_ns=None):
        touch_mtimes.append(root_mtime_ns)
        return original_touch(root_id, root_mtime_ns=root_mtime_ns)

    with patch("session_store._touch_summary_file_current", side_effect=track_touch):
        session_store.write_session_full(root, bump_updated_at=False)
    results.append(
        (
            "unchanged summary refresh reuses write-path mtime",
            bool(touch_mtimes) and all(mtime is not None for mtime in touch_mtimes),
            f"touch_mtimes={touch_mtimes}",
        )
    )

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

    root["draft_input"] = "tail-draft"
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
            and "draft_input" not in tail_disk
            and "events" not in tail_msg
            and "isStreaming" not in tail_msg
            and "_uid_idx" not in tail_msg
            and root.get("draft_input") == "tail-draft"
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
    root.pop("draft_input", None)
    msg.pop("isStreaming", None)
    msg.pop("_uid_idx", None)
    msg.pop("events", None)

    # Alternating write/read smoke metric for the authoritative session-detail
    # tree owner used by the REST route. This is deliberately not a contention
    # proof: the persistence coordinator's lock-release contract is covered by
    # its lifecycle integration suite.
    rest_latencies: list[float] = []
    for _ in range(30):
        session_manager.set_pinned(sid, True)
        session_manager.flush_root_persist(sid)
        t0 = time.perf_counter()
        tree = session_detail_api._session_detail_snapshot_sync(
            sid,
            msg_limit=50,
            exchange_count=None,
            include_cache_key=True,
        )
        rest_latencies.append((time.perf_counter() - t0) * 1000.0)
        assert tree is not None
        assert tree.get("id") == sid
        assert tree.get("_detail_response_cache_key_parts")
        session_manager.set_pinned(sid, False)
        session_manager.flush_root_persist(sid)

    rest_latencies.sort()
    p95 = rest_latencies[int(len(rest_latencies) * 0.95)]
    # The 200ms smoke ceiling catches gross regressions in the current
    # stubbed-tree snapshot/cache path without measuring the retired full-event
    # paginated clone path.
    results.append(
        (f"REST p95 < 200ms across alternating writes",
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
        session_manager.flush_pending_persists()
        session_store._summary_sidecar_write_queue.join()
        session_store.shutdown_durability_writer()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
