from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home  # noqa: E402

_test_home.isolate("session-status-projection")

import session_listing_api  # noqa: E402
import session_status  # noqa: E402
import session_status_projection  # noqa: E402
import user_input_api  # noqa: E402
import user_input_store  # noqa: E402
from global_events import validate_global_event  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


class _Coordinator:
    async def broadcast_global(self, _event_type: str, _payload: dict) -> None:
        return None


async def _run() -> None:
    assert validate_global_event(
        "session_status_changed",
        {"session_id": "sid", "status_key": "idle", "status_rank": 0},
    )["status_key"] == "idle"
    session = session_manager.create(
        name="status",
        model="sonnet",
        cwd="/tmp/status-projection",
        orchestration_mode="native",
        source="cli",
    )
    sid = session["id"]
    monitoring = {sid: "stopped"}
    snapshot = (set(), monitoring, {}, {})
    session_row = {
        **session_manager.get_lite(sid),
        "node_id": "primary",
        "source": "cli",
    }
    row = session_listing_api._decorate_local_sidebar_sessions(
        [session_row],
        snapshot,
    )[0]
    expected = session_status.project(session_row, monitoring, {}, {})
    assert row["status_key"] == expected["status_key"]
    assert row["status_rank"] == expected["status_rank"]

    frames: list[tuple[str, dict]] = []
    facts: list[dict] = []

    async def broadcast(event_type: str, payload: dict) -> None:
        frames.append((event_type, payload))

    async def capture_fact(event: BusEvent) -> None:
        facts.append(event.payload)

    session_status_projection.bind(
        broadcast,
        lambda: (set(), dict(monitoring)),
    )
    bus.unsubscribe("test_session_status_projection")
    bus.subscribe(
        "session.status_projected",
        capture_fact,
        priority=60,
        name="test_session_status_projection",
    )
    try:
        regular_kinds = session_status_projection.STATUS_INPUT_KINDS - {
            "deleted",
            "user_input_changed",
        }
        for kind in regular_kinds:
            frames.clear()
            facts.clear()
            await bus.publish(BusEvent(
                type=f"session.{kind}",
                root_id=sid,
                sid=sid,
                payload={"kind": kind},
                persist=False,
            ))
            assert frames == [(
                "session_status_changed",
                {"session_id": sid, **expected},
            )], kind
            assert facts[-1]["session_id"] == sid
            assert facts[-1]["project_key"] == {
                "cwd": "/tmp/status-projection",
                "node_id": "primary",
            }

        user_input_api.configure(coordinator=_Coordinator())
        user_input_store.create_request(
            app_session_id=sid,
            questions=[{"id": "q", "text": "Choose", "options": ["yes", "no"]}],
            prompt="Choose",
            timeout_seconds=None,
        )
        frames.clear()
        facts.clear()
        await user_input_api._broadcast_user_input_state(sid)
        assert frames
        event_type, payload = frames[-1]
        assert event_type == "session_status_changed"
        assert payload["session_id"] == sid
        assert payload["status_key"] == "needs_decision"
        assert payload["status_rank"] == 5
        assert facts[-1]["waiting_for_user"] is True

        facts.clear()
        await bus.publish(BusEvent(
            type="session.deleted",
            root_id=sid,
            sid=sid,
            payload={"kind": "deleted", "deleted_sids": [sid]},
            persist=False,
        ))
        assert facts == [{"session_id": sid, "deleted": True}]
    finally:
        bus.unsubscribe("test_session_status_projection")
        session_status_projection.unbind()


asyncio.run(_run())
PROJECTION_CONVERGED = True


def test_session_status_projection_converges() -> None:
    assert PROJECTION_CONVERGED
