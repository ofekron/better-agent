"""Canonical per-session status dimensions.

One authoritative derivation of the five independent status dimensions
described in the session-states requirement: Running, Waiting-on-user,
Unread, Errored, and Is-done. Every surface that displays or aggregates
session status projects from `compute` rather than re-deriving from raw
session fields.

The priority-ordered sidebar bucket and rank are projected here too, so
REST rows, live deltas, filters, sorting, and project aggregates share one
formula owner.

The dimensions are independent and combine freely: a session can be
errored, unread, and waiting on the user at once. Only the bucket
projection applies a priority order.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Attention-marker tags emitted by the user-attention extension. The tag
#: rides the marker projection (see file_ref_resolver.detect_markers); we
#: read it rather than match on color/tooltip (which drift).
MARKER_TAG_NEEDS_DECISION = "NEEDS_USER_DECISION"
MARKER_TAG_ALL_TASKS_DONE = "ALL_TASKS__DONE"

#: Values of the Running dimension.
RUNNING = "running"
AWAITING_BACKGROUND = "awaiting_background"
IDLE = "idle"

#: monitoring_state values, as produced by turn_manager.monitoring_state,
#: mapped onto the Running dimension. Every other monitoring value
#: ("stopped", "idle", "blocked_on_user") means the session is not doing
#: foreground work and therefore reads as IDLE on this dimension —
#: blocked_on_user carries its signal on the waiting-on-user dimension
#: instead, so the two never mask one another.
_MONITORING_TO_RUNNING = {
    "active": RUNNING,
    "waiting_on_background": AWAITING_BACKGROUND,
}

SESSION_STATUS_KEYS: tuple[str, ...] = (
    "error",
    "needs_decision",
    "unread",
    "open_work",
    "running",
    "all_done",
    "idle",
)


@dataclass(frozen=True)
class SessionStatus:
    """The five independent dimensions, plus the open-work signal the
    sidebar bucket ordering needs."""

    running: str
    waiting_for_user: bool
    unread: bool
    errored: bool
    is_done: bool
    open_work: bool

    @property
    def busy(self) -> bool:
        """True while the session has foreground or background work
        outstanding — i.e. anything other than IDLE on the Running
        dimension."""
        return self.running != IDLE


def running_dimension(monitoring_state: str | None) -> str:
    """Project a raw monitoring_state onto the Running dimension."""
    return _MONITORING_TO_RUNNING.get(monitoring_state or "", IDLE)


def _marker_tags(session: dict) -> set[str]:
    markers = session.get("markers") or {}
    return {
        (m or {}).get("tag")
        for m in markers.values()
        if isinstance(m, dict)
    }


def _pending_input_count(
    session: dict,
    pending_input_by_sid: dict[str, int] | None,
) -> int:
    sid = session.get("id") or ""
    pending = None
    if pending_input_by_sid is not None:
        pending = pending_input_by_sid.get(sid)
    if pending is None:
        pending = session.get("pending_user_input_count", 0)
    try:
        return max(0, int(pending or 0))
    except (TypeError, ValueError):
        return 0


def _has_open_work_items(session: dict) -> bool:
    items = (
        list(session.get("current_todos") or [])
        + list(session.get("current_tasks") or [])
    )
    return any(
        (item or {}).get("status") != "completed"
        for item in items
        if isinstance(item, dict)
    )


def compute(
    session: dict,
    monitoring_by_sid: dict[str, str],
    unread_by_sid: dict[str, int],
    pending_input_by_sid: dict[str, int] | None = None,
) -> SessionStatus:
    """Derive every status dimension for one session row.

    Live snapshots win over the row's own fields for local sessions
    (whose stored summary has no monitoring_state at read time); the row
    fields are the fallback for remote-node sessions absent from the
    local snapshot.
    """
    sid = session.get("id") or ""
    state = (
        monitoring_by_sid.get(sid)
        or session.get("monitoring_state")
        or "stopped"
    )
    unread = unread_by_sid.get(sid)
    if unread is None:
        unread = session.get("unread_count", 0)
    tags = _marker_tags(session)
    return SessionStatus(
        running=running_dimension(state),
        waiting_for_user=(
            state == "blocked_on_user"
            or _pending_input_count(session, pending_input_by_sid) > 0
            or MARKER_TAG_NEEDS_DECISION in tags
        ),
        unread=(unread or 0) > 0,
        errored=bool(session.get("has_error") or session.get("unseen_error")),
        is_done=MARKER_TAG_ALL_TASKS_DONE in tags,
        open_work=_has_open_work_items(session),
    )


def key_for(status: SessionStatus) -> str:
    if status.errored:
        return "error"
    if status.waiting_for_user:
        return "needs_decision"
    if status.unread and not status.busy:
        return "unread"
    if status.open_work:
        return "open_work"
    if status.busy:
        return "running"
    if status.is_done:
        return "all_done"
    return "idle"


def project_status(status: SessionStatus) -> dict[str, str | int]:
    key = key_for(status)
    return {
        "status_key": key,
        "status_rank": len(SESSION_STATUS_KEYS) - 1 - SESSION_STATUS_KEYS.index(key),
    }


def project(
    session: dict,
    monitoring_by_sid: dict[str, str],
    unread_by_sid: dict[str, int],
    pending_input_by_sid: dict[str, int] | None = None,
) -> dict[str, str | int]:
    return project_status(
        compute(
            session,
            monitoring_by_sid,
            unread_by_sid,
            pending_input_by_sid,
        )
    )
