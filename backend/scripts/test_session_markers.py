"""Locks the generic session-marker projection + WS broadcast + clear-on-view.

Run with:
    cd backend && .venv/bin/python scripts/test_session_markers.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-markers-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from session_manager import manager  # noqa: E402
from session_ws_broadcaster import SessionWSBroadcaster  # noqa: E402
import file_ref_resolver  # noqa: E402
import extension_applied_config  # noqa: E402


class _FakeCoordinator:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    def broadcast_global(self, type_: str, data: dict):
        # Capture synchronously, then return a no-op awaitable so the
        # broadcaster's create_task/loop fallback has something to schedule.
        self.frames.append((type_, dict(data)))

        async def _noop():
            return None

        return _noop()


def main() -> int:
    try:
        sid = manager.create(cwd="/tmp/marker-proj", orchestration_mode="team")["id"]

        coord = _FakeCoordinator()
        bc = SessionWSBroadcaster(coord)
        # No running loop in this sync test; bind a fresh one for fallback.
        loop = asyncio.new_event_loop()
        bc.bind(loop)
        manager.add_listener(bc.on_change)

        marker = {"color": "#ff8c00", "tooltip": "Needs your decision"}
        manager.set_marker(sid, "ext.a", marker)

        # Projection updated.
        assert session_store._markers_for_session(sid) == {"ext.a": marker}, \
            session_store._markers_for_session(sid)

        # WS frame emitted with cwd + node_id.
        sets = [f for f in coord.frames if f[0] == "session_marker_changed"]
        assert sets, f"no marker frame: {coord.frames}"
        data = sets[-1][1]
        assert data["session_id"] == sid
        assert data["extension_id"] == "ext.a"
        assert data["marker"] == marker
        assert data["cwd"] == "/tmp/marker-proj"
        assert data["node_id"] == "primary"

        # Marker appears in the session summary projection.
        summary = session_store._build_summary_for_root(manager.get_ref(sid))
        assert summary["markers"] == {"ext.a": marker}, summary["markers"]

        # clear_marker drops it + fires marker_cleared(null).
        coord.frames.clear()
        manager.clear_marker(sid, "ext.a")
        assert session_store._markers_for_session(sid) == {}
        cleared = [f for f in coord.frames if f[0] == "session_marker_changed"]
        assert cleared and cleared[-1][1]["marker"] is None, coord.frames

        # clear_markers_for_extension purges across sessions + fires per sid.
        manager.set_marker(sid, "ext.a", marker)
        coord.frames.clear()
        manager.clear_markers_for_extension("ext.a")
        assert session_store._markers_for_session(sid) == {}
        assert any(
            f[0] == "session_marker_changed" and f[1]["session_id"] == sid
            for f in coord.frames
        ), coord.frames

        # mark_seen clears a clear_on:view marker.
        records = [{
            "enabled": True,
            "manifest": {
                "id": "ext.a",
                "entrypoints": {"applied_config": {"tag_rules": [{
                    "tag": "NEEDS_USER_DECISION", "strip_wrapper": True,
                    "marker": marker, "clear_on": "view",
                }]}},
            },
        }]
        extension_applied_config._all_enabled_records = lambda: records  # type: ignore

        manager.set_marker(sid, "ext.a", marker)
        assert session_store._markers_for_session(sid) == {"ext.a": marker}
        manager.mark_seen(sid, None)
        assert session_store._markers_for_session(sid) == {}, \
            f"view ack must clear marker: {session_store._markers_for_session(sid)}"

        manager._listeners.remove(bc.on_change)
        loop.close()
        print("PASS test_session_markers")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
