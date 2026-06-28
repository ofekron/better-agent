"""OrchestrationStrategy — per-mode behavior for messages, events, and rendering.

Each orchestration mode (native, manager) implements this ABC so that
orchestrator.py, main.py, and the frontend never branch on mode strings.
Mode-specific logic lives in one place; callers just call
strategy.method(). (`supervisor` is NOT a mode — it's the
`supervisor_enabled` toggle layered on top of native/manager; see
orchs/supervisor/.)

`apply_event` is the SINGLE delta-applier for the session render tree.
The same method runs for live ingest (events streaming from a running
claude subprocess) and for restore (events replayed from disk after a
backend restart or dead-orphan recovery). The `source_is_provider_stream`
flag gates side-effects that only make sense when the event comes from
the provider's CLI stream (SDK callback or crash-recovery replay):

  - source_is_provider_stream=True  → rewrite file refs and fire live-only
                 lifecycle/provenance/unread side effects. Live SDK
                 callbacks write events.jsonl before calling apply_event;
                 recovery/orphan paths may still request a journal write.
  - source_is_provider_stream=False → render-tree only; events.jsonl is
                 the SOURCE during replay, no double-write.

apply_event is idempotent on event uuid — re-applying an event that's
already in the msg's events list is a no-op. This makes replay safe.
"""

from abc import ABC, abstractmethod
import json
from dataclasses import dataclass, field
from typing import Literal, Optional

import perf
from session_manager import manager as session_manager
from user_msg_lifecycle import emit_received

_ALL_TASKS_DONE_MARKER_TAG = "ALL_TASKS__DONE"


def _sum_token_usage(usages: list[dict]) -> Optional[dict]:
    """Sum a list of token_usage dicts field-wise. Returns None if the
    input is empty. Fields summed: input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens. Unknown
    numeric fields are summed too; non-numeric fields from the first
    dict pass through unchanged."""
    if not usages:
        return None
    out: dict = {}
    for u in usages:
        if not isinstance(u, dict):
            continue
        for k, v in u.items():
            if isinstance(v, (int, float)):
                out[k] = out.get(k, 0) + v
            elif k not in out:
                out[k] = v
    return out or None


@dataclass
class ApplyEventCtx:
    """Per-turn closure-locals passed to apply_event.

    Holds state that lives across many apply_event calls within one
    turn: the mutable manager_sid_holder (so turn_start/complete
    frames can pin the discovered claude sid), the workers_list
    accumulator (so worker_complete frames can snapshot accurately),
    and the preceding user_msg (so manager_event with a user claude
    entry can wire the rewind anchor).
    """
    manager_sid_holder: Optional[dict] = None
    workers_list: Optional[list] = None
    user_msg: Optional[dict] = None
    root_id: Optional[str] = None
    run_id: Optional[str] = None


def _uid_idx_for(owner: dict, evs: list) -> dict:
    """Return the `{uuid: index}` map for an events list, building lazily
    if missing AND validating cheaply via a length check.

    The map lets `apply_event`'s dedup → O(1) lookup instead of O(N)
    linear scan. Without it, hydrating a 5000-event session via
    `apply_event(live=False)` × N is O(N²) (measured 5968 ms cold-load
    on the 4ddbd4d7 session).

    `owner` is the dict that holds the `events` key — for the primary
    agent that's `msg`, for worker panels the panel dict. The index
    lives on `owner["_uid_idx"]` and is stripped from
    disk snapshots by `session_store._strip_volatile_from_tree`.

    Cheap validity check: a lazy compare of `len(idx)` against the
    number of uuid-bearing events in `evs`. Catches structural drift
    (someone replaced the whole list) without walking every event. Does
    NOT catch the pathological "same length, different uuids" case —
    that requires the mutating caller to `owner.pop('_uid_idx', None)`
    explicitly. Today the only callers that bypass `apply_event` and
    its mutator family are `session_manager.set_native_events` and
    `main._strip_synthetic_events_from_tree`; both pop the index when
    they touch `m['events']`.
    """
    idx = owner.get("_uid_idx")
    if idx is not None:
        return idx
    idx = {}
    for i, e in enumerate(evs):
        eu = _event_uuid(e)
        if eu:
            idx[eu] = i
    owner["_uid_idx"] = idx
    return idx


def _event_uuid(event: dict) -> Optional[str]:
    """Extract the claude event uuid from an event dict.

    Handles legacy manager_event wrappers (`data.event.data.uuid`)
    and the canonical agent_message shape (`data.uuid`).
    """
    if not isinstance(event, dict):
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    uid = data.get("uuid")
    if uid:
        return uid
    # Legacy manager_event wrapper: {"data": {"event": {"data": {"uuid": ...}}}}
    inner = data.get("event")
    if isinstance(inner, dict):
        inner_data = inner.get("data")
        if isinstance(inner_data, dict):
            return inner_data.get("uuid")
    return None


def _normalize_for_render(event: dict) -> dict:
    """Normalize to canonical agent_message shape.

    Unwraps legacy manager_event wrappers.  Passes agent_message
    through unchanged.  Returns the inner event dict for storage on
    msg.events (uniform shape regardless of which path produced it).
    """
    if not isinstance(event, dict):
        return event
    if event.get("type") == "manager_event":
        data = event.get("data") or {}
        inner = data.get("event")
        if isinstance(inner, dict):
            return inner
    if event.get("type") == "agent_message":
        data = event.get("data")
        if isinstance(data, dict) and data.get("type") == "agent_message":
            inner_data = data.get("data")
            if isinstance(inner_data, dict):
                return {"type": "agent_message", "data": inner_data}
    return event


def _agent_message_text(data: dict) -> str:
    """Concatenate text-block text from a canonical agent_message ``data``
    dict. Used to scan RAW (pre file-ref/tag rewrite) assistant text for
    attention-marker tags."""
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _complete_current_todos(session_id: str) -> None:
    current = session_manager.get_current_todos_snapshot(session_id)
    if not current:
        return
    completed = [
        {**todo, "status": "completed"}
        for todo in current
        if isinstance(todo, dict)
    ]
    if completed and completed != current:
        session_manager.set_current_todos(session_id, completed)


def _unwrap_typed_worker_envelope(event: dict) -> dict:
    if not isinstance(event, dict) or event.get("type") != "agent_message":
        return event
    data = event.get("data")
    if not isinstance(data, dict):
        return event
    inner_type = data.get("type")
    inner_data = data.get("data")
    if inner_type not in {"worker_start", "worker_event", "worker_complete"}:
        return event
    if not isinstance(inner_data, dict):
        return event
    return {"type": inner_type, "data": inner_data}


class OrchestrationStrategy(ABC):

    # Event types that land on msg.events (the narrow render tree).
    # DO NOT extend without auditing `_reconcile_msg_events_from_jsonl`.
    # `manager_event` kept for backward compat with pre-migration events.jsonl rows.
    _RENDER_TREE_ETYPES = ("agent_message", "manager_event", "model_switched", "steer_prompt")

    # Hard caps on lifecycle accumulators so a `done` event that never
    # fires (orphan run, crashed handler, race between cancel and
    # finally-cleanup) can't leak memory forever in the long-lived
    # strategy singletons.
    _LIFECYCLE_MAX_PENDING_MSGS = 1024
    _LIFECYCLE_RECEIVED_MAX_PER_SESSION = 512

    def __init__(self) -> None:
        # Per-lifecycle_msg_id accumulator of sub-turn results. Each
        # mode's `handle_turn` calls `record_turn_result` after every
        # `run_turn` it drives; `make_done_payload` reads the accumulated
        # list to assemble the `user_message_done` body. Native/manager
        # have exactly one entry per user msg; supervisor has N (worker
        # turns + verdict turns).
        self._lifecycle_sub_turns: dict[str, list[dict]] = {}
        self._lifecycle_started_at: dict[str, float] = {}
        # Per-app-session set of lifecycle ids already received. Prevents
        # double-firing `user_message_received` if apply_event re-walks
        # the same user-line during reconcile.
        self._lifecycle_received_seen: dict[str, set[str]] = {}

    def _evict_lifecycle_overflow(self) -> None:
        """Drop the oldest entries from the per-msg accumulators if a
        cap is exceeded. Triggered on every `record_turn_start` since
        that's the entry point for new lifecycle ids. FIFO via insertion
        order (CPython dicts preserve it)."""
        cap = self._LIFECYCLE_MAX_PENDING_MSGS
        while len(self._lifecycle_sub_turns) > cap:
            oldest = next(iter(self._lifecycle_sub_turns))
            self._lifecycle_sub_turns.pop(oldest, None)
            self._lifecycle_started_at.pop(oldest, None)
        while len(self._lifecycle_started_at) > cap:
            oldest = next(iter(self._lifecycle_started_at))
            self._lifecycle_started_at.pop(oldest, None)

    def _fire_user_msg_received_if_pending(
        self,
        *,
        app_session_id: str,
        agent_user_uuid: str,
    ) -> None:
        """Resolve the in-flight lifecycle id (set by the prompt processor
        when it picked this turn off the queue) and publish
        `user_message_received` to the bus. Idempotent: if the lifecycle
        already saw `received` this no-ops because the strategy clears
        its pending-received set on emit."""
        # Reach into the running coordinator's stash. Imported lazily
        # to dodge the circular orchestrator → orchs → base loop.
        try:
            from orchestrator import get_active_coordinator
        except Exception:
            return
        coord = get_active_coordinator()
        if coord is None:
            return
        lifecycle_msg_id = coord.user_prompt_manager.get_in_flight_lifecycle_msg_id(
            app_session_id,
        )
        if not lifecycle_msg_id:
            return
        seen = self._lifecycle_received_seen.setdefault(app_session_id, set())
        if lifecycle_msg_id in seen:
            return
        # Bound the per-session set so a long-lived session with many
        # prompts can't grow unbounded. The set is only consulted to
        # dedupe the immediate received-emit; older ids that fall out
        # would at worst cause a duplicate received event, which is
        # benign (UI is idempotent on lifecycle_msg_id).
        if len(seen) >= self._LIFECYCLE_RECEIVED_MAX_PER_SESSION:
            for _ in range(len(seen) - self._LIFECYCLE_RECEIVED_MAX_PER_SESSION + 1):
                try:
                    seen.pop()
                except KeyError:
                    break
        seen.add(lifecycle_msg_id)
        agent_sid = None
        try:
            sess = session_manager.get_lite(app_session_id) or {}
            agent_sid = sess.get(self.session_id_field)
        except Exception:
            pass
        # Schedule the bus publish on the running event loop without
        # awaiting it from this sync apply_event path.
        import asyncio
        try:
            asyncio.get_running_loop().create_task(
                emit_received(
                    app_session_id=app_session_id,
                    lifecycle_msg_id=lifecycle_msg_id,
                    agent_user_uuid=agent_user_uuid,
                    agent_sid=agent_sid,
                ),
                name=f"lifecycle-received-{lifecycle_msg_id[:8]}",
            )
        except RuntimeError:
            # No running loop (replay/sync context); skip.
            pass

    def record_turn_start(self, lifecycle_msg_id: str) -> None:
        """Stamp the wall-clock start for this user message. Idempotent
        — only the first call sticks, subsequent are ignored (supervisor
        loop runs multiple sub-turns but the user msg started at the
        first one)."""
        import time
        if lifecycle_msg_id not in self._lifecycle_started_at:
            self._lifecycle_started_at[lifecycle_msg_id] = time.monotonic()
            self._evict_lifecycle_overflow()

    def record_turn_result(
        self,
        lifecycle_msg_id: str,
        *,
        role: str,                       # "worker" | "supervisor" | "manager" | "native"
        success: bool,
        token_usage: Optional[dict] = None,
        error: Optional[str] = None,
        agent_sid: Optional[str] = None,
    ) -> None:
        """Append one sub-turn outcome to the user msg's accumulator.
        Called from `handle_turn` after each `run_turn` completes."""
        self._lifecycle_sub_turns.setdefault(lifecycle_msg_id, []).append({
            "role": role,
            "success": success,
            "token_usage": token_usage,
            "error": error,
            "agent_sid": agent_sid,
        })

    def make_done_payload(
        self,
        lifecycle_msg_id: str,
        *,
        cancelled: bool = False,
        terminal_error: Optional[str] = None,
        interrupted_by_msg_id: Optional[str] = None,
    ) -> dict:
        """Assemble the `user_message_done` payload body.

        Default impl (native/manager): the last sub-turn's outcome is
        the user-msg outcome; token_usage_total = sum across sub-turns
        (single element in the simple case).

        Supervisor overrides for terminal-verdict semantics.
        """
        import time
        sub_turns = self._lifecycle_sub_turns.pop(lifecycle_msg_id, [])
        started_at = self._lifecycle_started_at.pop(lifecycle_msg_id, None)
        duration_ms = (
            int((time.monotonic() - started_at) * 1000)
            if started_at is not None else None
        )
        last = sub_turns[-1] if sub_turns else {}
        if cancelled or terminal_error:
            success = False
        elif sub_turns:
            success = bool(last.get("success"))
        else:
            # No sub-turn results recorded but no cancel/error either:
            # handle_turn ran to completion without raising. For modes
            # that don't (yet) call record_turn_result this is the
            # "completed cleanly" baseline. Supervisor overrides via
            # explicit per-sub-turn recording.
            success = True
        error = terminal_error or (last.get("error") if last else None)
        token_usage_total = _sum_token_usage(
            [st.get("token_usage") for st in sub_turns if st.get("token_usage")]
        )
        return {
            "success": success,
            "cancelled": cancelled,
            "error": error,
            "duration_ms": duration_ms,
            "token_usage_total": token_usage_total,
            "sub_turns": sub_turns,
            "interrupted_by_msg_id": interrupted_by_msg_id,
        }

    @property
    @abstractmethod
    def mode(self) -> str:
        """The orchestration_mode string this strategy handles."""

    @property
    @abstractmethod
    def session_id_field(self) -> str:
        """Session-JSON field for the claude session id
        (e.g. 'agent_session_id')."""

    @property
    @abstractmethod
    def trace_step_name(self) -> str:
        """Trace step label for a primary turn in this mode
        (e.g. 'native_turn' or 'manager_turn')."""

    def wrap_cli_prompt(self, *, cwd: str, prompt: str, session: dict) -> str:
        """Transform the user prompt into the text actually fed to the
        CLI for a primary turn. Identity by default (native); manager
        overrides to prepend BOOTSTRAP + <known_workers>."""
        return prompt

    async def run_primary(
        self,
        coordinator,
        *,
        session: dict,
        prompt: str,
        app_session_id: str,
        model: str,
        cwd: str,
        ws_callback,
        images: Optional[list] = None,
        files: Optional[list] = None,
        cli_prompt: Optional[str] = None,
        source: Optional[str] = None,
        user_initiated: bool = False,
        client_id: Optional[str] = None,
        disallowed_tools: Optional[list[str]] = None,
        queue_item_id: Optional[str] = None,
        team_message: Optional[dict] = None,
        capability_contexts: Optional[list[dict]] = None,
        file_discussion_id: Optional[str] = None,
        run_mode: Literal["manager", "native"] | None = None,
    ) -> None:
        """Run ONE primary turn in this mode through coordinator.turn_manager.run_turn.

        Single source for the per-mode dispatch the user-prompt handlers
        (`orchs.native/manager.handle_turn`) and the supervisor verdict
        loop (`orchs.supervisor._primary.run_primary_turn`) all share.
        The strategy supplies `session_id_field`, `mode`,
        `trace_step_name`, and `wrap_cli_prompt`; the caller supplies the
        per-call knobs (`source`, `user_initiated`, `client_id`,
        `images`, and an optional `cli_prompt` override that, when not
        None, bypasses `wrap_cli_prompt` — used by the Ask singleton).

        NOT used for supervisor-slot turns (`_run_supervisor_turn`,
        the supervisor-direct block in orchestrator.handle_prompt): those
        run mode='native' ON the supervisor sid slot and are
        intentionally heterogeneous — they stay raw `run_turn` calls.
        """
        final_cli = (
            cli_prompt
            if cli_prompt is not None
            else self.wrap_cli_prompt(cwd=cwd, prompt=prompt, session=session)
        )
        await coordinator.turn_manager.run_turn(
            session=session,
            prompt=prompt,
            cli_prompt=final_cli,
            app_session_id=app_session_id,
            model=model,
            cwd=cwd,
            ws_callback=ws_callback,
            images=images,
            files=files,
            trace_step_name=self.trace_step_name,
            session_id_field=self.session_id_field,
            mode=run_mode or self.mode,
            client_id=client_id,
            source=source,
            user_initiated=user_initiated,
            disallowed_tools=disallowed_tools,
            queue_item_id=queue_item_id,
            team_message=team_message,
            capability_contexts=capability_contexts,
            file_discussion_id=file_discussion_id,
        )

    @abstractmethod
    def build_assistant_scaffold(self) -> dict:
        """Build the initial assistant message dict (not persisted).

        Manager mode adds a `manager` key; native mode does not.
        """

    @abstractmethod
    def _events_list(self, msg: dict) -> list:
        """Return the mode-specific events list on this msg.
        Native: msg['events']. Manager: msg['manager']['events']."""

    @abstractmethod
    def _events_owner(self, msg: dict) -> dict:
        """Return the dict that OWNS the events list — the parent dict
        whose `events` key holds the list. The uid_idx cache lives on
        this owner. Native: `msg`. Manager: `msg['manager']`."""

    @abstractmethod
    def _append_event(
        self, app_session_id: str, msg_id: str, event: dict,
    ) -> None:
        """Persist one event onto the mode-specific events list of the msg.
        Goes through session_manager so the persistence batch picks it up."""

    @abstractmethod
    def _replace_event(
        self, app_session_id: str, msg_id: str, event: dict, uuid: str,
    ) -> None:
        """Replace an existing event with the same uuid on the msg.events list."""

    def _after_event(
        self,
        *,
        app_session_id: str,
        msg: dict,
        event: dict,
        ctx: ApplyEventCtx,
        source_is_provider_stream: bool,
    ) -> None:
        """Mode-specific hook run after the shared apply_event body.
        Default no-op; manager pins `manager.session_id` on the msg."""

    @staticmethod
    def _apply_ai_title(app_session_id: str, title: str) -> None:
        """Rename the session to the Claude-provided AI title.

        Skips when the session already has the same name (Claude emits
        ai-title many times per turn with an identical string).
        """
        from session_manager import manager as session_manager
        sess = session_manager.get_lite(app_session_id)
        if sess is None:
            return
        if sess.get("name") == title:
            return
        session_manager.rename(app_session_id, title)

    def _apply_metadata_side_effects(
        self,
        *,
        app_session_id: str,
        data: dict,
    ) -> bool:
        metadata_type = data.get("type")
        if metadata_type == "ai-title":
            title = data.get("aiTitle")
            if isinstance(title, str) and title.strip():
                self._apply_ai_title(app_session_id, title.strip())
            return True
        if metadata_type == "file-history-snapshot":
            return True
        return False

    @staticmethod
    def _publish_provider_event(
        write_journal: bool,
        ctx: "ApplyEventCtx",
        *,
        app_session_id: str,
        etype: str,
        data: dict,
        msg_id: Optional[str],
        log_label: str,
    ) -> None:
        if not write_journal or not ctx.root_id:
            return
        from event_journal import EventJournalWriteError, publish_event_sync
        try:
            publish_event_sync(
                session_id=ctx.root_id,
                context_id=app_session_id,
                event_type=etype or "unknown",
                data=data,
                source="apply_event",
                run_id=ctx.run_id,
                message_id=msg_id,
                timeout=0,
            )
        except EventJournalWriteError as exc:
            if "writer is closed" in str(exc):
                return
            import logging
            logging.getLogger(__name__).exception(log_label)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(log_label)

    @staticmethod
    def _ingest_metadata(
        source_is_provider_stream: bool,
        write_journal: bool,
        ctx: "ApplyEventCtx",
        etype: str,
        data: dict,
        app_session_id: str,
        msg_id: Optional[str],
    ) -> None:
        """Persist a metadata-only event to events.jsonl (for crash
        recovery replay) without adding it to msg.events.

        Stamps a deterministic synthetic UUID so the ingester's
        uid:sha256(data) dedup suppresses duplicate rows across
        live ingest + crash recovery replay.
        """
        if not source_is_provider_stream:
            return
        import hashlib as _hashlib
        # Deterministic UUID derived from event content so source_is_provider_stream +
        # recovery dedup correctly.
        payload = json.dumps(data, sort_keys=True)
        synthetic_uuid = f"meta-{_hashlib.sha256(payload.encode()).hexdigest()[:16]}"
        deduped_data = dict(data)
        deduped_data["uuid"] = synthetic_uuid
        OrchestrationStrategy._publish_provider_event(
            write_journal,
            ctx,
            app_session_id=app_session_id,
            etype=etype,
            data=deduped_data,
            msg_id=msg_id,
            log_label="apply_event: metadata ingest failed",
        )

    def prepare_provider_event_for_journal(
        self,
        *,
        app_session_id: str,
        event: dict,
    ) -> tuple[str, dict]:
        event = _unwrap_typed_worker_envelope(event)
        etype = event.get("type") or "unknown"
        data = event.get("data") or {}
        normalized = _normalize_for_render(event)
        normalized_data = normalized.get("data") or {}
        norm_etype = normalized.get("type") or etype
        norm_data = (
            normalized.get("data")
            if isinstance(normalized.get("data"), dict)
            else data
        )

        if normalized_data.get("type") in ("ai-title", "file-history-snapshot"):
            import hashlib as _hashlib

            payload = json.dumps(norm_data, sort_keys=True)
            synthetic_uuid = (
                f"meta-{_hashlib.sha256(payload.encode()).hexdigest()[:16]}"
            )
            deduped_data = dict(norm_data)
            deduped_data["uuid"] = synthetic_uuid
            return norm_etype, deduped_data

        if etype == "worker_event":
            inner = data.get("event") if isinstance(data, dict) else None
            inner_data = inner.get("data") if isinstance(inner, dict) else None
            if isinstance(inner_data, dict):
                from file_ref_resolver import (
                    assume_exists_for_session, rewrite_event_data,
                )
                try:
                    sess = session_manager.get_lite(app_session_id) or {}
                    rewrite_event_data(
                        inner.get("type") or "unknown",
                        inner_data,
                        sess.get("cwd"),
                        assume_exists=assume_exists_for_session(sess),
                    )
                except Exception:
                    import logging
                    logging.getLogger(__name__).debug(
                        "file_ref_resolver rewrite failed for worker_event",
                        exc_info=True,
                    )
            return "worker_event", data

        from file_ref_resolver import (
            assume_exists_for_session, rewrite_event_data,
        )
        try:
            sess = session_manager.get_lite(app_session_id) or {}
            rewrite_event_data(
                etype,
                data,
                sess.get("cwd"),
                assume_exists=assume_exists_for_session(sess),
            )
        except Exception:
            import logging
            logging.getLogger(__name__).debug(
                "file_ref_resolver rewrite failed", exc_info=True,
            )
        return norm_etype, norm_data

    def _apply_worker_event(
        self,
        *,
        app_session_id: str,
        msg_id: str,
        data: dict,
        ctx: "ApplyEventCtx",
        source_is_provider_stream: bool,
        write_journal: bool,
    ) -> None:
        """Route a worker_event to the matching delegation panel.

        Worker_event frames wrap a worker's inner agent_message under
        `data.event`. They route to the panel's events list — NOT to
        the primary `msg.events`. The panel is looked up by
        `delegation_id` in `msg.workers`.
        """
        delegation_id = data.get("delegation_id")
        inner = data.get("event") or {}
        inner_data = inner.get("data") or {}
        # Rewrite file refs on the INNER claude-shaped event. The
        # outer worker_event wrapper has no file paths of its own.
        if source_is_provider_stream:
            from file_ref_resolver import (
                assume_exists_for_session, rewrite_event_data,
            )
            try:
                _sess = session_manager.get_lite(app_session_id) or {}
                rewrite_event_data(
                    inner.get("type") or "unknown",
                    inner_data,
                    _sess.get("cwd"),
                    assume_exists=assume_exists_for_session(_sess),
                )
            except Exception:
                import logging
                logging.getLogger(__name__).debug(
                    "file_ref_resolver rewrite failed for worker_event",
                    exc_info=True,
                )
        # Route to the panel; no-op when delegation_id absent or
        # panel missing (mutator handles both).
        if delegation_id and inner:
            session_manager.apply_worker_panel_event(
                app_session_id, msg_id, delegation_id, inner,
            )
        if ctx.workers_list is not None:
            session_manager.snapshot_workers(
                app_session_id, msg_id, ctx.workers_list,
            )
        # events.jsonl gets the OUTER worker_event wrapper so
        # reconcile can re-apply through this same branch.
        self._publish_provider_event(
            write_journal,
            ctx,
            app_session_id=app_session_id,
            etype="worker_event",
            data=data,
            msg_id=msg_id,
            log_label="apply_event: worker_event ingest failed",
        )

    @perf.timed_fn("ingest_orphan")
    def ingest_orphan(
        self,
        *,
        app_session_id: str,
        event: dict,
        ctx: "ApplyEventCtx",
        source_is_provider_stream: bool,
    ) -> None:
        """Append a single event to events.jsonl with `msg_id=None`.

        Used when the source is the provider's CLI session jsonl but no
        streaming assistant_msg owns the event yet — e.g. the primary
        `OwnedClaudeJsonlTailer` fires after the orchestrator has
        finalized the turn. SRP-distinct from `apply_event` (which
        mutates the render tree). The orphan write reuses
        `event_ingester.ingest`'s built-in reconcile-dirty trigger
        (event_ingester.py:308-316), so a later read picks the line up
        and seq-brackets it onto the right assistant_msg.

        No-op when `source_is_provider_stream=False` or `ctx.root_id` is missing — replay
        paths must not double-write events.jsonl, and a ctx without a
        root has nowhere to write.
        """
        if not source_is_provider_stream or not ctx.root_id:
            return
        from event_ingester import event_ingester
        etype = event.get("type") or "unknown"
        data = event.get("data") or {}
        is_metadata = isinstance(data, dict) and self._apply_metadata_side_effects(
            app_session_id=app_session_id,
            data=data,
        )
        if is_metadata:
            self._ingest_metadata(
                source_is_provider_stream,
                source_is_provider_stream,
                ctx,
                etype,
                data,
                app_session_id,
                None,
            )
            return
        # Pre-flight UUID check: skip events already known to the
        # ingester.  This catches the case where the OwnedClaudeJsonlTailer
        # restarts after root eviction with a stale offset and re-reads
        # events that were already live-ingested.  The ingester's own
        # uid:sha256 dedup inside `_ingest_impl` is the authoritative
        # guard; this check is a cheaper O(1) shortcut that avoids the
        # executor round-trip for known duplicates.
        uid = event_ingester._extract_uuid(data)
        if uid and event_ingester.has_uid(ctx.root_id, uid):
            return
        self._publish_provider_event(
            source_is_provider_stream,
            ctx,
            app_session_id=app_session_id,
            etype=etype,
            data=data,
            msg_id=None,
            log_label="ingest_orphan failed",
        )


    @perf.timed_fn("apply_event")
    def apply_event(
        self,
        *,
        app_session_id: str,
        msg: dict,
        event: dict,
        ctx: ApplyEventCtx,
        source_is_provider_stream: Optional[bool] = None,
        write_journal: Optional[bool] = None,
        live: Optional[bool] = None,
    ) -> None:
        """Apply one session event to the render tree.

        Idempotent on event uuid: re-applying an already-present event
        is a no-op. Shared work for every mode: dedup, append to events
        list, wire user_claude_uuid from manager_event with a user
        claude entry, capture manager sid from turn_start/complete,
        snapshot workers. Mode-specific work runs in `_after_event`.

        `source_is_provider_stream=True` enables live-only side effects.
        `write_journal=True` also writes to events.jsonl; live SDK callbacks
        publish first and then call this with `write_journal=False`.
        `source_is_provider_stream=False` skips live side effects because
        events.jsonl is the source during replay. All publish_event_sync calls here MUST use
        timeout=0 because apply_event runs inside session_manager.batch() which
        holds the per-root RLock; the executor thread's event_ingester.ingest calls
        mark_reconcile_dirty which also acquires that lock — blocking would deadlock.
        """
        if source_is_provider_stream is None:
            if live is None:
                raise TypeError(
                    "apply_event requires source_is_provider_stream=... "
                    "(legacy alias: live=...)"
                )
            source_is_provider_stream = live
        elif live is not None and live != source_is_provider_stream:
            raise ValueError(
                "apply_event got conflicting source_is_provider_stream= "
                f"{source_is_provider_stream!r} and live={live!r}"
            )
        if write_journal is None:
            write_journal = source_is_provider_stream

        msg_id = msg.get("id")
        if not msg_id:
            return

        event = _unwrap_typed_worker_envelope(event)
        etype = event.get("type")
        data = event.get("data") or {}

        # Metadata-only events: no UUID, not rendered, consumed for
        # side-effects only.  Persist to events.jsonl so crash recovery
        # can replay them, but skip msg.events (the REST snapshot).
        # Checked BEFORE file-ref rewrite since metadata events have no
        # file paths to linkify.
        normalized = _normalize_for_render(event)
        normalized_data = normalized.get("data") or {}
        # Canonical event shape for events.jsonl.  All three ingestion
        # paths (live SDK callback, OwnedClaudeJsonlTailer, crash-recovery
        # replay) must produce the same on-disk shape so the convergence
        # invariant holds.  _normalize_for_render unwraps manager_event
        # envelopes into the inner agent_message — that is the canonical
        # form.  Use it for both the render tree AND events.jsonl writes.
        norm_etype = normalized.get("type") or etype or "unknown"
        norm_data = normalized.get("data") if isinstance(normalized.get("data"), dict) else data

        # Attention markers MUST be detected on RAW assistant text, BEFORE
        # the file-ref/tag rewrite below strips the `<TAG>` wrapper out of
        # norm_data. Captured here, applied once the event lands on the
        # render tree. Live path only — replay re-detection is idempotent
        # via set_marker's change-gate, but markers are a live signal.
        attention_markers: list[tuple[str, dict]] = []
        if source_is_provider_stream and etype in self._RENDER_TREE_ETYPES:
            import file_ref_resolver
            attention_markers = file_ref_resolver.detect_markers(
                _agent_message_text(norm_data)
            )

        if self._apply_metadata_side_effects(
            app_session_id=app_session_id,
            data=normalized_data,
        ):
            self._ingest_metadata(
                source_is_provider_stream,
                write_journal,
                ctx,
                norm_etype,
                norm_data,
                app_session_id,
                msg_id,
            )
            return

        if etype == "worker_start":
            delegation_id = data.get("delegation_id")
            if delegation_id:
                panel = {
                    "delegation_id": delegation_id,
                    "worker_session_id": data.get("worker_session_id") or "",
                    "worker_description": data.get("worker_description") or delegation_id,
                    "panel_kind": data.get("panel_kind") or "worker",
                    "started_at": data.get("started_at"),
                    "insert_at": data.get("insert_at")
                    if isinstance(data.get("insert_at"), (int, float))
                    else len(self._events_list(msg)),
                    "orchestration_mode": data.get("orchestration_mode"),
                    "is_new": bool(data.get("is_new", False)),
                    "instructions_preview": data.get("instructions_preview") or "",
                    "events": [],
                    "jsonl_path": data.get("jsonl_path"),
                    "new_byte_offset": data.get("new_byte_offset"),
                    "fork_agent_sid": data.get("fork_agent_sid"),
                    "run_mode": data.get("run_mode"),
                    "token_usage": data.get("token_usage"),
                }
                if ctx.workers_list is not None and not any(
                    p.get("delegation_id") == delegation_id
                    for p in ctx.workers_list
                ):
                    ctx.workers_list.append(panel)
                session_manager.upsert_worker_panel(app_session_id, msg_id, panel)
            self._publish_provider_event(
                write_journal,
                ctx,
                app_session_id=app_session_id,
                etype="worker_start",
                data=data,
                msg_id=msg_id,
                log_label="apply_event: worker_start ingest failed",
            )
            return

        if etype == "worker_complete":
            delegation_id = data.get("delegation_id")
            if delegation_id:
                fields = {
                    "worker_session_id": data.get("worker_session_id"),
                    "jsonl_path": data.get("jsonl_path"),
                    "new_byte_offset": data.get("new_byte_offset"),
                    "token_usage": data.get("token_usage"),
                    "success": data.get("success"),
                    "error": data.get("error"),
                    "fork_agent_sid": data.get("fork_agent_sid"),
                    "run_mode": data.get("run_mode"),
                }
                session_manager.update_worker_panel(
                    app_session_id,
                    msg_id,
                    str(delegation_id),
                    {k: v for k, v in fields.items() if v is not None},
                )
                if ctx.workers_list is not None:
                    panel = next(
                        (p for p in ctx.workers_list
                         if p.get("delegation_id") == delegation_id),
                        None,
                    )
                    if panel is not None:
                        panel.update({k: v for k, v in fields.items() if v is not None})
            self._publish_provider_event(
                write_journal,
                ctx,
                app_session_id=app_session_id,
                etype="worker_complete",
                data=data,
                msg_id=msg_id,
                log_label="apply_event: worker_complete ingest failed",
            )
            return

        # `worker_event` frames wrap a worker's inner agent_message under
        # `data.event`. They MUST route to the matching panel's events
        # list — NOT to the primary `msg.events`.
        if etype == "worker_event":
            self._apply_worker_event(
                app_session_id=app_session_id,
                msg_id=msg_id,
                data=data,
                ctx=ctx,
                source_is_provider_stream=source_is_provider_stream,
                write_journal=write_journal,
            )
            return

        # Rewrite file refs BEFORE appending to the session JSON so the
        # persisted events carry bcfile: links.  The ingester also rewrites
        # (it writes to events.jsonl), but the session JSON is persisted
        # first by _append_event — without this early call the on-disk
        # session would have the raw, un-linkified text.
        if source_is_provider_stream:
            from file_ref_resolver import (
                assume_exists_for_session, rewrite_event_data,
            )
            try:
                # `get_lite` skips the msg.events deepcopy — we only
                # need `cwd` for file-ref rewrite. Per-event hot path:
                # `get()` was 97 ms on the 13 MB heavy session; lite
                # is ~3 ms.
                _sess = session_manager.get_lite(app_session_id) or {}
                _cwd = _sess.get("cwd")
                rewrite_event_data(
                    etype or "unknown", data, _cwd,
                    assume_exists=assume_exists_for_session(_sess),
                )
            except Exception:
                import logging
                logging.getLogger(__name__).debug(
                    "file_ref_resolver rewrite failed", exc_info=True,
                )

        # INVARIANT: `msg.events` is the NARROW render tree. Only event
        # types that the frontend's MessageBubble switch actually renders
        # (`agent_message`, `steer_prompt`, and legacy `manager_event`) belong here.
        # `_normalize_for_render` unwraps both into uniform agent_message
        # before storage. Non-render etypes (REST `command_received`,
        # `run_state`, lifecycle `user_message_*`, `messages_delta`,
        # `trace_step`, `turn_start`/`complete`) ride only on
        # `events.jsonl` for the frontend's audit-trail WS broadcast —
        # they MUST NOT land on msg.events.
        #
        # The gate also closes the reconcile orphan-bracketing leak:
        # `_reconcile_msg_events_from_jsonl` brackets every orphan
        # (msg_id=None) onto the nearest assistant msg by seq window
        # and re-calls `apply_event(source_is_provider_stream=False)`.
        # Without the gate, any orphan with a `data.uuid` would land on
        # msg.events.
        # Idempotence: an event with a claude uuid is durable and only
        # belongs on msg.events once.
        # UPDATE: if the event content changed, we replace the existing
        # entry to support streaming updates (e.g. Gemini).
        ev_uuid = _event_uuid(event)
        evs = self._events_list(msg)
        if ev_uuid and etype in self._RENDER_TREE_ETYPES:
            # O(1) lookup via cached `_uid_idx` dict on the events-list
            # owner. Pre-uid_idx this was a per-call linear scan over
            # `evs`, making `apply_event(source_is_provider_stream=False)` × N during cold-
            # load hydration O(N²) — 6 s measured for 5268 events on
            # session 4ddbd4d7. Now O(1) per call → O(N) total.
            owner = self._events_owner(msg)
            uid_idx = _uid_idx_for(owner, evs)
            existing_idx = uid_idx.get(ev_uuid)
            if existing_idx is not None:
                existing = evs[existing_idx]
                if existing == normalized:
                    # Identical re-apply: full no-op for both render
                    # tree and events.jsonl. Early-return is safe here
                    # because `event_ingester.ingest`'s `uid:sha256(data)`
                    # dedup would no-op too — we just skip the call.
                    return
                # Mutated data (Gemini streaming, in-place updates,
                # or any provider re-emitting same uuid). Replace in
                # msg.events, then FALL THROUGH to the events.jsonl
                # ingest tail so disk also sees the new snapshot.
                #
                # INVARIANT (CLAUDE.md "Dedup semantics differ by
                # surface"): msg.events dedupes by uuid alone
                # (REPLACE on mutation); events.jsonl dedupes by
                # `uid:sha256(data)` (APPEND on mutation). If we
                # early-returned here, events.jsonl would only ever
                # hold the FIRST snapshot of a streaming uuid. After
                # a backend restart, `_reconcile_msg_events_from_jsonl`
                # would re-apply that stale FIRST snapshot against
                # the (correctly-persisted-via-_replace_event) LATEST
                # in msg.events, REGRESSING the render tree to the
                # older snapshot. Falling through closes that
                # regression — the ingester's uid:sha256(data) dedup
                # makes same-data idempotent, and mutated data lands
                # as a new row in seq order so reconcile-replay's
                # last-write-wins gives the latest snapshot back.
                #
                # NOTE: `bump_unread` MUST stay in the append branch
                # below — incrementing on mutation would double-count
                # for every streaming snapshot.
                self._replace_event(app_session_id, msg_id, normalized, ev_uuid)
                evs[existing_idx] = normalized
                # uid_idx[ev_uuid] unchanged — same uuid, same index.
                # Fall through to side-effect blocks + ingest tail.
            else:
                # APPEND new uuid path: persist the scaffolded event,
                # update local mirror if not already updated by
                # `_append_event`, and bump the unread counter on the
                # live path.
                #
                # `bump_unread` MUST stay scoped to this append branch.
                # Replace path above (mutated data) MUST NOT bump —
                # Gemini streaming would otherwise increment unread on
                # every cumulative-text snapshot.
                #
                # `source_is_provider_stream` gate: the replay path
                # (`_reconcile_msg_events_from_jsonl`,
                # `run_recovery._apply_integration_sync`) re-applies
                # events already on disk. Bumping there would (a) fire
                # one `broadcast_global` per re-applied event = unbounded
                # WS spam on every cold session load, and (b) double-count
                # — `_count_unread_from_disk` on lazy hydration already
                # walks the same `msg.events` we'd be re-bumping. Live
                # is the only path with a genuinely-new event.
                self._append_event(app_session_id, msg_id, normalized)
                # Replay path may have left the mirror in sync via
                # the mutator; track whether THIS call mutates `evs`
                # so uid_idx stays consistent. Check by index against
                # uid_idx (the mutator updates that too — see
                # `append_native_event`).
                if ev_uuid not in uid_idx:
                    uid_idx[ev_uuid] = len(evs)
                    evs.append(normalized)
                if source_is_provider_stream:
                    session_manager.bump_unread(app_session_id, msg_id)

            # Set attention markers detected on the raw assistant text
            # (captured pre-strip above). Change-gated, so re-detecting the
            # same tag across streaming deltas broadcasts at most once.
            for ext_id, marker in attention_markers:
                if ext_id:
                    session_manager.set_marker(app_session_id, ext_id, marker)
                if marker.get("tag") == _ALL_TASKS_DONE_MARKER_TAG:
                    _complete_current_todos(app_session_id)

            import session_event_extensions
            session_event_extensions.apply_event(
                app_session_id,
                normalized,
                use_sdk=source_is_provider_stream,
            )

            # ── Provenance hook (what ran + WHY) ──────────────────
            # Append-only log of tool invocations + the reasoning that
            # preceded them. The `source_is_provider_stream` gate only skips the reconcile path
            # (events.jsonl re-read); crash-recovery replay IS live=True
            # (run_recovery.py), so idempotency CANNOT rely on this flag —
            # provenance_store dedups by tool_use id with a dedup set
            # hydrated from disk on first touch, so a recovered turn does
            # not double-write. Worker_event already early-returned above,
            # so worker tool calls don't land here.
            if source_is_provider_stream:
                session_manager.apply_provenance_from_event(
                    app_session_id, normalized,
                )

        if etype == "turn_start":
            sid = data.get("manager_session_id")
            if sid and ctx.manager_sid_holder is not None:
                ctx.manager_sid_holder["id"] = sid
        elif etype == "turn_complete":
            sid = data.get("session_id")
            if sid and ctx.manager_sid_holder is not None:
                ctx.manager_sid_holder["id"] = sid
        elif etype in ("agent_message", "manager_event"):
            # Wire user_claude_uuid from the first non-sidechain user-typed
            # line. After _normalize_for_render the user entry can be at two
            # levels: (a) under normalized.data (agent_message wrapping user)
            # or (b) on normalized itself (legacy manager_event wrapping a
            # raw user entry with no intermediate agent_message).
            nd = normalized_data
            if not (isinstance(nd, dict) and nd.get("type") == "user"):
                if normalized.get("type") == "user":
                    nd = normalized
            if (
                ctx.user_msg is not None
                and ctx.user_msg.get("agent_message_uuid") is None
                and isinstance(nd, dict)
                and nd.get("type") == "user"
                and not nd.get("isSidechain")
            ):
                inner_msg = nd.get("message") or {}
                content = inner_msg.get("content")
                if isinstance(content, str) and nd.get("uuid"):
                    session_manager.set_user_agent_uuid(
                        app_session_id,
                        ctx.user_msg["id"],
                        nd["uuid"],
                    )
                    if source_is_provider_stream:
                        self._fire_user_msg_received_if_pending(
                            app_session_id=app_session_id,
                            agent_user_uuid=nd["uuid"],
                        )

        if ctx.workers_list is not None:
            session_manager.snapshot_workers(
                app_session_id, msg_id, ctx.workers_list,
            )

        self._after_event(
            app_session_id=app_session_id,
            msg=msg,
            event=event,
            ctx=ctx,
            source_is_provider_stream=source_is_provider_stream,
        )

        self._publish_provider_event(
            write_journal,
            ctx,
            app_session_id=app_session_id,
            etype=norm_etype,
            data=norm_data,
            msg_id=msg_id,
            log_label="apply_event: event_ingester.ingest failed",
        )

    @abstractmethod
    def finalize_turn(
        self,
        *,
        app_session_id: str,
        assistant_msg: dict,
        primary_result: dict,
    ) -> None:
        """Mode-specific finalization after a turn completes.

        Pin session ids, promote recovered placeholders, etc.
        Must be called inside session_manager.batch(...).
        """
