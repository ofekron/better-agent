"""Pure-function helpers over claude jsonl event lists.

These reduce a list of `{"type": "agent_message", "data": {...}}`
dicts into the things downstream code needs:

  - `extract_output_text(events)` — concatenated assistant text
  - `strip_synthetic_events(events)` — drop SDK continuation markers
  - `is_synthetic_event(event)` — predicate for the above

Lives apart from `orchestrator.py` so callers that only need event
shape (REST handlers, finalize paths, run_recovery) don't have to
import the whole orchestrator module — and so the orchestrator's
deferred-import dodge from `main.py` goes away.
"""

from __future__ import annotations

RENDER_EVENT_TYPES = frozenset({
    "agent_message",
    "manager_event",
    "model_switched",
    "steer_prompt",
    "lifecycle_notice",
    "worker_event",
})


def _event_uuid(event: dict) -> str | None:
    if not isinstance(event, dict):
        return None
    uid = event.get("uuid")
    if isinstance(uid, str) and uid:
        return uid
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    uid = data.get("uuid")
    if isinstance(uid, str) and uid:
        return uid
    inner = data.get("event")
    if isinstance(inner, dict):
        inner_data = inner.get("data")
        if isinstance(inner_data, dict):
            uid = inner_data.get("uuid")
            if isinstance(uid, str) and uid:
                return uid
    return None


def frontend_event_from_journal_row(
    row: dict, *, include_seq: bool = False,
) -> dict | None:
    event_type = row.get("type")
    if event_type not in RENDER_EVENT_TYPES:
        return None
    data = row.get("data", {})
    if event_type == "manager_event" and isinstance(data, dict):
        inner = data.get("event")
        if not isinstance(inner, dict):
            return None
        event = dict(inner)
    else:
        event = {"type": event_type, "data": data}
    if include_seq:
        event["seq"] = row.get("seq")
    return event


def frontend_events_from_journal_rows(
    rows: list[dict], *, include_seq: bool = False,
) -> list[dict]:
    events: list[dict] = []
    uuid_idx: dict[str, int] = {}
    for row in rows:
        event = frontend_event_from_journal_row(row, include_seq=include_seq)
        if event is None:
            continue
        uid = _event_uuid(event)
        if uid and uid in uuid_idx:
            events[uuid_idx[uid]] = event
            continue
        if uid:
            uuid_idx[uid] = len(events)
        events.append(event)
    return events


def is_synthetic_event(event: dict) -> bool:
    if not isinstance(event, dict):
        return False
    # A manager_event frame wraps the real event one level deeper:
    # {type: "manager_event", data: {event: {type: "agent_message", ...}}}.
    # Unwrap it so synthetic continuation markers carried inside a manager
    # frame are caught too — not just bare agent_message frames. (Mirrors
    # the same unwrap in orchs/base._normalize_for_render / _event_uuid.)
    if event.get("type") == "manager_event":
        data = event.get("data")
        inner = data.get("event") if isinstance(data, dict) else None
        return is_synthetic_event(inner) if isinstance(inner, dict) else False
    data = event.get("data")
    if not isinstance(data, dict):
        return False
    msg = data.get("message")
    return (
        event.get("type") == "agent_message"
        and data.get("type") == "assistant"
        and isinstance(msg, dict)
        and msg.get("model") == "<synthetic>"
        and not data.get("isApiErrorMessage")
    )


# Agent-message `data.type` values that are session/UI metadata, not
# renderable assistant content. They have no uuid, never land on
# msg.events, and must not be surfaced as detached root children either.
# Mirrors the frontend skip list in MessageBubble.tsx (`mtype === ...`).
NON_RENDER_AGENT_DATA_TYPES = frozenset({
    "system",
    "queue-operation",
    "last-prompt",
    "attachment",
    "ai-title",
    "file-history-snapshot",
    "mode",
})


def is_metadata_event(event: dict) -> bool:
    """True for non-render agent_message metadata (ai-title, last-prompt,
    file-history-snapshot, …). Unwraps a manager_event frame first, like
    is_synthetic_event."""
    if not isinstance(event, dict):
        return False
    if event.get("type") == "manager_event":
        data = event.get("data")
        inner = data.get("event") if isinstance(data, dict) else None
        return is_metadata_event(inner) if isinstance(inner, dict) else False
    data = event.get("data")
    if not isinstance(data, dict):
        return False
    return data.get("type") in NON_RENDER_AGENT_DATA_TYPES


def strip_synthetic_events(events: list[dict]) -> list[dict]:
    """Remove SDK-emitted synthetic continuation markers from the event
    list. These have ``model == "<synthetic>"`` and contain a "No
    response requested." placeholder that should never reach the
    persisted session or the frontend."""
    return [e for e in events if not is_synthetic_event(e)]


def _assistant_text_units(event: dict) -> list[tuple[str, str | None, list[str] | None]]:
    """Flatten one assistant event into text runs and non-text boundaries.

    The assistant message's `content` snapshot is the final answer text,
    not every bit of prose the model wrote before/interleaved with tools.
    We therefore preserve block order and treat any non-text content block
    as a boundary between text batches.
    """
    if not isinstance(event, dict):
        return []
    if event.get("type") != "agent_message":
        return []
    data = event.get("data") or {}
    if data.get("type") != "assistant":
        return []
    if data.get("parent_tool_use_id"):
        return []
    if data.get("isStreamError"):
        return []
    message = data.get("message")
    if (
        isinstance(message, dict)
        and message.get("model") == "<synthetic>"
        and not data.get("isApiErrorMessage")
    ):
        return []
    if not isinstance(message, dict):
        return []

    uid = data.get("uuid")
    uid = uid if isinstance(uid, str) and uid else None
    units: list[tuple[str, str | None, list[str] | None]] = []

    content = message.get("content")
    if isinstance(content, str):
        if content:
            units.append(("text", uid, [content]))
        return units

    if not isinstance(content, list):
        return []

    pending: list[str] = []

    def flush_text() -> None:
        nonlocal pending
        if pending:
            units.append(("text", uid, pending))
            pending = []

    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str) and t:
                pending.append(t)
            continue
        flush_text()
        units.append(("boundary", None, None))
    flush_text()
    return units


def extract_output_text(events: list[dict]) -> str:
    """Return the final contiguous assistant text batch.

    INVARIANT: when multiple events carry the SAME `uuid` (gemini
    streams cumulative text snapshots — every delta re-emits the
    growing buffer under one stable per-message uuid), only the LAST
    occurrence per uuid counts. Without this, a turn that ends with
    "pong" appears as "p po pon pong" because every snapshot is
    concatenated. Claude emits one event per assistant message so the
    dedup is a no-op for it.

    Text before or between tool/thinking blocks is renderable in
    `msg.events`, but it is not the assistant bubble's plain-content
    snapshot. The snapshot is the trailing maximal run of primary-agent
    text blocks. If the primary agent ends on a tool/thinking block,
    there is no plain-content final answer.
    """
    units: list[tuple[str, str | None, list[str] | None]] = []
    prev_uuid: str | None = None
    for e in events:
        ev_units = _assistant_text_units(e)
        if ev_units:
            ev_uuid = next(
                (uid for _, uid, _ in ev_units if uid is not None),
                None,
            )
            # Different UUIDs mean different API messages — a tool round-trip
            # sat between them but tool_result events are not stored in the
            # render list, so we insert an explicit boundary.
            if (
                ev_uuid is not None
                and prev_uuid is not None
                and ev_uuid != prev_uuid
            ):
                units.append(("boundary", None, None))
            prev_uuid = ev_uuid
        units.extend(ev_units)

    batch_reversed: list[tuple[str | None, list[str]]] = []
    seen_text = False
    for kind, uid, parts in reversed(units):
        if kind == "boundary":
            break
        if not parts:
            continue
        seen_text = True
        batch_reversed.append((uid, parts))

    batch = list(reversed(batch_reversed))
    by_uuid: dict[str, list[str]] = {}        # uuid -> its latest text parts
    order: list[str] = []                     # first-seen order of uuids
    anonymous: list[str] = []                 # events with no uuid (legacy)
    for uid, parts in batch:
        if uid:
            if uid not in by_uuid:
                order.append(uid)
            # Overwrite — last snapshot wins. Streaming providers emit
            # cumulative text per stable uuid; the final emission for
            # a given uuid is the complete message.
            by_uuid[uid] = parts
        else:
            anonymous.extend(parts)
    out: list[str] = []
    for uid in order:
        out.extend(by_uuid[uid])
    out.extend(anonymous)
    return " ".join(out).strip()


def extract_subagent_types(events: list[dict]) -> list[str]:
    """Collect unique subagent_type values from Agent/Task tool_use blocks."""
    seen: set[str] = set()
    result: list[str] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("type") != "agent_message":
            continue
        data = e.get("data") or {}
        if data.get("type") != "assistant":
            continue
        message = data.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if name not in ("Agent", "Task"):
                continue
            inp = block.get("input") or {}
            subagent_type = inp.get("subagent_type") or name
            if subagent_type not in seen:
                seen.add(subagent_type)
                result.append(subagent_type)
    return result
