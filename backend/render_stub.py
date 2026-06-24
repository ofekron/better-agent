"""Render-tree message stubbing for lazy event fetch (Tier 1).

Collapses COMPLETED, non-latest assistant messages to a lightweight
stub on the heavy read paths (REST snapshot, WS replay, older-message
loading). The full events are fetched on demand when the user expands
a turn, via `session_manager.get_message_full` /
`GET /api/sessions/{id}/messages/{msgId}/events`.

READ-SIDE ONLY: this never touches ingestion or `apply_event`. A stub
preserves every message metadata field and only empties the events
lists (`msg.events` / `msg.workers[*].events`),
attaching `msg["stub"] = {event_count, last_events}`.

`event_count` / `last_events` are derived from the same manager/worker
timeline the frontend expands: primary events plus worker-panel events
interleaved by the backend-stamped `insert_at` delegation point.
"""

from typing import Optional

# Lifecycle / non-rendered event types excluded from the collapsed
# count + preview. Mirrors the frontend:
#   - CollapsibleTimelineBlock: drops {complete, session_discovered}
#   - AssistantMessage manager mode: also strips worker_prep_*
_NON_RENDER_TYPES = frozenset({
    "complete",
    "session_discovered",
    "worker_prep_start",
    "worker_prep_event",
    "worker_prep_complete",
    "worker_prep_cancelled",
})

STUB_TAIL = 25


def primary_events(msg: dict) -> list:
    """The primary events list: `msg.events`. Read-only; returns the
    live list reference."""
    return msg.get("events") or []


def _renderable(events: list) -> list:
    return [
        e for e in events
        if isinstance(e, dict) and e.get("type") not in _NON_RENDER_TYPES
    ]


def _worker_events(worker: dict) -> list:
    return worker.get("events") or []


def timeline_events(msg: dict) -> list:
    manager_events = primary_events(msg)
    workers = []
    seen_delegation_ids = set()
    for index, worker in enumerate(msg.get("workers") or []):
        if not isinstance(worker, dict):
            continue
        delegation_id = worker.get("delegation_id")
        if not delegation_id or delegation_id in seen_delegation_ids:
            continue
        seen_delegation_ids.add(delegation_id)
        workers.append((worker, index))
    if not workers:
        return _renderable(manager_events)

    ordered = sorted(
        workers,
        key=lambda item: (
            item[0].get("insert_at")
            if isinstance(item[0].get("insert_at"), (int, float))
            else float("inf"),
            item[1],
        ),
    )
    out = []
    manager_index = 0

    def _append_renderable(events: list) -> None:
        out.extend(_renderable(events))

    for worker, _index in ordered:
        insert_at = worker.get("insert_at")
        if not isinstance(insert_at, (int, float)):
            insert_at = float("inf")
        stop = (
            len(manager_events)
            if insert_at == float("inf")
            else min(int(insert_at), len(manager_events))
        )
        _append_renderable(manager_events[manager_index:stop])
        manager_index = stop
        _append_renderable(_worker_events(worker))
    _append_renderable(manager_events[manager_index:])
    return out


def renderable_count(msg: dict) -> int:
    """Count of renderable expanded-timeline events."""
    return len(timeline_events(msg))


def build_stub(msg: dict, *, tail: int = STUB_TAIL) -> dict:
    """Compute `{event_count, last_events}` from a msg's timeline.
    `last_events` references live event dicts — caller deepcopies if it
    will outlive the live tree."""
    rendered = timeline_events(msg)
    return {"event_count": len(rendered), "last_events": rendered[-tail:]}


def build_stub_from_events(events: list, *, tail: int = STUB_TAIL) -> dict:
    """Compute a stub from an explicit primary events list."""
    rendered = _renderable(events)
    return {"event_count": len(rendered), "last_events": rendered[-tail:]}


def message_output_text(msg: dict) -> str:
    from event_shape import extract_output_text, strip_synthetic_events

    return extract_output_text(strip_synthetic_events(timeline_events(msg)))


def latest_assistant_id(msgs: list) -> Optional[str]:
    """Id of the most-recent assistant message in a node's message list.
    Max by `seq`; falls back to last-in-order when seqs are absent. This
    msg is kept FULL (auto-expanded on the frontend); all earlier
    assistant msgs are stubbed."""
    latest_id: Optional[str] = None
    latest_seq: Optional[int] = None
    for m in msgs:
        if m.get("role") != "assistant" or not m.get("id"):
            continue
        seq = m.get("seq")
        if latest_id is None:
            latest_id, latest_seq = m["id"], seq
            continue
        if seq is not None and (latest_seq is None or seq >= latest_seq):
            latest_id, latest_seq = m["id"], seq
    return latest_id


def _empty_event_lists(msg: dict) -> None:
    """Empty every events list on a msg + drop the uid indexes."""
    if isinstance(msg.get("events"), list):
        msg["events"] = []
    msg.pop("_uid_idx", None)
    for w in msg.get("workers") or []:
        if isinstance(w, dict):
            if isinstance(w.get("events"), list):
                w["events"] = []
            w.pop("_uid_idx", None)


def stub_message_inplace(msg: dict, *, tail: int = STUB_TAIL) -> dict:
    """Stub an assistant msg IN PLACE (mutates). Computes the stub from
    the msg's CURRENT events, then empties the events lists. For use on
    an ALREADY-COPIED msg (e.g. a throwaway deepcopy) — never call on a
    live cached msg without the pop/restore guard. Returns the msg."""
    msg["stub"] = build_stub(msg, tail=tail)
    _empty_event_lists(msg)
    return msg
