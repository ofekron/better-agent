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


# MCP tool short names (suffix after the last `__`) that spawn a panel 1:1
# in the SAME assistant message they fire in. `create_worker` is excluded:
# it's approval-gated and its worker panel appears later via a separate
# delegation (ask/delegate), so its tool_use has no same-message panel and
# would desync the positional match.
_DELEGATION_TOOL_SHORT_NAMES = frozenset({
    "ask",
    "mssg",
    "delegate_task",
    "create_session",
    "create_sub_session",
})


def _tool_short_name(name: str) -> str:
    idx = name.rfind("__")
    return name if idx == -1 else name[idx + 2:]


def _delegation_tool_uses(manager_events: list) -> list:
    """Delegation tool_use blocks in firing order, each tagged with the
    index of the event entry that holds it (parallel asks in one entry
    share that index)."""
    out: list = []
    for entry_index, ev in enumerate(manager_events):
        if not isinstance(ev, dict) or ev.get("type") != "agent_message":
            continue
        data = ev.get("data")
        if not isinstance(data, dict) or data.get("type") != "assistant":
            continue
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if not isinstance(name, str):
                continue
            short = _tool_short_name(name)
            if short in _DELEGATION_TOOL_SHORT_NAMES:
                out.append((entry_index, short))
    return out


def _panel_matches_tool(short: str, worker: dict) -> bool:
    kind = worker.get("panel_kind")
    run_mode = worker.get("run_mode")
    if short == "create_sub_session":
        return kind == "sub_session_created"
    if short == "create_session":
        return kind == "session_created"
    if short == "ask":
        return run_mode in ("team_ask", "fork", "team_message")
    if short in ("mssg", "delegate_task"):
        return run_mode == "team_message"
    return False


def _derive_panel_anchors(manager_events: list, workers: list) -> dict:
    """delegation_id -> anchor index right after the triggering tool_use
    entry. Mirrors frontend `derivePanelAnchors`: panels (in firing order)
    consume compatible delegation tool_use blocks positionally; unmatched
    panels (e.g. Codex native subagents) fall back to stored `insert_at`.
    The backend-stamped `insert_at` is racy (captured before the tool_use
    is tail-appended), so the derived position is authoritative when found."""
    tool_uses = _delegation_tool_uses(manager_events)
    anchors: dict = {}
    cursor = 0
    for worker, _index in workers:
        if cursor < len(tool_uses) and _panel_matches_tool(tool_uses[cursor][1], worker):
            anchors[worker.get("delegation_id")] = tool_uses[cursor][0] + 1
            cursor += 1
    return anchors


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

    anchors = _derive_panel_anchors(manager_events, workers)

    def _anchor_of(worker: dict):
        derived = anchors.get(worker.get("delegation_id"))
        if isinstance(derived, (int, float)):
            return derived
        stored = worker.get("insert_at")
        return stored if isinstance(stored, (int, float)) else float("inf")

    ordered = sorted(
        workers,
        key=lambda item: (_anchor_of(item[0]), item[1]),
    )
    out = []
    manager_index = 0

    def _append_renderable(events: list) -> None:
        out.extend(_renderable(events))

    for worker, _index in ordered:
        insert_at = _anchor_of(worker)
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
