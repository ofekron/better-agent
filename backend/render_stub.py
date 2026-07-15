"""Render-tree message stubbing for lazy event fetch (Tier 1).

Collapses COMPLETED assistant messages to a lightweight
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

import hashlib
import json
from typing import Any, Optional

_TEXT_EVENT_TYPES = {
    "assistant_text",
    "output_text",
    "text",
    "text_delta",
    "text_group",
}
_TEXT_KEYS = ("text", "content", "message")


def _visible_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_visible_text(item) for item in value]
        return "".join(part for part in parts if part)
    if isinstance(value, dict):
        if value.get("type") in {"text", "output_text"}:
            return _visible_text(value.get("text"))
    return ""


def _event_text(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    data = event.get("data")
    if event_type == "agent_message" and isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, dict):
            return _visible_text(message.get("content"))
        return _visible_text(message)
    if event_type not in _TEXT_EVENT_TYPES:
        return ""
    if isinstance(data, dict):
        for key in _TEXT_KEYS:
            text = _visible_text(data.get(key))
            if text:
                return text
    return _visible_text(data)


def event_display_summary(event: dict[str, Any]) -> str:
    return _event_text(event)[:160]


def assistant_display_summary(message: dict[str, Any]) -> str:
    return _visible_text(message.get("content"))[:160]


def _content_revision(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def historical_root_revision(message: dict[str, Any]) -> str:
    stub = message.get("stub")
    event_count = (
        int(stub.get("event_count") or 0)
        if isinstance(stub, dict)
        else len(message.get("events") or [])
    )
    return _content_revision({
        "message_id": message.get("id"),
        "seq": message.get("seq"),
        "event_count": event_count,
        "worker_count": len(message.get("workers") or []),
    })


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
_PANEL_ANCHOR_CACHE = "_panel_anchor_cache"
_STUB_ALWAYS_INCLUDE_TYPES = frozenset({"steer_prompt"})


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


def _tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_tool_result_text(item) for item in content)
    if isinstance(content, dict):
        return "\n".join(
            text for text in (_tool_result_text(value) for value in content.values())
            if text
        )
    return ""


def _tool_results_by_id(manager_events: list) -> dict:
    results: dict = {}
    for ev in manager_events:
        if not isinstance(ev, dict) or ev.get("type") != "agent_message":
            continue
        data = ev.get("data")
        if not isinstance(data, dict) or data.get("type") != "user":
            continue
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if isinstance(tool_use_id, str):
                results[tool_use_id] = _tool_result_text(block.get("content"))
    return results


def _delegation_tool_uses(manager_events: list) -> list:
    """Delegation tool_use blocks in firing order, each tagged with the
    index of the event entry that holds it (parallel asks in one entry
    share that index)."""
    results = _tool_results_by_id(manager_events)
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
                tool_use_id = block.get("id")
                out.append({
                    "entry_index": entry_index,
                    "short": short,
                    "tool_use_id": tool_use_id if isinstance(tool_use_id, str) else None,
                    "result_text": results.get(tool_use_id) if isinstance(tool_use_id, str) else None,
                })
    return out


def _creation_result_matches(tool_use: dict, worker: dict) -> bool:
    session_id = worker.get("worker_session_id")
    result_text = tool_use.get("result_text")
    if not isinstance(session_id, str) or not session_id.strip():
        return True
    if result_text is None:
        return True
    return session_id.strip() in result_text


def _panel_matches_tool(tool_use: dict, worker: dict) -> bool:
    short = tool_use.get("short")
    kind = worker.get("panel_kind")
    run_mode = worker.get("run_mode")
    if short == "create_sub_session":
        return kind == "sub_session_created" and _creation_result_matches(tool_use, worker)
    if short == "create_session":
        return kind == "session_created" and _creation_result_matches(tool_use, worker)
    if short == "ask":
        return run_mode in ("team_ask", "fork", "team_message")
    if short in ("mssg", "delegate_task"):
        return run_mode == "team_message"
    return False


def _should_skip_unmatched_tool_use(tool_use: dict) -> bool:
    return (
        tool_use.get("short") in ("create_session", "create_sub_session")
        and tool_use.get("result_text") is not None
    )


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
        while cursor < len(tool_uses):
            tool_use = tool_uses[cursor]
            if not _panel_matches_tool(tool_use, worker):
                if not _should_skip_unmatched_tool_use(tool_use):
                    break
                cursor += 1
                continue
            cursor += 1
            anchors[worker.get("delegation_id")] = tool_use["entry_index"] + 1
            break
    return anchors


def _panel_anchor_cache_key(manager_events: list, workers: list) -> tuple:
    return (
        len(manager_events),
        tuple(
            (
                worker.get("delegation_id"),
                worker.get("panel_kind"),
                worker.get("run_mode"),
            )
            for worker, _index in workers
        ),
    )


def invalidate_panel_anchor_cache(msg: dict) -> None:
    msg.pop(_PANEL_ANCHOR_CACHE, None)


def _panel_anchors(msg: dict, manager_events: list, workers: list) -> dict:
    key = _panel_anchor_cache_key(manager_events, workers)
    cached = msg.get(_PANEL_ANCHOR_CACHE)
    if isinstance(cached, dict) and cached.get("key") == key:
        anchors = cached.get("anchors")
        if isinstance(anchors, dict):
            return anchors
    anchors = _derive_panel_anchors(manager_events, workers)
    msg[_PANEL_ANCHOR_CACHE] = {"key": key, "anchors": anchors}
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

    anchors = _panel_anchors(msg, manager_events, workers)

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


def stub_preview_events(rendered: list, tail: int) -> list:
    tail_events = rendered[-tail:] if tail > 0 else []
    tail_ids = {id(e) for e in tail_events}
    pinned = [
        e for e in rendered
        if isinstance(e, dict)
        and e.get("type") in _STUB_ALWAYS_INCLUDE_TYPES
        and id(e) not in tail_ids
    ]
    return pinned + tail_events


def build_stub(msg: dict, *, tail: int = STUB_TAIL) -> dict:
    """Compute `{event_count, last_events}` from a msg's timeline.
    `last_events` references live event dicts — caller deepcopies if it
    will outlive the live tree."""
    rendered = timeline_events(msg)
    existing = msg.get("stub") if isinstance(msg.get("stub"), dict) else {}
    direct_child_count = existing.get("direct_child_count")
    historical_revision = existing.get("historical_revision")
    if not isinstance(direct_child_count, int) or direct_child_count < 0:
        direct_child_count = len(msg.get("events") or []) + len(msg.get("workers") or [])
    if not isinstance(historical_revision, str) or not historical_revision:
        historical_revision = historical_root_revision(msg)
    return build_stub_projection(
        event_count=len(rendered),
        direct_child_count=direct_child_count,
        historical_revision=historical_revision,
        last_events=stub_preview_events(rendered, tail),
    )


def build_stub_projection(
    *, event_count: int, direct_child_count: int, historical_revision: str, last_events: list,
) -> dict:
    return {
        "event_count": event_count,
        "direct_child_count": direct_child_count,
        "historical_revision": historical_revision,
        "last_events": last_events,
    }


def build_stub_from_events(events: list, *, tail: int = STUB_TAIL) -> dict:
    """Compute a stub from an explicit primary events list."""
    rendered = _renderable(events)
    return {"event_count": len(rendered), "last_events": stub_preview_events(rendered, tail)}


def message_output_text(msg: dict) -> str:
    from event_shape import extract_output_text, strip_synthetic_events

    return extract_output_text(strip_synthetic_events(primary_events(msg)))


def latest_assistant_id(msgs: list) -> Optional[str]:
    """Id of the most-recent assistant message in a node's message list.
    Max by `seq`; falls back to last-in-order when seqs are absent."""
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
    invalidate_panel_anchor_cache(msg)
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
