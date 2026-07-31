"""Session-status WS mapping + restart persistence gaps.

Three status dimensions had no coverage on the paths that make them
visible and durable. Each check here fails if that path regresses:

  1. `error_changed` must reach open clients as a `session_error_changed`
     frame carrying the project routing key. This is the SESSION-level
     error dot (distinct from `assistant_error_set` →
     `message_error_changed`, which is per message).
  2. A pending user-facing MCP request must survive a backend restart —
     otherwise a session that is genuinely waiting on the user reads as
     idle after a restart, which is exactly the "indicator went stale"
     failure.
  3. The ALL_TASKS__DONE attention marker must survive a restart, like
     the other markers.

Run: cd backend && .venv/bin/python scripts/test_session_status_ws_and_persistence.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home  # noqa: E402

_HOME = _test_home.isolate("session-status-ws-persistence")

import session_status  # noqa: E402
import session_store  # noqa: E402
import session_ws_broadcaster as swb  # noqa: E402
import user_input_store  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    if not cond:
        failures.append(name)
        print(f"FAIL {name}")
    else:
        print(f"ok   {name}")


# ── 1. Session-level error reaches clients ───────────────────────────────
check("error_changed.not_internal", "error_changed" not in swb._INTERNAL_KINDS)

broadcaster = swb.SessionWSBroadcaster(coordinator=None)
frames: list[dict] = []
broadcaster._dispatch = frames.append  # capture instead of WS fanout
broadcaster.on_change("sid-err", {"kind": "error_changed", "has_error": True})

check("error_changed.dispatches_frame", bool(frames))
if frames:
    frame = frames[0]
    check("error_changed.frame_type", frame["type"] == "session_error_changed")
    check("error_changed.session_id", frame["data"]["session_id"] == "sid-err")
    check("error_changed.has_error_true", frame["data"]["has_error"] is True)
    # The frontend routes the delta to a project by (cwd, node_id); a
    # frame without them cannot update the project aggregate.
    check("error_changed.carries_cwd", "cwd" in frame["data"])
    check("error_changed.carries_node_id", "node_id" in frame["data"])

frames.clear()
broadcaster.on_change("sid-err", {"kind": "error_changed", "has_error": False})
check(
    "error_changed.clear_dispatches_false",
    bool(frames) and frames[0]["data"]["has_error"] is False,
)


# ── 2. Pending user-input survives a restart ─────────────────────────────
SID = "sid-waiting"


def reload_user_input_store() -> None:
    """Drop every in-memory cache and re-read from disk — the state a
    fresh backend process would see."""
    import importlib
    importlib.reload(user_input_store)


user_input_store.create_request(
    app_session_id=SID,
    questions=[{"id": "q1", "text": "Pick one", "options": ["a", "b"]}],
    prompt="Pick one",
    timeout_seconds=None,
)

check(
    "pending_input.counted_before_restart",
    user_input_store.pending_counts_by_session().get(SID, 0) == 1,
)
reload_user_input_store()
check(
    "pending_input.persists_across_restart",
    user_input_store.pending_counts_by_session().get(SID, 0) == 1,
)
# And it drives the waiting-on-user dimension.
check(
    "pending_input.sets_waiting_dimension",
    session_status.compute(
        {"id": SID}, {SID: "stopped"}, {},
        user_input_store.pending_counts_by_session(),
    ).waiting_for_user,
)


# ── 3. ALL_TASKS__DONE marker survives a restart ─────────────────────────
DONE_SID = "sid-done"
session_store.set_marker_projection(
    DONE_SID,
    "user-attention",
    {
        "color": "#08f",
        "tooltip": "All tasks done",
        "tag": session_status.MARKER_TAG_ALL_TASKS_DONE,
    },
)

session_store._reset_home_scoped_caches()  # the restart-equivalent path

markers = session_store._markers_for_session(DONE_SID)
check(
    "done_marker.tag_persists_across_restart",
    (markers.get("user-attention") or {}).get("tag")
    == session_status.MARKER_TAG_ALL_TASKS_DONE,
)
restored = session_status.compute({"id": DONE_SID, "markers": markers}, {}, {})
check("done_marker.sets_is_done_dimension", restored.is_done)
# The done tag must not bleed into any other dimension.
check("done_marker.does_not_set_waiting", not restored.waiting_for_user)
check("done_marker.does_not_set_errored", not restored.errored)
check("done_marker.does_not_set_running", restored.running == session_status.IDLE)

# The same restart must not resurrect a cleared done marker.
session_store.set_marker_projection(DONE_SID, "user-attention", None)
session_store._reset_home_scoped_caches()
check(
    "done_marker.clear_persists_across_restart",
    not session_status.compute(
        {"id": DONE_SID, "markers": session_store._markers_for_session(DONE_SID)}, {}, {}
    ).is_done,
)


print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all status WS + persistence checks passed")
