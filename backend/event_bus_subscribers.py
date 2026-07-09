"""Standard subscribers wired into `event_bus` at startup.

Today:
  - Persistence subscriber (priority 10) that funnels every persistent
    bus event into the event journal writer. From there `BetterAgentJsonlTailer`
    (which tails `events.jsonl` for any subscribed root) handles live
    WS fan-out and catch-up replay automatically — so persistence + WS
    broadcast are the same single side-effect.
  - Session content projection subscriber: turns written journal rows
    back into SessionManager-owned render-tree state.
  - Session worker-fanout projection subscriber: owns worker/fork cleanup
    for `session.worker_fanout_required` facts.

When/if other transports are added (metrics, traces, third-party
webhooks), they register here with appropriately higher priority so
they never run before persistence.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from event_bus import BusEvent, bus, register_event_schema
from event_journal import (
    EVENT_JOURNAL_EVENT,
    EVENT_JOURNAL_WRITTEN,
    EventJournalWriter,
    RENDER_EVENT_TYPES,
    event_journal_writer,
)
from session_manager import manager as session_manager
from ordered_root_dispatcher import OrderedRootDispatcher

logger = logging.getLogger(__name__)


_JOURNAL_SUBSCRIBER_PRIORITY = 10  # MUST run before WS-facing subscribers
_SESSION_PROJECTION_PRIORITY = 20  # after journal write, before WS
_SESSION_PROJECTION_SHARDS = 8
_SESSION_PROJECTION_MAX_PENDING = 256


@dataclass(frozen=True)
class SessionProjectionCommand:
    root_id: str
    sid: str
    msg_id: str
    event_type: str
    source: str
    seq: int


# Declare event schemas at MODULE LOAD time (not inside
# `register_default_subscribers`). `register_default_subscribers` runs
# only in `on_startup`, but other `bind_*` calls happen during
# `main.py` module load — earlier. Without module-load registration,
# producers that publish between module-load and on_startup would
# stamp `schema_version=1` (the unregistered default) instead of the
# real registered version. Today everything's v1 so the outcome is
# identical; a future v2 bump without this discipline would silently
# emit mixed stamps depending on startup timing.
register_event_schema("lifecycle.turn_complete", 1)
register_event_schema("lifecycle.turn_stopped", 1)
register_event_schema("lifecycle.turn_start", 1)
register_event_schema("session.parent_deleted", 1)
register_event_schema("session.worker_fanout_required", 1)
register_event_schema("requirement_tags.refresh_requested", 1)
register_event_schema("requirement_tags.refreshed", 1)


async def _persist_to_event_journal(event: BusEvent) -> None:
    """Bus → EventJournalWriter.

    Honors `event.persist`: backend-internal notifications opt out by
    setting `persist=False` on the BusEvent. Skipped events still fan
    out to other subscribers — they just don't land on disk.
    """
    if not event.persist:
        return
    if event.type.startswith("event_journal."):
        return
    source = "event_bus"
    payload = event.payload
    explicit_source = payload.get("__source")
    if isinstance(explicit_source, str) and explicit_source:
        source = explicit_source
        payload = {k: v for k, v in payload.items() if k != "__source"}
    journal_payload = {
        "event_type": event.type,
        "data": payload,
        "source": source,
    }
    if event.msg_id:
        journal_payload["message_id"] = event.msg_id
    await bus.publish(BusEvent(
        type=EVENT_JOURNAL_EVENT,
        root_id=event.root_id,
        sid=event.sid,
        payload=journal_payload,
        run_id=event.run_id,
        persist=False,
    ))


def _apply_session_content_projection(command: SessionProjectionCommand) -> None:
    if command.event_type == "event_ownership_resolved":
        session_manager.apply_journal_ownership_resolution(
            command.root_id,
            command.sid,
            command.msg_id,
            command.seq,
        )
        return
    if command.source == "provider_stream":
        return
    if command.event_type not in RENDER_EVENT_TYPES:
        return
    from event_journal import event_journal_reader
    rows, _, _ = event_journal_reader.read_events(
        command.root_id,
        after_seq=command.seq - 1,
        limit=1,
    )
    row = rows[0] if rows else None
    if not isinstance(row, dict) or int(row.get("seq") or 0) != command.seq:
        raise RuntimeError(
            f"durable journal row {command.root_id}:{command.seq} is unavailable",
        )
    data = row.get("data")
    session_manager.apply_written_journal_event(
        command.root_id,
        command.sid,
        command.msg_id,
        command.event_type,
        data if isinstance(data, dict) else {},
        command.seq,
    )


def _mark_session_projection_dirty(
    root_id: str,
    _command: SessionProjectionCommand,
    _exc: BaseException,
) -> None:
    session_manager.mark_reconcile_dirty(root_id)


_SESSION_PROJECTION_DISPATCHER = OrderedRootDispatcher(
    _apply_session_content_projection,
    pool_size=_SESSION_PROJECTION_SHARDS,
    thread_name_prefix="session-projection",
    logger=logger,
    on_error=_mark_session_projection_dirty,
    max_pending=_SESSION_PROJECTION_MAX_PENDING,
)


async def _refresh_session_content_projection(event: BusEvent) -> None:
    """Enqueue an ordered projection after its journal row is durable."""
    if not event.msg_id:
        return
    payload = event.payload
    command = SessionProjectionCommand(
        root_id=str(event.root_id),
        sid=str(event.sid),
        msg_id=str(event.msg_id),
        event_type=str(payload.get("event_type") or "unknown"),
        source=str(payload.get("source") or ""),
        seq=int(payload.get("seq") or 0),
    )
    _SESSION_PROJECTION_DISPATCHER.submit(command.root_id, command)


def shutdown_session_content_projection() -> None:
    bus.unsubscribe("session_content_projection")
    _SESSION_PROJECTION_DISPATCHER.shutdown(wait=True)


async def _refresh_session_search_projection(event: BusEvent) -> None:
    payload = event.payload
    data = payload.get("data")
    if not isinstance(data, dict):
        return
    entry = {
        "seq": payload.get("seq"),
        "sid": event.sid,
        "type": payload.get("event_type"),
        "data": data,
    }
    if event.msg_id:
        entry["msg_id"] = event.msg_id
    _enqueue_session_search_projection(event.root_id, entry)


def _enqueue_session_search_projection(root_id: str, entry: dict) -> None:
    import session_search_projection
    session_search_projection.note_event_written(root_id, entry)


async def _refresh_requirement_tags(event: BusEvent) -> None:
    await asyncio.to_thread(_refresh_requirement_tags_sync)


def _refresh_requirement_tags_sync() -> None:
    import extension_package_loader
    import extension_store
    try:
        try:
            extension_package_loader.ensure_package_importable(
                extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID,
                "requirement_analysis",
            )
        except extension_package_loader.ExtensionPackageUnavailable:
            pass  # Extension not registered; try direct import (tests, standalone).
        from requirement_analysis.session_tags import tags_by_session
    except ModuleNotFoundError:
        return
    tags_by_session(blocking=False)


async def _apply_requirement_tags_projection(event: BusEvent) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    tags_by_session = payload.get("tags_by_session")
    if not isinstance(tags_by_session, dict):
        return
    import session_store
    await asyncio.to_thread(
        session_store.set_requirement_tags_projection,
        tags_by_session,
    )


def bind_event_journal_writer(
    writer: EventJournalWriter = event_journal_writer,
) -> None:
    writer.register(bus, priority=_JOURNAL_SUBSCRIBER_PRIORITY)
    logger.info("event_bus: registered event journal writer")


def bind_session_content_projection() -> None:
    bus.unsubscribe("session_content_projection")
    bus.unsubscribe("session_search_projection")
    bus.subscribe(
        EVENT_JOURNAL_WRITTEN,
        _refresh_session_content_projection,
        priority=_SESSION_PROJECTION_PRIORITY,
        name="session_content_projection",
    )
    bus.subscribe(
        EVENT_JOURNAL_WRITTEN,
        _refresh_session_search_projection,
        priority=_SESSION_PROJECTION_PRIORITY + 1,
        name="session_search_projection",
    )
    logger.info("event_bus: registered session content projection")


def bind_requirement_tags_projection() -> None:
    bus.unsubscribe("requirement_tags_refresh")
    bus.unsubscribe("requirement_tags_projection")
    bus.subscribe(
        "requirement_tags.refresh_requested",
        _refresh_requirement_tags,
        priority=200,
        name="requirement_tags_refresh",
    )
    bus.subscribe(
        "requirement_tags.refreshed",
        _apply_requirement_tags_projection,
        priority=40,
        name="requirement_tags_projection",
    )
    logger.info("event_bus: registered requirement tags projection")


def register_default_subscribers() -> None:
    """Idempotent. Wires the standard subscribers; safe to call multiple
    times during startup or in tests — duplicate `name` registrations
    are pruned first."""
    bind_event_journal_writer()
    bind_session_content_projection()
    bind_requirement_tags_projection()
    bus.unsubscribe("ingester_to_events_jsonl")
    bus.unsubscribe("event_journal_persistence_adapter")
    bus.subscribe(
        "*",
        _persist_to_event_journal,
        priority=_JOURNAL_SUBSCRIBER_PRIORITY,
        name="event_journal_persistence_adapter",
    )
    # (Event schema registrations live at module-load time above
    # so they're in place before any producer runs.)
    logger.info("event_bus: registered event journal persistence adapter")
    try:
        from hook_runner import bind_configured_hooks
        bind_configured_hooks()
    except Exception:
        logger.exception("event_bus: hook runner registration failed")


def bind_session_ws_broadcaster(broadcaster) -> None:
    """Subscribe `session_ws_broadcaster.on_change` to `session.*` BusEvents.

    The broadcaster's `on_change(sid, change)` signature is unchanged:
    the bus subscriber unwraps `(event.sid, event.payload)` so the
    broadcaster itself doesn't need to know about the bus.

    Idempotent. Priority 50 (default, well after persistence at 10
    so events.jsonl writes always land first)."""
    async def _handler(event: BusEvent) -> None:
        # `event.payload` is the enriched change dict that
        # `session_manager._fire` published. Same shape the legacy
        # `add_listener` callers received.
        try:
            broadcaster.on_change(event.sid, event.payload)
        except Exception:
            logger.exception(
                "bind_session_ws_broadcaster: on_change raised "
                "for %s", event.type,
            )

    bus.unsubscribe("session_ws_broadcaster_on_change")
    bus.subscribe(
        "session.*",
        _handler,
        priority=50,
        name="session_ws_broadcaster_on_change",
    )
    logger.info(
        "event_bus: registered session_ws_broadcaster.on_change "
        "as bus subscriber on session.*",
    )


def bind_worker_fanout_cleanup(broadcast_workers_changed) -> None:
    """Project worker/fork invalidation facts into worker_store state."""
    async def _handler(event: BusEvent) -> None:
        payload = event.payload
        session_id = str(payload.get("session_id") or event.sid or "")
        if not session_id:
            logger.warning("worker_fanout_cleanup: missing session_id")
            return
        op_label = str(payload.get("op_label") or event.type)
        caller_scope = bool(payload.get("caller_scope"))
        remove_worker = bool(payload.get("remove_worker"))
        try:
            from stores import worker_store as _ws
            cleared: list[str] = []
            if caller_scope:
                cleared.extend(_ws.clear_forks_for_caller_everywhere(session_id))
            if remove_worker or not caller_scope:
                cleared.extend(_ws.clear_forks_for_worker_everywhere(session_id))
            if remove_worker:
                _ws.remove_worker_everywhere(session_id)
            seen_forks: set[str] = set()
            for fork_session_id in cleared:
                if fork_session_id in seen_forks:
                    continue
                seen_forks.add(fork_session_id)
                try:
                    session_manager.delete(fork_session_id)
                except Exception:
                    logger.exception(
                        "delete delegate-fork BC %s failed during %s",
                        fork_session_id, op_label,
                    )
            await broadcast_workers_changed(None)
        except Exception:
            logger.exception(
                str(payload.get("outer_log_msg") or "worker fan-out cleanup failed"),
            )

    bus.unsubscribe("worker_fanout_cleanup")
    bus.subscribe(
        "session.worker_fanout_required",
        _handler,
        priority=200,
        name="worker_fanout_cleanup",
    )
    logger.info("event_bus: registered worker fan-out cleanup subscriber")


def _last_assistant_text(sess: dict) -> str:
    """Concatenate finalized text-block text from the LAST assistant message.
    READ-ONLY — never mutates msg.events (convergence invariant)."""
    msgs = sess.get("messages") or []
    last = None
    for msg in msgs:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            last = msg
    if last is None:
        return ""
    parts: list[str] = []
    for ev in last.get("events") or []:
        if not isinstance(ev, dict):
            continue
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        message = data.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)


def _log_hook_task_exception(task: asyncio.Task, label: str) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "%s hook task raised: %r",
            label,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def bind_post_turn_hooks() -> None:
    """Dispatch ``lifecycle.turn_complete`` to every installed extension that
    declares an ``entrypoints.hooks.post_turn`` backend path. Fire-and-forget,
    isolated errors — one failing hook never blocks another or the turn.

    Additive: subscribes to the existing turn-complete bus event the
    orchestrator already publishes, so it touches no turn-finalization path
    (no convergence-invariant risk). Each hook is a sandboxed backend-host
    invocation via ``invoke_extension_backend``."""
    import extension_backend_loader

    async def _on_turn_complete(event: BusEvent) -> None:
        try:
            import extension_store
            hooks = extension_store.post_turn_hooks()
            if not hooks:
                return
            from env_compat import get_env
            import json as _json
            base_url = get_env("BETTER_CLAUDE_BACKEND_URL", "http://localhost:8000")
            body = _json.dumps({
                "session_id": event.sid,
                "app_session_id": event.sid,
                "turn_type": event.type,
                "payload": event.payload or {},
            }).encode("utf-8")
            for ext_id, path in hooks:
                async def _invoke(eid: str = ext_id, p: str = path) -> None:
                    try:
                        await extension_backend_loader.invoke_extension_backend(
                            eid, p.lstrip("/"), method="POST", body_bytes=body, base_url=base_url,
                        )
                    except Exception:
                        logger.exception("post-turn hook %s failed", eid)
                t = asyncio.create_task(_invoke(), name=f"post-turn-{ext_id}-{event.sid[:8]}")
                t.add_done_callback(
                    lambda task: _log_hook_task_exception(task, "post-turn")
                )
        except Exception:
            logger.exception("post-turn hook dispatch failed for %s", event.sid)

    bus.unsubscribe("extension_post_turn_hooks")
    bus.subscribe(
        "lifecycle.turn_complete",
        _on_turn_complete,
        priority=300,
        name="extension_post_turn_hooks",
    )
    logger.info("event_bus: registered extension post-turn hooks subscriber")


def bind_pre_turn_hooks() -> None:
    """Dispatch ``lifecycle.turn_start`` to every installed extension that
    declares an ``entrypoints.hooks.pre_turn`` backend path. Fire-and-forget,
    isolated errors — one failing hook never blocks another or the turn.

    Mirror of ``bind_post_turn_hooks``: subscribes to the existing
    turn-start bus event the orchestrator already publishes, so it touches no
    turn-execution path (no convergence-invariant risk). Each hook is a
    sandboxed backend-host invocation via ``invoke_extension_backend``. The
    body carries the turn context (session id + payload); hooks fetch whatever
    else they need (prompt, cwd) via core internal endpoints, exactly as
    post-turn hooks do."""
    import extension_backend_loader

    async def _on_turn_start(event: BusEvent) -> None:
        try:
            import extension_store
            hooks = extension_store.pre_turn_hooks()
            if not hooks:
                return
            from env_compat import get_env
            import json as _json
            base_url = get_env("BETTER_CLAUDE_BACKEND_URL", "http://localhost:8000")
            body = _json.dumps({
                "session_id": event.sid,
                "app_session_id": event.sid,
                "turn_type": event.type,
                "payload": event.payload or {},
            }).encode("utf-8")
            for ext_id, path in hooks:
                async def _invoke(eid: str = ext_id, p: str = path) -> None:
                    try:
                        await extension_backend_loader.invoke_extension_backend(
                            eid, p.lstrip("/"), method="POST", body_bytes=body, base_url=base_url,
                        )
                    except Exception:
                        logger.exception("pre-turn hook %s failed", eid)
                t = asyncio.create_task(_invoke(), name=f"pre-turn-{ext_id}-{event.sid[:8]}")
                t.add_done_callback(
                    lambda task: _log_hook_task_exception(task, "pre-turn")
                )
        except Exception:
            logger.exception("pre-turn hook dispatch failed for %s", event.sid)

    bus.unsubscribe("extension_pre_turn_hooks")
    bus.subscribe(
        "lifecycle.turn_start",
        _on_turn_start,
        priority=300,
        name="extension_pre_turn_hooks",
    )
    logger.info("event_bus: registered extension pre-turn hooks subscriber")
