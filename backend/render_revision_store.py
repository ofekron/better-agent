from __future__ import annotations

import copy
import threading
import uuid
from typing import Any

_MAX_ENTRIES = 2048
# Reconnects can replay only within one backend lifetime. A restart creates a
# new incarnation, so clients fail closed to a fresh compact REST snapshot.
_PROCESS_INCARNATION = str(uuid.uuid4())
_states: dict[str, dict[str, Any]] = {}
_guard = threading.RLock()


def _new_state() -> dict[str, Any]:
    return {"revision": 0, "entries": []}


def _state(root_id: str) -> dict[str, Any]:
    return _states.setdefault(root_id, _new_state())


def _delta_for(sid: str, change: dict[str, Any]) -> dict[str, Any]:
    kind = str(change.get("kind") or "unknown")
    delta: dict[str, Any] = {"type": "session_view", "sid": sid, "kind": kind}
    compact_turn = change.get("_compact_turn")
    if isinstance(compact_turn, dict):
        delta = {
            "op": "replace_turn",
            "sid": sid,
            "kind": kind,
            "turn_id": compact_turn.get("id"),
            "turn": copy.deepcopy(compact_turn),
        }
    elif kind in {
        "user_msg_appended",
        "assistant_msg_appended",
        "message_ownership_resolved",
        "completed_at_set",
        "user_msg_marked_error",
    }:
        msg = change.get("msg") or change.get("delta")
        delta = {"type": "message_upsert", "sid": sid, "kind": kind}
        if isinstance(msg, dict):
            from messages_delta_compaction import compact_message_delta_payload
            delta["message"] = compact_message_delta_payload(msg)
        if change.get("msg_id") is not None:
            delta["message_id"] = change["msg_id"]
    elif kind == "assistant_msg_removed":
        replacement = change.get("replacement_turn")
        if isinstance(replacement, dict):
            delta = {
                "op": "replace_turn",
                "sid": sid,
                "kind": kind,
                "turn_id": change.get("previous_turn_id"),
                "turn": copy.deepcopy(replacement),
            }
        else:
            delta = {
                "op": "delete_turn",
                "sid": sid,
                "turn_id": change.get("previous_turn_id"),
            }
    elif kind == "messages_truncated":
        delta = {
            "op": "truncate_after_seq",
            "sid": sid,
            "keep_count": int(change.get("keep_count") or 0),
            "after_seq": change.get("_truncate_after_seq"),
        }
    elif kind == "deleted":
        delta = {"op": "session_delete", "sid": sid}
    else:
        for key, value in change.items():
            if key != "kind":
                delta[key] = copy.deepcopy(value)
    return delta


def append(
    root_id: str, sid: str, change: dict[str, Any],
) -> dict[str, Any] | None:
    from session_ws_broadcaster import _INTERNAL_KINDS
    kind = change.get("kind")
    retained_internal = {"assistant_msg_removed", "messages_truncated"}
    # Live token/tool/content projection stays on WS. The journal fences only
    # structural history and session-view changes needed after the snapshot.
    live_only = {"journal_event_projected", "running_content_updated"}
    if kind in live_only:
        return None
    if kind in _INTERNAL_KINDS and kind not in retained_internal:
        return None
    with _guard:
        state = _state(root_id)
        revision = int(state["revision"]) + 1
        entry = {"revision": revision, "delta": _delta_for(sid, change)}
        entries = state["entries"]
        entries.append(entry)
        if len(entries) > _MAX_ENTRIES:
            del entries[:-_MAX_ENTRIES]
        state["revision"] = revision
        return {
            "incarnation": _PROCESS_INCARNATION,
            "render_revision": revision,
            "delta": copy.deepcopy(entry["delta"]),
        }


def fence(root_id: str) -> dict[str, Any]:
    with _guard:
        state = _state(root_id)
        return {
            "incarnation": _PROCESS_INCARNATION,
            "render_revision": int(state["revision"]),
        }


def replay(
    root_id: str,
    *,
    incarnation: str,
    after_revision: int,
    through_revision: int | None = None,
) -> dict[str, Any]:
    with _guard:
        state = _state(root_id)
        current = int(state["revision"])
        through = current if through_revision is None else int(through_revision)
        failed = {
            "status": "resnapshot_required",
            "incarnation": _PROCESS_INCARNATION,
            "render_revision": current,
        }
        if incarnation != _PROCESS_INCARNATION:
            return failed
        if after_revision < 0 or through < after_revision or through > current:
            return failed
        expected = list(range(after_revision + 1, through + 1))
        by_revision = {
            int(entry["revision"]): entry for entry in state["entries"]
        }
        if any(revision not in by_revision for revision in expected):
            return failed
        return {
            "status": "ok",
            "incarnation": _PROCESS_INCARNATION,
            "from_revision": after_revision,
            "through_revision": through,
            "render_revision": current,
            "entries": [
                copy.deepcopy(by_revision[revision]) for revision in expected
            ],
        }
