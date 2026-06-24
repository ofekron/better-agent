"""Locks the SessionWSBroadcaster's mapping for the three change kinds:

  • `running_changed` → WS `session_running_changed` (payload carries
    `cwd` + `node_id`; NO `projects_changed` fan-out).
  • `unread_changed` → WS `session_unread_changed` (payload carries
    `cwd` + `node_id`; NO `projects_changed` fan-out).
  • `seen_advanced`  → WS `session_unread_changed{unread_count:0}`
    (payload carries `cwd` + `node_id`; NO `projects_changed`
    fan-out).

INVARIANT: the broadcaster MUST NOT emit `projects_changed` as a
side-effect of session running/unread/seen changes. That was the
refetch-storm cause — frontend now derives per-project aggregates
locally from the per-session deltas. `projects_changed` is reserved
for STRUCTURAL project list mutations (create/delete/touch — emitted
from `main.py`, not this broadcaster).

INVARIANT: payloads include `(cwd, node_id)` so the frontend can
route the delta to the right project aggregate without a session
lookup. For sessions hidden from the sidebar
(`working_mode.should_hide_from_sidebar`) the broadcaster sends
`cwd=""` — frontend treats that as the "skip aggregate" signal,
matching backend's `_project_aggregates` filter (main.py:761).

Also pins the `broadcast_global` allowlist — every new wire-event-type
the broadcaster emits MUST be in `GLOBAL_EVENT_ALLOWLIST` or the
coordinator's enforcement raises ValueError.

Run with:
    cd backend && .venv/bin/python scripts/test_session_ws_broadcaster_unread.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ws-bcast-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as _sm  # noqa: E402
from session_ws_broadcaster import SessionWSBroadcaster  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _StubCoord:
    """Captures broadcast_global calls without an actual WS. Also
    exposes the production allowlist so the dispatch path is exercised
    against the real gate."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.GLOBAL_EVENT_ALLOWLIST = Coordinator.GLOBAL_EVENT_ALLOWLIST

    async def broadcast_global(self, event_type: str, data: dict) -> None:
        if event_type not in self.GLOBAL_EVENT_ALLOWLIST:
            raise ValueError(
                f"broadcast_global called with non-allowlisted type "
                f"{event_type!r}"
            )
        self.calls.append((event_type, data))


def _drive(sid: str, change: dict) -> list[tuple[str, dict]]:
    """Construct a broadcaster, fire one kind through it for the given
    sid, drain the coroutines that the broadcaster schedules onto the
    running loop."""
    stub = _StubCoord()
    bcast = SessionWSBroadcaster(stub)
    loop = asyncio.new_event_loop()
    bcast.bind(loop)
    asyncio.set_event_loop(loop)
    try:
        bcast.on_change(sid, change)
        async def _drain():
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        loop.run_until_complete(_drain())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    return stub.calls


def _create_session(*, cwd: str = "/tmp/proj", working_mode=None) -> str:
    """Create a real session_manager-backed record. The broadcaster's
    `_project_key_for` looks the session up via `manager.get`, so the
    test environment needs an actual record on disk for the cwd /
    node_id resolution to match production."""
    sess = _sm.create(name="test", cwd=cwd, model="gpt", node_id="primary")
    if working_mode is not None:
        # `_sm.create` initializes a plain root; mutate the live root
        # dict in the in-memory `_roots` cache so `_sm.get(sid)` sees
        # the new working_mode without round-tripping to disk. The
        # broadcaster's `_project_key_for` reads via `manager.get`,
        # so updating the cache is sufficient.
        live = _sm._roots.get(sess["id"])  # type: ignore[attr-defined]
        if live is not None:
            live["working_mode"] = working_mode
    return sess["id"]


def test_running_changed_mapping_visible() -> None:
    sid = _create_session(cwd="/tmp/proj-visible")
    calls = _drive(sid, {"kind": "running_changed", "value": True})
    types = [c[0] for c in calls]
    assert types == ["session_running_changed"], (
        f"only session_running_changed expected; got {types}"
    )
    payload = calls[0][1]
    assert payload == {
        "session_id": sid,
        "value": True,
        "cwd": "/tmp/proj-visible",
        "node_id": "primary",
    }, f"payload mismatch: {payload}"
    print(f"{PASS} running_changed_mapping_visible (no projects_changed fan-out)")


def test_unread_changed_mapping_visible() -> None:
    sid = _create_session(cwd="/tmp/proj-unread")
    calls = _drive(sid, {"kind": "unread_changed", "unread_count": 5})
    types = [c[0] for c in calls]
    assert types == ["session_unread_changed"], (
        f"only session_unread_changed expected; got {types}"
    )
    payload = calls[0][1]
    assert payload == {
        "session_id": sid,
        "unread_count": 5,
        "cwd": "/tmp/proj-unread",
        "node_id": "primary",
    }, f"payload mismatch: {payload}"
    print(f"{PASS} unread_changed_mapping_visible (no projects_changed fan-out)")


def test_seen_advanced_mapping_visible() -> None:
    sid = _create_session(cwd="/tmp/proj-seen")
    calls = _drive(sid, {
        "kind": "seen_advanced",
        "last_seen_event_uid": "u-99",
        "unread_count": 0,
    })
    types = [c[0] for c in calls]
    assert types == ["session_unread_changed"], (
        f"only session_unread_changed expected; got {types}"
    )
    payload = calls[0][1]
    assert payload == {
        "session_id": sid,
        "unread_count": 0,
        "last_seen_event_uid": "u-99",
        "cwd": "/tmp/proj-seen",
        "node_id": "primary",
    }, f"payload mismatch: {payload}"
    print(f"{PASS} seen_advanced_mapping_visible (no projects_changed fan-out)")


def test_hidden_session_sends_empty_cwd() -> None:
    """A working_mode-set session must broadcast with `cwd=""` so the
    frontend skips the per-project aggregate mutation. The per-session
    event still fires (the chat view may have the session open even
    though it's not in the sidebar)."""
    sid = _create_session(cwd="/tmp/proj-hidden", working_mode="file_editing")
    calls = _drive(sid, {"kind": "running_changed", "value": True})
    assert len(calls) == 1, f"expected 1 frame, got {len(calls)}: {calls}"
    payload = calls[0][1]
    assert payload["cwd"] == "", (
        f"hidden session must carry cwd=''; got {payload['cwd']!r}"
    )
    assert payload["node_id"] == "primary"
    print(f"{PASS} hidden_session_sends_empty_cwd (FE skips aggregate)")


def test_missing_session_returns_safe_default() -> None:
    """`_project_key_for` must not crash on an unknown sid (race with
    delete). It returns ('', 'primary') — frontend skips aggregate."""
    calls = _drive("sid-does-not-exist", {"kind": "running_changed", "value": True})
    assert len(calls) == 1
    payload = calls[0][1]
    assert payload["cwd"] == ""
    assert payload["node_id"] == "primary"
    print(f"{PASS} missing_session_returns_safe_default")


def test_todos_snapshot_carries_app_session_id() -> None:
    sid = _create_session(cwd="/tmp/proj-todos")
    todos = [{"content": "A", "status": "in_progress"}]
    calls = _drive(sid, {"kind": "todos_snapshot", "todos": todos})
    assert calls == [
        (
            "todos_snapshot",
            {
                "app_session_id": sid,
                "session_id": sid,
                "todos": todos,
            },
        )
    ], f"todos_snapshot payload mismatch: {calls}"
    print(f"{PASS} todos_snapshot_carries_app_session_id")


def test_allowlist_contains_new_types() -> None:
    al = Coordinator.GLOBAL_EVENT_ALLOWLIST
    assert "session_running_changed" in al, "missing in allowlist"
    assert "session_unread_changed" in al, "missing in allowlist"
    assert "active_process_counts_changed" not in al, (
        "legacy active_process_counts_changed must be removed"
    )
    print(f"{PASS} allowlist_contains_new_types")


def test_marker_set_dispatches_allowlisted_frame() -> None:
    """Regression: marker_set → broadcast_global('session_marker_changed').
    Before the fix the type was missing from GLOBAL_EVENT_ALLOWLIST, so
    broadcast_global raised ValueError inside the fire-and-forget task —
    the frame was dropped and the exception showed up only as an
    unretrieved-task log line. With the type allowlisted the frame lands."""
    sid = _create_session(cwd="/tmp/proj-marker")
    marker = {"kind": "attention", "label": "needs-review"}
    calls = _drive(sid, {
        "kind": "marker_set",
        "extension_id": "ext-reviews",
        "marker": marker,
    })
    assert calls == [
        (
            "session_marker_changed",
            {
                "session_id": sid,
                "extension_id": "ext-reviews",
                "marker": marker,
                "cwd": "/tmp/proj-marker",
                "node_id": "primary",
            },
        )
    ], f"marker frame missing/malformed: {calls}"
    print(f"{PASS} marker_set_dispatches_allowlisted_frame")


def main() -> int:
    try:
        test_running_changed_mapping_visible()
        test_unread_changed_mapping_visible()
        test_seen_advanced_mapping_visible()
        test_hidden_session_sends_empty_cwd()
        test_missing_session_returns_safe_default()
        test_todos_snapshot_carries_app_session_id()
        test_allowlist_contains_new_types()
        test_marker_set_dispatches_allowlisted_frame()
        print("ALL PASSED")
        return 0
    except AssertionError as e:
        print(f"{FAIL}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
