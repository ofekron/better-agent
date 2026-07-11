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


def event_uuid(event: dict) -> str | None:
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


_event_uuid = event_uuid


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
        uid = event_uuid(event)
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


_TextUnit = tuple[str, str | None, list[str] | None, bool, str]


def _assistant_text_units(event: dict) -> list[_TextUnit]:
    """Flatten one assistant event into text runs and non-text boundaries.

    Each unit is `(kind, uuid, parts, final, origin)`. `final` is True for
    events the provider marked as final-answer emissions (Codex
    `phase: final_answer`); `origin` names the emitting agent when it is
    not the main agent.

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
    final = data.get("final_answer") is True
    origin = data.get("final_answer_origin")
    origin = origin if isinstance(origin, str) else ""
    units: list[_TextUnit] = []

    content = message.get("content")
    if isinstance(content, str):
        if content:
            units.append(("text", uid, [content], final, origin))
        return units

    if not isinstance(content, list):
        return []

    pending: list[str] = []

    def flush_text() -> None:
        nonlocal pending
        if pending:
            units.append(("text", uid, pending, final, origin))
            pending = []

    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str) and t:
                pending.append(t)
            continue
        flush_text()
        units.append(("boundary", None, None, False, ""))
    flush_text()
    return units


def _final_marked_text(units: list[_TextUnit]) -> str:
    """Concatenate final-marked text units, labeling origins when needed.

    Per-uuid last snapshot wins (streaming providers re-emit cumulative
    text under one stable uuid). A single final from the main agent
    renders plain; multiple finals and/or non-main origins render each
    part under an explicit origin label.
    """
    by_uuid: dict[str, tuple[list[str], str]] = {}
    order: list[str] = []
    anonymous: list[tuple[list[str], str]] = []
    for kind, uid, parts, final, origin in units:
        if kind != "text" or not final or not parts:
            continue
        if uid:
            if uid not in by_uuid:
                order.append(uid)
            by_uuid[uid] = (parts, origin)
        else:
            anonymous.append((parts, origin))
    # The same final can exist twice with different uuids (Codex streams
    # it as event_msg AND re-emits it as the finalized response_item; the
    # in-memory echo dedup does not survive a mid-turn tailer restart).
    # Collapse exact (origin, text) duplicates so a durable echo never
    # flips a single final into a double-labeled snapshot.
    finals: list[tuple[list[str], str]] = []
    seen: set[tuple[str, str]] = set()
    for parts, origin in [by_uuid[uid] for uid in order] + anonymous:
        key = (origin, " ".join(parts).strip())
        if key in seen:
            continue
        seen.add(key)
        finals.append((parts, origin))
    if not finals:
        return ""
    if len(finals) == 1 and not finals[0][1]:
        return " ".join(finals[0][0]).strip()
    labeled: list[str] = []
    for parts, origin in finals:
        text = " ".join(parts).strip()
        if not text:
            continue
        labeled.append(f"[final answer · {origin or 'main agent'}]\n{text}")
    return "\n\n".join(labeled).strip()


def _collect_text_units(events: list[dict]) -> list[_TextUnit]:
    """Flatten events into ordered text units with inter-message boundaries.

    INVARIANT: when multiple events carry the SAME `uuid` (gemini
    streams cumulative text snapshots — every delta re-emits the
    growing buffer under one stable per-message uuid), only the LAST
    occurrence per uuid counts downstream. Text before or between
    tool/thinking blocks is renderable in `msg.events`, but it is not
    the assistant bubble's plain-content snapshot.

    Final-answer marks override the trailing-run heuristic in
    `extract_output_text`: when any event is provider-marked final
    (`final_answer` on the event data), the snapshot is the
    concatenation of ALL final-marked text, in order. With more than
    one final emission — or a final from other than the main agent —
    each part is labeled with its origin so the reader can tell where
    every message came from.
    """
    units: list[_TextUnit] = []
    prev_uuid: str | None = None
    for e in events:
        ev_units = _assistant_text_units(e)
        if ev_units:
            ev_uuid = next(
                (uid for _, uid, _, _, _ in ev_units if uid is not None),
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
                units.append(("boundary", None, None, False, ""))
            prev_uuid = ev_uuid
        units.extend(ev_units)

    return units


def extract_output_text(events: list[dict]) -> str:
    """Final-answer snapshot: provider final-answer marks when present,
    otherwise the trailing-text-run heuristic (`extract_trailing_output_text`).
    """
    units = _collect_text_units(events)
    final_text = _final_marked_text(units)
    if final_text:
        return final_text
    return _trailing_run_text(units)


def extract_trailing_output_text(events: list[dict]) -> str:
    """Trailing maximal text run ONLY — ignores final-answer marks.

    For machine consumers that must see the literally-last text (e.g.
    rate-limit/transient-error sniffing in turn_helpers), where an earlier
    final-marked answer must not mask trailing error text.
    """
    return _trailing_run_text(_collect_text_units(events))


def _trailing_run_text(units: list[_TextUnit]) -> str:
    batch_reversed: list[tuple[str | None, list[str]]] = []
    for kind, uid, parts, _final, _origin in reversed(units):
        if kind == "boundary":
            break
        if not parts:
            continue
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


def has_final_answer_event(events: list[dict]) -> bool:
    """True when any event data carries a provider final-answer mark."""
    for e in events or []:
        if not isinstance(e, dict):
            continue
        data = e.get("data")
        if isinstance(data, dict) and data.get("final_answer") is True:
            return True
    return False


def has_assistant_text(events: list[dict]) -> bool:
    """True when any event carries primary-agent text (regardless of
    whether it survives the trailing-run projection)."""
    return any(
        kind == "text"
        for e in events or []
        for kind, _uid, _parts, _final, _origin in _assistant_text_units(e)
    )


def project_content_snapshot(events: list[dict], current: str | None) -> str:
    """Final-answer content snapshot for an assistant message.

    Single source of truth for every content (re-)projection site.
    `extract_output_text` is empty when the event list ends on a
    tool/thinking boundary — e.g. events late-flushed after the turn
    finalized, or a turn cut off mid-tools. An empty
    projection must never clobber an already-set non-empty snapshot, so
    the caller's current content wins in that case.
    """
    projected = extract_output_text(strip_synthetic_events(events or []))
    if projected:
        return projected
    return current or ""


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
