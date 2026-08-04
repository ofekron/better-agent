from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home  # noqa: E402

_test_home.isolate("project-aggregate-projection")

import project_aggregate_projection  # noqa: E402
import project_store  # noqa: E402
import projects_api  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
from global_events import validate_global_event  # noqa: E402


def _payload(
    sid: str,
    cwd: str,
    *,
    running: bool = False,
    unread: bool = False,
    waiting: bool = False,
    errored: bool = False,
) -> dict:
    return {
        "session_id": sid,
        "project_key": {"cwd": cwd, "node_id": "primary"},
        "running": running,
        "unread": unread,
        "waiting_for_user": waiting,
        "errored": errored,
        "status_key": "needs_decision" if waiting else "idle",
        "status_rank": 5 if waiting else 0,
    }


async def _publish(sid: str, payload: dict) -> None:
    await bus.publish(BusEvent(
        type="session.status_projected",
        root_id=sid,
        sid=sid,
        payload=payload,
        persist=False,
    ))


async def _run() -> None:
    frames: list[dict] = []
    active_broadcasts = 0
    max_active_broadcasts = 0
    block_next = False
    broadcast_started = asyncio.Event()
    release_broadcast = asyncio.Event()

    async def broadcast(event_type: str, payload: dict) -> None:
        nonlocal active_broadcasts, max_active_broadcasts, block_next
        assert event_type == "project_aggregates_changed"
        active_broadcasts += 1
        max_active_broadcasts = max(max_active_broadcasts, active_broadcasts)
        if block_next:
            block_next = False
            broadcast_started.set()
            await release_broadcast.wait()
        frames.append(payload)
        active_broadcasts -= 1

    async def noop() -> None:
        return None

    original_list_projects = project_store.list_projects
    project_aggregate_projection.bind(broadcast)
    projects_api.configure(noop, broadcast, project_aggregate_projection.snapshot)
    try:
        await bus.publish(BusEvent(
            type="session.user_input_changed",
            root_id="s1",
            sid="s1",
            payload={"pending_user_input_count": 1},
            persist=False,
        ))
        await project_aggregate_projection.flush()
        assert (await project_aggregate_projection.snapshot())["revision"] == 0

        await _publish("s1", _payload("s1", "/one"))
        await project_aggregate_projection.flush()
        first = frames[-1]
        assert first["revision"] == 1
        assert first["upserts"] == [{
            "path": "/one",
            "node_id": "primary",
            **project_aggregate_projection.empty_aggregate(),
        }]

        project_store.list_projects = lambda: [
            {"path": "/one", "node_id": "primary", "name": "One"},
            {"path": "/two", "node_id": "primary", "name": "Two"},
        ]
        rest = await projects_api.get_projects()
        assert rest["epoch"] == first["epoch"]
        assert rest["revision"] == first["revision"]
        assert rest["projects"][0]["running_count"] == 0

        before = len(frames)
        await _publish("s1", _payload("s1", "/one", waiting=True))
        await _publish("s1", _payload("s1", "/one"))
        await _publish("s1", _payload("s1", "/one", waiting=True))
        await project_aggregate_projection.flush()
        assert len(frames) == before + 1
        pending_frame = frames[-1]
        assert pending_frame["revision"] == 2
        assert pending_frame["upserts"][0]["waiting_for_user_count"] == 1

        await _publish("s1", _payload("s1", "/one", waiting=True))
        await project_aggregate_projection.flush()
        assert len(frames) == before + 1
        assert (await project_aggregate_projection.snapshot())["revision"] == 2

        await _publish("s1", _payload("s1", "/two", waiting=True))
        await project_aggregate_projection.flush()
        moved = frames[-1]
        assert moved["revision"] == 3
        assert moved["upserts"][0]["path"] == "/two"
        assert moved["upserts"][0]["waiting_for_user_count"] == 1
        assert moved["tombstones"] == [{
            "path": "/one",
            "node_id": "primary",
            **project_aggregate_projection.empty_aggregate(),
        }]

        block_next = True
        await _publish("s1", _payload("s1", "/two", waiting=True, unread=True))
        await broadcast_started.wait()
        await _publish(
            "s1",
            _payload("s1", "/two", waiting=True, unread=True, errored=True),
        )
        release_broadcast.set()
        await project_aggregate_projection.flush()
        assert [frame["revision"] for frame in frames[-2:]] == [4, 5]
        assert max_active_broadcasts == 1

        await _publish("s1", {"session_id": "s1", "deleted": True})
        await project_aggregate_projection.flush()
        deleted = frames[-1]
        assert deleted["revision"] == 6
        assert deleted["upserts"] == []
        assert deleted["tombstones"] == [{
            "path": "/two",
            "node_id": "primary",
            **project_aggregate_projection.empty_aggregate(),
        }]
        snapshot = await project_aggregate_projection.snapshot()
        assert snapshot["epoch"] == deleted["epoch"]
        assert snapshot["revision"] == deleted["revision"]
        assert snapshot["aggregates"] == {}

        assert validate_global_event("project_aggregates_changed", deleted) == deleted
    finally:
        project_store.list_projects = original_list_projects
        await project_aggregate_projection.unbind()


def test_project_aggregate_projection() -> None:
    asyncio.run(_run())
