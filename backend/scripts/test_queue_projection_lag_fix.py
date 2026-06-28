from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("ba-test-queue-projection-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import session_queue_projection  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _Coordinator:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict]] = []

    async def submit_prompt_async(self, sid: str, params: dict) -> str:
        self.submitted.append((sid, params))
        return params["_queued_id"]


def _queued_prompt(prompt_id: str = "qp-1", client_id: str = "client-1") -> dict:
    return {
        "id": prompt_id,
        "client_id": client_id,
        "content": "hello",
        "images": ["img"],
        "files": ["file"],
        "orchestration_mode": "native",
        "send_target": None,
        "cli_prompt": "hello",
        "capability_contexts": [{"kind": "x"}],
        "alter_rewind_latest": True,
    }


def _make_session() -> str:
    sess = session_manager.create(
        name="queue-projection",
        model="sonnet",
        cwd="/tmp/queue-projection",
        orchestration_mode="native",
        source="cli",
    )
    return sess["id"]


async def test_reenqueue_uses_projection_without_full_root_load() -> bool:
    sid = _make_session()
    session_manager.add_queued_prompt(sid, _queued_prompt())
    session_manager.flush_pending_persists()
    session_queue_projection.rebuild_from_disk()

    original_get_root_tree = session_store.get_root_tree
    original_coordinator = main.coordinator
    coordinator = _Coordinator()
    main.coordinator = coordinator

    def fail_get_root_tree(*_args, **_kwargs):
        raise AssertionError("re-enqueue must not cold-load full root trees")

    session_store.get_root_tree = fail_get_root_tree
    try:
        await main._re_enqueue_queued_prompts()
    finally:
        session_store.get_root_tree = original_get_root_tree
        main.coordinator = original_coordinator

    projected = session_queue_projection.get(sid) or {}
    queued = projected.get("queued_prompts") or []
    lifecycle_id = queued[0].get("lifecycle_msg_id") if queued else None
    submitted = coordinator.submitted[0][1] if coordinator.submitted else {}
    ok = (
        len(coordinator.submitted) == 1
        and bool(lifecycle_id)
        and submitted.get("lifecycle_msg_id") == lifecycle_id
        and submitted.get("images") == ["img"]
        and submitted.get("files") == ["file"]
        and submitted.get("capability_contexts") == [{"kind": "x"}]
        and submitted.get("_alter_rewind_latest") is True
    )
    session_manager.remove_queued_prompt(sid, "qp-1")
    print(f"{PASS if ok else FAIL} re-enqueue uses queue projection")
    return ok


async def test_reenqueue_dedupes_from_projection() -> bool:
    sid = _make_session()
    lifecycle_id = "life-1"
    session_manager.append_user_msg(sid, {
        "role": "user",
        "content": "already sent",
        "client_id": "client-dedupe",
        "lifecycle_msg_id": lifecycle_id,
    })
    session_manager.add_queued_prompt(
        sid,
        {**_queued_prompt("qp-dedupe", "client-dedupe"), "lifecycle_msg_id": lifecycle_id},
    )
    session_manager.flush_pending_persists()
    session_queue_projection.rebuild_from_disk()

    original_coordinator = main.coordinator
    coordinator = _Coordinator()
    main.coordinator = coordinator
    try:
        await main._re_enqueue_queued_prompts()
    finally:
        main.coordinator = original_coordinator

    queued = session_queue_projection.queued_prompts(sid)
    ok = coordinator.submitted == [] and queued == []
    print(f"{PASS if ok else FAIL} re-enqueue dedupes from projected user keys")
    return ok


async def test_get_session_context_scan_is_off_thread() -> bool:
    sid = _make_session()
    original_max_seq = main.event_ingester.max_seq_by_sid
    original_cursor = main.event_ingester.cursor
    original_render_seq = main.event_ingester.render_seq_by_sid
    original_tree = session_manager.get_root_tree_stubbed

    main.event_ingester.cursor = lambda _root_id: 1
    session_manager.get_root_tree_stubbed = (
        lambda _sid, msg_limit=50, exchange_count=None: {"id": sid, "messages": []}
    )

    def slow_max_seq(_root_id: str) -> dict[str, int]:
        import time
        time.sleep(0.2)
        return {sid: 1}

    main.event_ingester.max_seq_by_sid = slow_max_seq
    main.event_ingester.render_seq_by_sid = lambda _root_id: {sid: 1}
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(heartbeat())
    try:
        result = await main.get_session(sid)
    finally:
        task.cancel()
        main.event_ingester.max_seq_by_sid = original_max_seq
        main.event_ingester.cursor = original_cursor
        main.event_ingester.render_seq_by_sid = original_render_seq
        session_manager.get_root_tree_stubbed = original_tree
        try:
            await task
        except asyncio.CancelledError:
            pass

    ok = result.get("max_seq_by_sid", {}).get(sid) == 1 and ticks >= 5
    print(f"{PASS if ok else FAIL} GET session context scan yields to event loop")
    return ok


async def _run() -> bool:
    results = [
        await test_reenqueue_uses_projection_without_full_root_load(),
        await test_reenqueue_dedupes_from_projection(),
        await test_get_session_context_scan_is_off_thread(),
    ]
    print(f"\n{sum(1 for r in results if r)}/{len(results)} passed")
    return all(results)


def main_test() -> int:
    try:
        return 0 if asyncio.run(_run()) else 1
    finally:
        session_manager.flush_pending_persists()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_test())
