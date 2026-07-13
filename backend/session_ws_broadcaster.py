"""Bridges SessionManager change events to WebSocket broadcasts.

Wires one global listener on the SessionManager singleton that fans
typed change events out as `session_metadata_updated` WS frames. This
removes the duplicate `_broadcast_session_metadata` calls that
previously had to be appended after every REST mutation in main.py —
the listener is the single place where that mapping lives.

Threading: SessionManager fires listeners synchronously inside the
per-sid lock. The actual WS send is async; we schedule it on the
running event loop with `create_task` (or fall back to the bound loop
if the listener fires from a non-async thread).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from messages_delta_compaction import compact_message_delta_payload

logger = logging.getLogger(__name__)

# Per-process set of unknown change kinds we've already warned about.
# The warning below is "loud on first occurrence" — once a novel kind
# is logged, suppress further warnings for the same kind in this
# process. Without this dedup the warning hammered the log at sustained
# rates (`processed_lines_advanced` alone fired ~5000×/day per backend
# trace), turning the boot/load-time hot paths into sync-I/O bound work
# (every dispatch did a `logger.warning` → format + file write). Some
# kinds that legitimately have no WS mapping are listed in
# `_INTERNAL_KINDS` below and bypass the warning entirely; the dedup
# catches anything that slips through that filter.
_warned_unknown_kinds: set[str] = set()


_METADATA_KINDS = {
    "tag_added",
    "tag_removed",
    "tag_updated",
    "tags_cleared",
    "open_panels_set",
    "draft_set",
    "fork_closed_set",
    "supervisor_enabled_set",
    "supervisor_separated",
    "pending_supervisor_verdict_set",
    "pending_supervisor_verdict_cleared",
    "pinned_set",
    "topbar_pinned_set",
    "archived_set",
    "all_projects_set",
    "worker_eligible_set",
    "worker_creation_policy_set",
    "capability_contexts_set",
    "active_capability_added",
    "active_capability_removed",
    "working_mode_marked",
    "adv_sync_updated",
    "msg_ask_result_set",
    "msg_ask_choice_set",
    "notes_updated",
    "right_panel_set",
    "todos_updated",
    "todos_snapshot",
    "tasks_updated",
    "queued_prompts_updated",
    "last_opened_set",
}

# Change kinds that are internal-only — they don't need WS frames because
# the frontend already receives this data through other channels (turn
# events, event stream, REST snapshot on reconnect).
_INTERNAL_KINDS = {
    "agent_sid_set",
    "parent_deleted",
    "worker_fanout_required",
    "workers_snapshot",
    "worker_panel_event",
    "worker_panel_upserted",
    "worker_panel_updated",
    "delegate_fork_created",
    "native_event_appended",
    "native_event_replaced",
    "native_events_set",
    "streaming_set",
    "stopped_at_set",
    "processed_lines_advanced",
    "assistant_msg_removed",
    "trace_id_set",
    "session_token_usage_added",
    "user_claude_uuid_set",
    "context_window_set",
    "context_tokens_set",
    "agent_rename_allowed_set",
    "backend_url_set",
    "bare_config_set",
    "disabled_builtin_extensions_set",
    "disallowed_tools_set",
    "forked_from_cleared",
    "forked_from_set",
    "forked_from_supervisor_cleared",
    "forked_from_supervisor_set",
    "migrated_fields_applied",
    "moved_from_set",
    "moved_to_set",
    "name_locked",
    "origin_set",
    "recovered_flag_cleared",
    "sub_session_created",
    "interrupted_by_set",
    "assistant_error_set",
    "supervisor_bootstrap_received_set",
    "messages_truncated",
    "agent_sid_on_msg_set",
    "msg_transient_attempt_set",
    "continuation_requested_set",
    "continuation_requested_cleared",
    "continuation_chain_set",
}


class SessionWSBroadcaster:
    def __init__(self, coordinator) -> None:
        self._coordinator = coordinator
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the loop the broadcaster falls back to when its
        listener fires from a non-async thread (e.g. SessionWatcher's
        background thread or a sync test helper)."""
        self._loop = loop

    def on_change(self, sid: str, change: dict) -> None:
        kind = change.get("kind")
        render_delta = change.get("render_delta")
        if isinstance(render_delta, dict):
            self._dispatch({
                "type": "render_delta",
                "data": {
                    "app_session_id": sid,
                    "incarnation": change.get("render_incarnation"),
                    "render_revision": change.get("render_revision"),
                    "delta": render_delta,
                },
            })
        if kind == "running_changed":
            # Per-session running-flag flip. Authoritative state is
            # computed live by `coordinator.is_running(sid)` (walks
            # `_run_state[sid]` + checks pid liveness); this is the
            # WS ping that tells the frontend to re-render the badge.
            # INVARIANT: payload carries `cwd` + `node_id` so the
            # frontend can update the per-project running_count locally
            # without a `/api/projects` refetch. Backend MUST NOT fire
            # `projects_changed` here — that was the refetch-storm
            # culprit (~132 calls/min under load).
            cwd, node_id = self._project_key_for(sid)
            self._dispatch({
                "type": "session_running_changed",
                "data": {
                    "session_id": sid,
                    "value": bool(change.get("value")),
                    "cwd": cwd,
                    "node_id": node_id,
                },
            })
            return
        if kind == "provenance_changed":
            # New provenance row(s) appended. The Details panel refetches
            # GET /api/sessions/{id}/details on this ping. Payload is just
            # the session id (the panel pulls the authoritative snapshot).
            self._dispatch({
                "type": "session_provenance_changed",
                "data": {"session_id": sid},
            })
            return
        if kind == "monitoring_changed":
            # Per-session monitoring-state transition (active / idle /
            # blocked_on_user / waiting_on_background / stopped). Authoritative
            # state is computed live by `coordinator.monitoring_state(sid)`.
            # This is the SINGLE state event the frontend registry consumes:
            # `is_running` is derived client-side as `state != "stopped"`, so
            # the payload carries `cwd` + `node_id` (like running_changed) to
            # route the per-project running_count aggregate + materialize a
            # not-yet-seen session — no separate running event needed.
            cwd, node_id = self._project_key_for(sid)
            self._dispatch({
                "type": "session_monitoring_changed",
                "data": {
                    "session_id": sid,
                    "monitoring_state": str(change.get("value")),
                    "cwd": cwd,
                    "node_id": node_id,
                },
            })
            return
        if kind == "unread_changed":
            # New event(s) appeared since last ack. Frontend reads the
            # count from the payload directly (no refetch needed).
            # INVARIANT: include `cwd` + `node_id` (see running_changed
            # above) — no `projects_changed` side-effect.
            cwd, node_id = self._project_key_for(sid)
            self._dispatch({
                "type": "session_unread_changed",
                "data": {
                    "session_id": sid,
                    "unread_count": int(change.get("unread_count") or 0),
                    "cwd": cwd,
                    "node_id": node_id,
                },
            })
            return
        if kind == "seen_advanced":
            # User acked viewing — unread_count is 0 in the change
            # payload. Same per-project key in the payload; no
            # `projects_changed` side-effect (see above).
            cwd, node_id = self._project_key_for(sid)
            self._dispatch({
                "type": "session_unread_changed",
                "data": {
                    "session_id": sid,
                    "unread_count": 0,
                    "last_seen_event_uid": change.get("last_seen_event_uid"),
                    "cwd": cwd,
                    "node_id": node_id,
                },
            })
            return
        if kind == "error_changed":
            # A turn ended in an unrecoverable error (set) or the dot was
            # retired by a view-ack / subsequent successful turn (clear).
            # Carries `cwd` + `node_id` like the other session-state frames.
            cwd, node_id = self._project_key_for(sid)
            self._dispatch({
                "type": "session_error_changed",
                "data": {
                    "session_id": sid,
                    "has_error": bool(change.get("has_error")),
                    "cwd": cwd,
                    "node_id": node_id,
                },
            })
            return
        if kind in ("marker_set", "marker_cleared"):
            # Extension attention marker on a session. Mirror unread/pinned:
            # carry `cwd` + `node_id` so the frontend can key the delta to
            # the right project node.
            cwd, node_id = self._project_key_for(sid)
            self._dispatch({
                "type": "session_marker_changed",
                "data": {
                    "session_id": sid,
                    "extension_id": change.get("extension_id"),
                    "marker": change.get("marker") if kind == "marker_set" else None,
                    "cwd": cwd,
                    "node_id": node_id,
                },
            })
            return
        if kind == "msg_recovering_set":
            # Per-message transient flag set by run_recovery while it
            # reconciles an in-flight run after a backend restart. Frontend
            # mirrors the flag onto the matching assistant message and
            # renders an "Updating state…" pill until cleared.
            self._dispatch({
                "type": "message_recovering_changed",
                "data": {
                    "session_id": sid,
                    "msg_id": change.get("msg_id"),
                    "value": bool(change.get("value")),
                },
            })
            return
        if kind == "message_ownership_resolved":
            self._dispatch({
                "type": "messages_delta",
                "data": {
                    "app_session_id": sid,
                    "messages": [change.get("msg")],
                },
            })
            return
        if kind in ("journal_event_projected", "historical_projection_changed"):
            delta = change.get("delta")
            if delta is None and change.get("msg") is not None:
                delta = compact_message_delta_payload(change["msg"])
            if delta is not None:
                self._dispatch({
                    "type": "messages_delta",
                    "data": {
                        "app_session_id": sid,
                        "messages": [delta],
                    },
                })
            return
        if kind == "completed_at_set":
            msg = change.get("msg")
            if msg is not None:
                self._dispatch({
                    "type": "messages_delta",
                    "data": {
                        "app_session_id": sid,
                        "messages": [compact_message_delta_payload(msg)],
                    },
                })
            return
        if kind == "running_content_updated":
            self._dispatch({
                "type": "message_content_updated",
                "data": {
                    "session_id": sid,
                    "msg_id": change.get("msg_id"),
                    "content": change.get("content") or "",
                },
            })
            return
        if kind == "user_msg_marked_error":
            # The user prompt's persist-ack (`user_message_persisted`) is
            # broadcast at append time, before the turn runs. If the turn
            # later errors and stamps status=error on the user message,
            # the persisted frame on the client has stale status. Push
            # the updated snapshot so the canonical user message reflects
            # the failure — otherwise the brief error flash from the
            # `error` WS event is overwritten by the stale persisted msg.
            msg = change.get("msg")
            if msg is not None:
                self._dispatch({
                    "type": "messages_delta",
                    "data": {
                        "app_session_id": sid,
                        "messages": [compact_message_delta_payload(msg)],
                    },
                })
            return
        if kind in ("user_msg_appended", "assistant_msg_appended"):
            msg = change.get("msg")
            if msg is not None:
                self._dispatch({
                    "type": "messages_delta",
                    "data": {
                        "app_session_id": sid,
                        "messages": [compact_message_delta_payload(msg)],
                    },
                })
            return
        if kind == "msg_retrying_set":
            # Per-message marker the orchestrator stamps while it sleeps
            # between a 429 rate-limit response and the next retry. The
            # frontend mirrors `retrying_until` onto the assistant
            # message and renders a 'Retrying in Ns…' pill that ticks
            # locally — no further WS traffic needed during the sleep.
            self._dispatch({
                "type": "message_retrying_changed",
                "data": {
                    "session_id": sid,
                    "msg_id": change.get("msg_id"),
                    "retry_at": change.get("retry_at"),
                },
            })
            return
        if kind == "msg_auto_retry_set":
            # A turn that succeeded only after >=1 automatic retry. Durable
            # marker on the message; clients badge the turn so the recovery
            # is distinguishable from a clean first-try run.
            self._dispatch({
                "type": "message_auto_retry_changed",
                "data": {
                    "session_id": sid,
                    "msg_id": change.get("msg_id"),
                    "auto_retry": change.get("auto_retry"),
                },
            })
            return
        if kind == "msg_continuation_set":
            self._dispatch({
                "type": "message_continuation_changed",
                "data": {
                    "session_id": sid,
                    "msg_id": change.get("msg_id"),
                    "chain_depth": change.get("chain_depth"),
                },
            })
            return
        if kind == "msg_run_meta_set":
            # Per-turn provider/model/effort actually used. Re-stamped on
            # each retry iteration so a mid-message selector switch updates
            # the badge to the provider that ran the succeeding attempt.
            self._dispatch({
                "type": "message_run_meta_changed",
                "data": {
                    "session_id": sid,
                    "msg_id": change.get("msg_id"),
                    "run_meta": change.get("run_meta"),
                },
            })
            return
        if kind == "msg_ask_result_set":
            # Per-turn Ask picker payload (`propose_sessions` result) stamped
            # on the producing assistant message. The frontend renders the
            # inline session picker from this field, per turn.
            self._dispatch({
                "type": "message_ask_result_changed",
                "data": {
                    "session_id": sid,
                    "msg_id": change.get("msg_id"),
                    "ask_result": change.get("ask_result"),
                },
            })
            return
        if kind == "msg_ask_choice_set":
            # Which session the user chose from a turn's picker — keeps the
            # chosen row highlighted across reloads / tabs / previous turns.
            self._dispatch({
                "type": "message_ask_choice_changed",
                "data": {
                    "session_id": sid,
                    "msg_id": change.get("msg_id"),
                    "chosen_session_id": change.get("chosen_session_id"),
                },
            })
            return
        if kind == "forked":
            self._dispatch({
                "type": "session_forked",
                "data": {
                    "session": change.get("session"),
                    "parent_session_id": change.get("parent_session_id"),
                },
            })
            return
        if kind == "created":
            # Multi-tab convergence (INV-3 / DIV-4): a new session in
            # tab A must appear in tab B's sidebar without a manual
            # refresh. Filter ephemeral sessions (file-edit /
            # engineering / plan workers — anything with working_mode
            # set) so they don't leak into the sidebar; this mirrors
            # `useSession.fetchSessions`'s `!working_mode` filter.
            sess = change.get("session") or {}
            if sess.get("working_mode"):
                return
            self._dispatch({
                "type": "session_created",
                "data": {"session": sess},
            })
            return
        if kind == "deleted":
            # Multi-tab convergence: a session deleted in tab A must
            # disappear from tab B's sidebar without a refresh. Fires
            # for every delete (root or fork) — frontend dedups by id.
            self._dispatch({
                "type": "session_deleted",
                "data": {"session_id": sid},
            })
            return
        if kind == "renamed":
            # Multi-tab convergence for rename. Replaces the manual
            # `await ws_callback({"type":"session_renamed",...})` that
            # used to live in `orchestrator._run_turn` — that path only
            # reached the active-turn WS subscriber; this listener
            # reaches every connected tab via broadcast_global.
            self._dispatch({
                "type": "session_renamed",
                "data": {
                    "session_id": sid,
                    "name": change.get("name"),
                },
            })
            return
        if kind == "selectors_set":
            # Multi-tab convergence (INV-3 / DIV-4) for model/cwd/
            # provider_id changes. Originating tab is filtered
            # downstream via `originated_by`.
            patch: dict = {}
            if change.get("model") is not None:
                patch["model"] = change["model"]
            if change.get("reasoning_effort") is not None:
                patch["reasoning_effort"] = change["reasoning_effort"]
            if change.get("cwd") is not None:
                patch["cwd"] = change["cwd"]
            if change.get("provider_id") is not None:
                patch["provider_id"] = change["provider_id"]
            if not patch:
                return
            self._dispatch({
                "type": "session_metadata_updated",
                "data": {
                    "session_id": sid,
                    "patch": patch,
                    "originated_by": change.get("client_id"),
                },
            })
            return
        if kind in _INTERNAL_KINDS:
            return
        if kind not in _METADATA_KINDS:
            # Loud-on-FIRST-occurrence: log a warning the first time we
            # see a novel unknown change kind, then suppress further
            # warnings for the same kind in this process. Without the
            # `_warned_unknown_kinds` dedup the per-kind rate (e.g.
            # `processed_lines_advanced` fires on every tailed line)
            # turned the dispatch into sync-I/O-bound work and visibly
            # slowed backend boot + session load. The first-occurrence
            # warning still surfaces a missing WS-frame mapping for a
            # novel kind (the original "loud-on-unknown" intent — past
            # bug: `running_content_updated` left assistant bubble blank
            # because no WS frame ever fired). Add the new kind to
            # `_INTERNAL_KINDS` (silent drop) or extend `on_change` to
            # emit a frame, depending on whether the frontend needs it.
            if kind not in _warned_unknown_kinds:
                _warned_unknown_kinds.add(kind)
                logger.warning(
                    "session_ws_broadcaster.on_change: dropping unknown "
                    "change kind %r (sid=%s) — no WS frame mapping. "
                    "Further occurrences of this kind suppressed.",
                    kind, sid,
                )
            return
        client_id = change.get("client_id")
        if kind == "draft_set":
            patch = {
                "draft_input": change.get("text"),
                "draft_input_seq": change.get("seq"),
            }
            if "images" in change:
                patch["draft_images"] = change["images"]
        elif kind == "fork_closed_set":
            patch = {"fork_closed": bool(change.get("value"))}
        elif kind == "supervisor_enabled_set":
            patch = {"supervisor_enabled": bool(change.get("value"))}
            if "supervisor_custom_prompt" in change:
                patch["supervisor_custom_prompt"] = change["supervisor_custom_prompt"]
        elif kind == "supervisor_separated":
            # Original session X's view after its supervisor was
            # graduated into a new root Y. Carries the post-mutation
            # state the frontend needs to drop its cached "supervisor
            # already running" markers.
            patch = {
                "supervisor_agent_session_id": None,
                "forked_from_supervisor_agent_sid": change.get(
                    "old_supervisor_sid",
                ),
                "supervisor_bootstrap_received": False,
            }
        elif kind == "pinned_set":
            patch = {"pinned": bool(change.get("value"))}
        elif kind == "topbar_pinned_set":
            patch = {
                "topbar_pinned": bool(change.get("value")),
                "topbar_pinned_at": change.get("topbar_pinned_at"),
            }
        elif kind == "archived_set":
            patch = {"archived": bool(change.get("value"))}
        elif kind == "all_projects_set":
            # Cross-project visibility flag (e.g. the assistant singleton,
            # whose cwd is the user home but must appear in every project).
            patch = {"all_projects": bool(change.get("all_projects"))}
        elif kind == "worker_eligible_set":
            patch = {"worker_eligible": bool(change.get("value"))}
        elif kind == "worker_creation_policy_set":
            patch = {"worker_creation_policy": change.get("policy") or "ask"}
        elif kind == "capability_contexts_set":
            patch = {
                "capability_contexts": list(
                    change.get("capability_contexts") or []
                )
            }
        elif kind in ("active_capability_added", "active_capability_removed"):
            from session_manager import manager as _sm
            patch = {
                "active_capability_ids": list(
                    _sm.get_field(sid, "active_capability_ids") or []
                )
            }
        elif kind == "open_panels_set":
            # The change payload is enriched by SessionManager._run
            # with the full post-mutation `open_file_panels` list, so
            # no reach-back into the singleton is needed.
            patch = {
                "open_file_panels": list(change.get("open_file_panels") or [])
            }
        elif kind == "open_config_panels_set":
            # Mirrors open_panels_set for the provider-config-sync
            # capability panels popped into the right side panel.
            patch = {
                "open_config_panels": list(change.get("open_config_panels") or [])
            }
        elif kind == "working_mode_marked":
            # File-set / working-mode changes are backend-owned; the
            # enriched change carries the full post-mutation meta so
            # any open client (e.g. the multi-file editor overlay)
            # reflects an added file without polling.
            patch = {
                "working_mode": change.get("working_mode"),
                "working_mode_meta": change.get("working_mode_meta"),
            }
        elif kind == "adv_sync_updated":
            # Adversarial-sync overlay add/update. Enriched payload
            # ships the full post-mutation list so any open client
            # converges without polling.
            patch = {
                "adv_sync_overlays": list(
                    change.get("adv_sync_overlays") or []
                )
            }
        elif kind == "notes_updated":
            # Per-session scratchpad notes. Enriched payload carries
            # the full post-mutation list for cross-tab convergence.
            patch = {
                "notes": list(change.get("notes") or [])
            }
        elif kind == "todos_updated":
            patch = {
                "current_todos": list(change.get("current_todos") or [])
            }
        elif kind == "todos_snapshot":
            # Inline chat snapshot of current todos. Dispatched as a
            # standalone frame so the frontend routes it onto the
            # streaming message's events array (not session metadata).
            self._dispatch({
                "type": "todos_snapshot",
                "data": {
                    "app_session_id": sid,
                    "session_id": sid,
                    "todos": list(change.get("todos") or []),
                },
            })
            return
        elif kind == "tasks_updated":
            # TaskCreate / TaskUpdate derived task list. Stored and
            # broadcast separately from current_todos.
            patch = {
                "current_tasks": list(change.get("current_tasks") or [])
            }
        elif kind == "right_panel_set":
            # Right-panel UI state. Patch carries only the keys that
            # were actually mutated; other tabs
            # apply them via applySessionMetadata.
            patch = {}
            if "right_panel_open" in change:
                patch["right_panel_open"] = bool(change["right_panel_open"])
            if "right_panel_active_tab" in change:
                patch["right_panel_active_tab"] = change["right_panel_active_tab"]
            if "right_panel_width" in change:
                patch["right_panel_width"] = change["right_panel_width"]
            if "right_panel_mobile_height" in change:
                patch["right_panel_mobile_height"] = change["right_panel_mobile_height"]
            if "right_panel_todos_dismissed" in change:
                patch["right_panel_todos_dismissed"] = bool(change["right_panel_todos_dismissed"])
            if "right_panel_auto_opened_by" in change:
                patch["right_panel_auto_opened_by"] = list(change.get("right_panel_auto_opened_by") or [])
            if "sidebar_minimized" in change:
                patch["sidebar_minimized"] = bool(change["sidebar_minimized"])
        elif kind == "queued_prompts_updated":
            patch = {
                "queued_prompts": list(change.get("queued_prompts") or [])
            }
        elif kind == "last_opened_set":
            patch = {"last_opened_at": change.get("at")}
        else:
            # Tag changes. Enriched payload carries the full
            # post-mutation inline_tags list.
            patch = {"inline_tags": list(change.get("inline_tags") or [])}

        payload = {
            "type": "session_metadata_updated",
            "data": {
                "session_id": sid,
                "patch": patch,
                "originated_by": client_id,
            },
        }
        self._dispatch(payload)

    def _project_key_for(self, sid: str) -> tuple[str, str]:
        """Resolve `(cwd, node_id)` for the session — the per-project
        key the frontend uses to update aggregates locally on
        `session_running_changed` / `session_unread_changed`.

        Delegates to `session_manager.get_project_key` which reads
        just the two fields under the per-root lock without the deep
        copy `get()` would do. Critical for perf — this is on the hot
        path of every running/unread/seen change (was previously
        copying ~12 MB session trees per delta when we used `get()`).

        INVARIANT: returns `("", node_id)` whenever the session must NOT
        contribute to the sidebar's per-project aggregate (missing
        session, or `should_hide_from_sidebar`). Frontend uses
        `cwd === ""` as the "skip aggregate" signal — matches backend's
        `_project_aggregates` filter (main.py:761).

        Import is lazy because the broadcaster is constructed before
        `session_manager.manager` fully initializes at app startup."""
        from session_manager import manager as _sm
        try:
            return _sm.get_project_key(sid)
        except Exception:
            return ("", "primary")

    def _dispatch(self, payload: dict) -> None:
        # Generic global-broadcast fan-out used by every typed mapping
        # in `on_change`. `payload` is `{"type": <wire-event>, "data": {...}}`
        # — an invalidation ping; authoritative state lives in
        # session_store / session_manager transient sets, frontend
        # refetches or reads the payload directly per event-type.
        try:
            loop = asyncio.get_running_loop()
            self._coordinator.schedule_global(
                payload["type"], payload["data"], loop=loop,
            )
            return
        except RuntimeError:
            pass
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._coordinator.schedule_global(
                    payload["type"], payload["data"], loop=self._loop,
                )
                return
            except Exception:
                logger.exception("WS broadcast schedule failed")
        # No loop available — drop. Caller (typically a sync test
        # helper) won't see the WS frame, but that's the price of
        # firing from a non-async context with no bound loop.
