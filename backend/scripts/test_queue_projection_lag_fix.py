from __future__ import annotations

import asyncio
import os
import shutil
import sys

import pytest

import _test_home
_TMP_HOME = _test_home.isolate("ba-test-queue-projection-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import lifecycle_command_store  # noqa: E402
import recovery  # noqa: E402
import session_detail_api  # noqa: E402
import session_queue_projection  # noqa: E402
import session_store  # noqa: E402
from hot_path_executor import session_detail_path  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

# re-enqueue now reconciles against the lifecycle command store; initialize it
# under the isolated home so transition_for() can open its database.
lifecycle_command_store.initialize()

pytestmark = pytest.mark.anyio


class _LifecycleSnapshot:
    identity = None
    execution = None


class _LifecycleCommands:
    def snapshot(self, _sid: str) -> _LifecycleSnapshot:
        return _LifecycleSnapshot()


class _Coordinator:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict]] = []
        self.lifecycle_commands = _LifecycleCommands()

    def is_prompt_item_in_flight(self, sid: str, item_id: str) -> bool:
        return False

    async def submit_prompt_async(
        self,
        sid: str,
        params: dict,
        **_kwargs,
    ) -> str:
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


async def test_reenqueue_uses_projection_without_full_root_load() -> None:
    sid = _make_session()
    session_manager.add_queued_prompt(sid, _queued_prompt())
    session_manager.flush_pending_persists()
    session_queue_projection.rebuild_from_disk()

    original_get_root_tree = session_store.get_root_tree
    original_coordinator = recovery._coordinator_ref
    coordinator = _Coordinator()
    recovery._coordinator_ref = coordinator

    def fail_get_root_tree(*_args, **_kwargs):
        raise AssertionError("re-enqueue must not cold-load full root trees")

    session_store.get_root_tree = fail_get_root_tree
    try:
        await recovery._re_enqueue_queued_prompts()
    finally:
        session_store.get_root_tree = original_get_root_tree
        recovery._coordinator_ref = original_coordinator

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
    session_manager.flush_pending_persists()
    assert ok, "re-enqueue must use queue projection without cold-loading root trees"


async def test_reenqueue_dedupes_from_projection() -> None:
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

    original_coordinator = recovery._coordinator_ref
    coordinator = _Coordinator()
    recovery._coordinator_ref = coordinator
    try:
        await recovery._re_enqueue_queued_prompts()
    finally:
        recovery._coordinator_ref = original_coordinator

    queued = session_queue_projection.queued_prompts(sid)
    assert coordinator.submitted == [] and queued == [], (
        "re-enqueue must dedupe against projected user keys"
    )


async def test_get_session_context_scan_is_off_thread() -> None:
    sid = _make_session()
    original_tree = session_manager.get_root_tree_stubbed

    def slow_tree(_sid: str, msg_limit: int = 50, exchange_count=None) -> dict:
        import time
        time.sleep(0.2)
        return {"id": _sid, "messages": []}

    session_manager.get_root_tree_stubbed = slow_tree
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(heartbeat())
    try:
        result = await session_detail_path.run(
            "sessions.detail.worker",
            session_detail_api._session_detail_snapshot_sync,
            sid,
            msg_limit=50,
            exchange_count=None,
            include_cache_key=False,
        )
    finally:
        session_manager.get_root_tree_stubbed = original_tree
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert result is not None, "detail snapshot must return the session tree"
    assert ticks >= 5, "detail snapshot scan must run off the event loop"


if __name__ == "__main__":
    async def _run() -> None:
        await test_reenqueue_uses_projection_without_full_root_load()
        await test_reenqueue_dedupes_from_projection()
        await test_get_session_context_scan_is_off_thread()

    try:
        asyncio.run(_run())
    finally:
        session_manager.flush_pending_persists()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
