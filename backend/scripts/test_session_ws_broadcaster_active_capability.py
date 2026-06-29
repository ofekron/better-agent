from __future__ import annotations

import os
import sys

import _test_home

_test_home.isolate("bc-test-ws-active-capability-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
from session_ws_broadcaster import SessionWSBroadcaster  # noqa: E402


def test_active_capability_changes_emit_metadata_patch() -> None:
    captured: list[dict] = []

    class StubCoordinator:
        async def noop(self) -> None:
            return None

        def broadcast_global(self, type_: str, data: dict):
            captured.append({"type": type_, "data": data})
            return self.noop()

    sess = session_manager.create(name="active-capability", cwd="/tmp")
    sid = sess["id"]
    session_manager.add_active_capability(sid, "ofek.testape:testape")

    broadcaster = SessionWSBroadcaster(StubCoordinator())
    broadcaster.on_change(sid, {
        "kind": "active_capability_added",
        "capability_id": "ofek.testape:testape",
    })

    assert captured == [{
        "type": "session_metadata_updated",
        "data": {
            "session_id": sid,
            "patch": {"active_capability_ids": ["ofek.testape:testape"]},
            "originated_by": None,
        },
    }]


if __name__ == "__main__":
    test_active_capability_changes_emit_metadata_patch()
    print("ok")
