"""Concrete SessionSurface implementation (ADR 0008).

Reads exclusively through `backend.adapters.store_access.store_access`;
live deltas come from the `session.fire.*` owner-side fact
(`session_manager._fire`, additive alongside its `session.<kind>`
WS-broadcaster event — see session_manager.py's `_fire` docstring),
`session.status_projected` (backend/session_status_projection.py:107-144,
the richer per-sid running/waiting_for_user/errored projection),
`lifecycle.turn_*`, and `project.fire.*` (the owner-side fact
`backend/session_commands.py`'s `SessionCommandPort` implementation
publishes after a project mutation — see that module). The fact/lifecycle
handlers re-pull the affected session record and re-map it — see
`_refresh` — so a tick that doesn't change the record's fields (the
common case) never re-broadcasts. Rollup state (`SessionSummary.state`)
is instead tracked in a separate per-sid `_RollupFlags` cache fed by the
fact sources — see `_derive_rollup`.

Every field this adapter cannot honestly derive from
`backend.adapters.store_access.StoreAccess`'s current surface is called
out inline as a `# gap:` comment rather than guessed at (CLAUDE.md:
"never invent"); the caller's report enumerates them.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, replace
from datetime import datetime

from backend.adapters.command_port import SessionCommandPort
from backend.adapters.projection import BusBoundProjection
from backend.adapters.store_access import SessionOrganizationRecord, SessionRecord, store_access
from backend.event_bus import BusEvent
from backend.surface_contract.identity import (
    CONTRACT_VERSION,
    Emit,
    Ok,
    PageCursor,
    ProjectRef,
    ProjectionResult,
    Rebuilding,
    SessionId,
    SessionSelectors,
    StaleCursor,
    Subscription,
)
from backend.surface_contract.intents import (
    ArchiveSession,
    AssignFolder,
    AssignProject,
    AssignTags,
    CreateFolder,
    CreateProject,
    CreateTag,
    DeleteFolder,
    DeleteProject,
    DeleteTag,
    IntentAccepted,
    IntentRejected,
    MarkOpened,
    MoveFolder,
    RecolorTag,
    RenameFolder,
    RenameProject,
    RenameSession,
    RenameTag,
    SessionIntent,
    TransportAck,
)
from backend.surface_contract.session_surface import (
    AttentionMarkerDetail,
    FolderRemoved,
    FolderUpsert,
    Project,
    ProjectRemoved,
    ProjectUpsert,
    SessionFolder,
    SessionPage,
    SessionRemoved,
    SessionRollupState,
    SessionSearchFilters,
    SessionSummary,
    SessionSurface,
    SessionSummaryUpsert,
    SessionTag,
    SessionTreeChanged,
    SessionTreeNode,
    TagAssignment,
    TagRemoved,
    TagUpsert,
)

logger = logging.getLogger(__name__)

_PAGE_SIZE = 50
# Opaque container id for the paginated session list — there is no single
# session this cursor belongs to, unlike chat surface cursors.
_LIST_SURFACE_ID = "sessions:list"

# `session.fire.*` kinds that change a root's fork *structure* (not just
# one node's own fields) — a live subscriber viewing that root's tree
# needs to re-fetch `session_tree()`. See `_on_session_fire`.
_TREE_STRUCTURE_KINDS = frozenset({"forked", "fork_closed_set", "renamed"})


class _SubscriptionImpl:
    def __init__(self, close_fn) -> None:
        self._close_fn = close_fn

    def close(self) -> None:
        self._close_fn()


def _parse_epoch(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class _RollupFlags:
    """Per-sid live-signal cache, fed incrementally by `session.fire.*`
    (running_changed/error_changed/queued_prompts_updated/unread_changed/
    monitoring_changed) and fully by `session.status_projected`
    (backend/session_status_projection.py:107-144). Never persisted —
    rebuilt purely from bus facts observed since this adapter started, so
    a sid this process hasn't heard about yet is simply absent (same
    "unknown -> default" honesty `_derive_rollup`'s own docstring
    documents for `state`; `unread_count`/`has_error`/`monitoring_state`
    below share that identical limitation, by design, not a new one).

    `unread_count`/`monitoring_state` are literal legacy-vocabulary
    pass-throughs (not remapped into the rollup's own 7-value model) —
    `has_error` reuses `errored` directly (the SAME boolean already drives
    `state == FAILED`, so there is exactly one source for both, never two
    that could drift)."""

    running: bool = False
    waiting_for_user: bool = False
    errored: bool = False
    queued: bool = False
    unread_count: int = 0
    monitoring_state: str = "stopped"


def _derive_rollup(flags: "_RollupFlags | None") -> SessionRollupState:
    # RECONNECTING / DETACHED have no source reachable from
    # session.fire.*/session.status_projected within this adapter's
    # boundary: turn_manager's execution-phase state machine
    # (lifecycle_command_model.py/lifecycle_command_states.py, the only
    # place either concept exists) publishes only turn_start/
    # turn_complete/turn_stopped on the bus — no phase detail — and
    # exposes no other per-sid queryable accessor `store_access` can
    # reach. Conservatively IDLE when nothing else is known, same
    # priority order as session_status.key_for (error > needs_decision >
    # running > idle), with `queued` slotted between running and idle.
    if flags is None:
        return SessionRollupState.IDLE
    if flags.errored:
        return SessionRollupState.FAILED
    if flags.waiting_for_user:
        return SessionRollupState.AWAITING_APPROVAL
    if flags.running:
        return SessionRollupState.RUNNING
    if flags.queued:
        return SessionRollupState.QUEUED
    return SessionRollupState.IDLE


def _project_ref_for(cwd: str, project_paths: frozenset[str]) -> str | None:
    """A session's project is identified by its own `cwd` (a project's
    identity IS its filesystem path — see `Project.project_ref`); real
    only when a `Project` record for that exact path exists, else honestly
    None rather than treating every ad-hoc working directory as a
    tracked project."""
    return cwd if cwd and cwd in project_paths else None


def _map_summary(
    record: SessionRecord,
    state: SessionRollupState,
    project_ref: str | None,
    organization: SessionOrganizationRecord,
    flags: "_RollupFlags | None",
    pending_interactions: int,
) -> SessionSummary:
    return SessionSummary(
        session_id=record.id,
        title=record.title,
        project_ref=project_ref,
        selectors=SessionSelectors(
            provider_id=record.provider_id,
            runtime_profile_id=record.runtime_profile_id,
            model=record.model,
            reasoning_effort=record.reasoning_effort,
            orchestration_mode=record.orchestration_mode,
            cwd=record.cwd,
        ),
        state=state,
        attention_markers=record.attention_markers,
        opened_at=_parse_epoch(record.opened_at),
        last_activity_at=_parse_epoch(record.updated_at) or 0.0,
        archived=record.archived,
        folder_ref=organization.folder_id,
        tag_refs=tuple(
            TagAssignment(tag_ref=tag_ref, source=source)
            for tag_ref, source in organization.tag_assignments
        ),
        unread_count=flags.unread_count if flags else 0,
        has_error=flags.errored if flags else False,
        monitoring_state=flags.monitoring_state if flags else "stopped",
        attention_marker_details=tuple(
            AttentionMarkerDetail(
                extension_id=m.extension_id, tag=m.tag,
                color=m.color, tooltip=m.tooltip, sound=m.sound,
                sound_setting=m.sound_setting,
            )
            for m in record.attention_marker_details
        ),
        pending_interactions=pending_interactions,
    )



class SessionSurfaceAdapter(SessionSurface):
    def __init__(self) -> None:
        self._projection = BusBoundProjection()
        self._cache_lock = threading.Lock()
        self._last_summary: dict[SessionId, SessionSummary] = {}
        self._rollup_by_sid: dict[SessionId, _RollupFlags] = {}
        # Wired by `backend/adapters/__init__.py`'s `build_adapter` — see
        # `backend/adapters/command_port.py`'s `SessionCommandPort` and
        # `backend/session_commands.py` for the concrete implementation.
        self._command_port: SessionCommandPort | None = None

    def bind(self) -> None:
        """Idempotent: `BusBoundProjection.bind` unsubscribes-then-
        resubscribes under deterministic per-pattern names.

        `interaction.fire.requested`/`interaction.fire.resolved` mirror
        `chat_adapter.py`'s own two explicit subscriptions (not a
        `interaction.fire.*` wildcard, matching that precedent exactly) —
        this adapter only needs to know a session's pending-interaction
        COUNT changed (re-derived fresh via `store_access.
        list_pending_interactions` in `_refresh`, never a second counter),
        unlike `ChatSurfaceAdapter`, which needs the interaction's full
        shape."""
        self._projection.bind(
            [
                ("session.fire.*", self._on_session_fire),
                ("session.status_projected", self._on_status_projected),
                ("lifecycle.turn_*", self._on_lifecycle),
                ("project.fire.*", self._on_project_fire),
                ("session_organization.fire.*", self._on_session_organization_fire),
                ("interaction.fire.requested", self._on_interaction_fact),
                ("interaction.fire.resolved", self._on_interaction_fact),
            ]
        )

    # ---- internals: rollup cache ----------------------------------------

    def _flags_for(self, session_id: SessionId) -> "_RollupFlags | None":
        with self._cache_lock:
            return self._rollup_by_sid.get(session_id)

    def _rollup_for(self, session_id: SessionId) -> SessionRollupState:
        return _derive_rollup(self._flags_for(session_id))

    def _update_flags(self, session_id: SessionId, **changes: bool | int | str) -> None:
        with self._cache_lock:
            current = self._rollup_by_sid.get(session_id, _RollupFlags())
            self._rollup_by_sid[session_id] = replace(current, **changes)

    def _evict(self, session_id: SessionId) -> None:
        with self._cache_lock:
            known = session_id in self._last_summary or session_id in self._rollup_by_sid
            self._last_summary.pop(session_id, None)
            self._rollup_by_sid.pop(session_id, None)
        if known:
            self._projection.bump_render()
            self._projection.broadcast(
                SessionRemoved(cv=CONTRACT_VERSION, session_id=session_id)
            )

    def _project_paths(self) -> frozenset[str]:
        return frozenset(p.path for p in store_access.list_projects())

    def _broadcast_tree_changed(self, root_id: str | None) -> None:
        if not root_id:
            return
        cv = self._projection.bump_render()
        self._projection.broadcast(SessionTreeChanged(cv=cv, session_id=root_id))

    def _broadcast_folder_upsert(
        self, folder_id: str, project_id: str | None, *, intent_id: str | None = None,
    ) -> None:
        """Re-pull the folder record by id and broadcast its `FolderUpsert`
        — shared by the `session_organization.fire.folder_upserted` handler
        (intent_id=None, the common case) and `_run_command`'s post-success
        stamping for `CreateFolder` (intent_id=the creating intent, so the
        frontend can correlate the new folder's server-generated ref — see
        `CommandResult.ref`'s docstring). Never trusts the fact payload's
        own shape (same discipline as `_on_project_fire`)."""
        records = store_access.list_folders(project_id)
        record = next((f for f in records if f.id == folder_id), None)
        if record is None:
            return
        folder = SessionFolder(
            folder_ref=record.id,
            project_ref=record.project_id,
            name=record.name,
            parent_folder_ref=record.parent_folder_id,
            order=record.order,
        )
        cv = self._projection.bump_render()
        self._projection.broadcast(FolderUpsert(cv=cv, folder=folder, intent_id=intent_id))

    def _broadcast_tag_upsert(
        self, tag_id: str, project_id: str | None, *, intent_id: str | None = None,
    ) -> None:
        """Tag counterpart of `_broadcast_folder_upsert` above — same
        rationale, same two call sites (organic fact + `CreateTag`'s
        post-success stamping)."""
        records = store_access.list_tags(project_id)
        record = next((t for t in records if t.id == tag_id), None)
        if record is None:
            return
        tag = SessionTag(
            tag_ref=record.id, project_ref=record.project_id, name=record.name, color=record.color,
        )
        cv = self._projection.bump_render()
        self._projection.broadcast(TagUpsert(cv=cv, tag=tag, intent_id=intent_id))

    def reject_intent(self, intent_id: str, code: str, message: str) -> None:
        """Push an async `IntentRejected` over the live plane (ADR 0006 §5
        "acks are projection facts") — called by `_run_command` when a
        mutation `submit()` already admitted (`IntentAccepted`) fails once
        its fire-and-forget coroutine actually runs. Mirrors
        `ProviderConfigSurfaceAdapter.reject_intent` exactly (see that
        method's docstring for the full rationale) — one ack vocabulary,
        two delivery times, same `IntentRejected` type `submit()` itself
        returns for synchronously-determinable rejections."""
        self._projection.broadcast(IntentRejected(intent_id=intent_id, code=code, message=message))

    # ---- read plane ------------------------------------------------

    def list_sessions(
        self,
        cursor: PageCursor | None,
        query: str | None,
        filters: SessionSearchFilters | None = None,
    ) -> ProjectionResult[SessionPage]:
        snapshot = self._projection.snapshot()
        if cursor is not None:
            if cursor.snapshot.incarnation != snapshot.incarnation:
                return StaleCursor()
            try:
                offset = int(cursor.token)
            except ValueError:
                return StaleCursor()
        else:
            offset = 0

        records = list(store_access.list_session_records())
        records.sort(key=lambda r: _parse_epoch(r.updated_at) or 0.0, reverse=True)
        if query:
            needle = query.lower()
            records = [r for r in records if needle in r.title.lower()]
        # One query path (ADR 0008) — folder_ref/tag_refs filtering applied
        # BEFORE paging, same as the query/needle filter above, never as a
        # parallel filtered-list route.
        if filters is not None and (filters.folder_ref or filters.tag_refs):
            records = [r for r in records if self._matches_filters(r.id, filters)]

        page = records[offset : offset + _PAGE_SIZE]
        next_cursor = None
        if offset + _PAGE_SIZE < len(records):
            next_cursor = PageCursor(
                surface_id=_LIST_SURFACE_ID, snapshot=snapshot, token=str(offset + _PAGE_SIZE),
            )
        project_paths = self._project_paths()
        summaries = tuple(
            _map_summary(
                r, self._rollup_for(r.id), _project_ref_for(r.cwd, project_paths),
                store_access.get_session_organization(r.id), self._flags_for(r.id),
                len(store_access.list_pending_interactions(r.id)),
            )
            for r in page
        )
        return Ok(SessionPage(sessions=summaries, next_cursor=next_cursor), snapshot)

    def _matches_filters(self, session_id: str, filters: SessionSearchFilters) -> bool:
        organization = store_access.get_session_organization(session_id)
        if filters.folder_ref and organization.folder_id != filters.folder_ref:
            return False
        if filters.tag_refs:
            session_tag_refs = {tag_ref for tag_ref, _source in organization.tag_assignments}
            wanted = set(filters.tag_refs)
            if filters.tag_match == "any":
                if not wanted & session_tag_refs:
                    return False
            elif not wanted.issubset(session_tag_refs):
                return False
        return True

    def session_tree(
        self, session_id: SessionId
    ) -> ProjectionResult[tuple[SessionTreeNode, ...]]:
        snapshot = self._projection.snapshot()
        tree = store_access.get_session_tree_records(session_id)
        if tree is None:
            return Rebuilding(retry_after_ms=None)
        nodes = tuple(
            SessionTreeNode(
                surface_id=node.id,
                parent_surface_id=node.parent_id,
                kind=node.kind,
                title=node.title,
                state=self._rollup_for(node.id),
            )
            for node in tree
        )
        return Ok(nodes, snapshot)

    def projects(self) -> ProjectionResult[tuple[Project, ...]]:
        snapshot = self._projection.snapshot()
        records = store_access.list_projects()
        counts = store_access.project_session_counts()
        # `order` reflects `project_store.list_projects()`'s real
        # persisted ordering (most-recently-used first) — genuine list
        # position, not a fabricated ranking.
        projects = tuple(
            Project(project_ref=r.path, name=r.name, order=i, session_count=counts.get(r.path, 0))
            for i, r in enumerate(records)
        )
        return Ok(projects, snapshot)

    def list_folders(
        self, project_ref: ProjectRef | None
    ) -> ProjectionResult[tuple[SessionFolder, ...]]:
        snapshot = self._projection.snapshot()
        records = store_access.list_folders(project_ref)
        folders = tuple(
            SessionFolder(
                folder_ref=r.id,
                project_ref=r.project_id,
                name=r.name,
                parent_folder_ref=r.parent_folder_id,
                order=r.order,
            )
            for r in records
        )
        return Ok(folders, snapshot)

    def list_tags(
        self, project_ref: ProjectRef | None
    ) -> ProjectionResult[tuple[SessionTag, ...]]:
        snapshot = self._projection.snapshot()
        records = store_access.list_tags(project_ref)
        tags = tuple(
            SessionTag(tag_ref=r.id, project_ref=r.project_id, name=r.name, color=r.color)
            for r in records
        )
        return Ok(tags, snapshot)

    # ---- live plane --------------------------------------------------

    def subscribe(self, emit: Emit) -> Subscription:
        self._projection.register(emit)
        return _SubscriptionImpl(lambda: self._projection.unregister(emit))

    # ---- command plane -------------------------------------------------

    def submit(self, intent: SessionIntent) -> TransportAck:
        port = self._command_port
        if port is None:
            return IntentRejected(
                intent_id=intent.intent_id,
                code="unsupported_contract_phase",
                message="command routing deferred; the legacy REST/WS path remains authoritative",
            )
        if isinstance(intent, ArchiveSession):
            coro = port.archive_session(intent.session_id, intent.archived)
        elif isinstance(intent, RenameSession):
            coro = port.rename_session(intent.session_id, intent.title)
        elif isinstance(intent, AssignProject):
            coro = port.assign_project(intent.session_id, intent.project_ref)
        elif isinstance(intent, CreateProject):
            coro = port.create_project(intent.name, intent.path)
        elif isinstance(intent, RenameProject):
            coro = port.rename_project(intent.project_ref, intent.name)
        elif isinstance(intent, DeleteProject):
            coro = port.delete_project(intent.project_ref)
        elif isinstance(intent, MarkOpened):
            coro = port.mark_opened(intent.session_id)
        elif isinstance(intent, CreateFolder):
            coro = port.create_folder(intent.project_ref, intent.name, intent.parent_folder_ref)
        elif isinstance(intent, RenameFolder):
            coro = port.rename_folder(intent.folder_ref, intent.name)
        elif isinstance(intent, MoveFolder):
            coro = port.move_folder(intent.folder_ref, intent.parent_folder_ref)
        elif isinstance(intent, DeleteFolder):
            coro = port.delete_folder(intent.folder_ref, intent.mode)
        elif isinstance(intent, CreateTag):
            coro = port.create_tag(intent.name, intent.project_ref, intent.color)
        elif isinstance(intent, RenameTag):
            coro = port.rename_tag(intent.tag_ref, intent.name)
        elif isinstance(intent, RecolorTag):
            coro = port.recolor_tag(intent.tag_ref, intent.color)
        elif isinstance(intent, DeleteTag):
            coro = port.delete_tag(intent.tag_ref)
        elif isinstance(intent, AssignFolder):
            coro = port.assign_folder(intent.session_id, intent.folder_ref)
        elif isinstance(intent, AssignTags):
            coro = port.assign_tags(
                intent.session_id,
                intent.source,
                intent.add_tag_refs,
                intent.remove_tag_refs,
                intent.sync_tag_refs,
            )
        else:
            # Defensive only: SessionIntent is a closed union and every
            # member is handled above.
            return IntentRejected(
                intent_id=intent.intent_id,
                code="unsupported",
                message=f"{type(intent).__name__} intents are not yet supported on this transport",
            )
        asyncio.get_running_loop().create_task(self._run_command(coro, intent))
        return IntentAccepted(intent_id=intent.intent_id)

    async def _run_command(self, coro, intent: SessionIntent) -> None:
        """Await a fire-and-forget `SessionCommandPort` coroutine and turn
        its outcome into a live-plane fact. `submit()` itself must stay
        sync (fixed by the `SessionSurface` ABC) so it never awaits; the
        real outcome surfaces later — a rejection becomes an async
        `IntentRejected` (`reject_intent`, mirroring
        `ProviderConfigSurfaceAdapter`'s own fix for the identical
        "swallowed into a log line only" gap this port used to have), and
        an acceptance that produced a server-generated `ref` (see
        `CommandResult.ref`'s docstring) gets an intent_id-stamped upsert
        forced out for exactly that resource, so a caller doing a create-
        then-navigate/create-then-assign flow (no synchronous RPC result)
        can correlate on the live frame instead."""
        try:
            result = await coro
        except Exception:
            logger.exception(
                "session intent %s (%s) raised", type(intent).__name__, intent.intent_id,
            )
            self.reject_intent(intent.intent_id, "internal_error", "the command raised")
            return
        if not result.accepted:
            logger.info(
                "session intent %s (%s) rejected: %s %s",
                type(intent).__name__, intent.intent_id, result.code, result.message,
            )
            self.reject_intent(intent.intent_id, result.code, result.message)
            return
        if not result.ref:
            return
        if isinstance(intent, AssignProject):
            # `ref` = the NEW session `move_session_to_project` created —
            # force a re-emit even if `_refresh`'s own dedup would
            # otherwise suppress it (e.g. the organic `session.fire.
            # created` handling already broadcast an identical summary
            # first); the caller needs an intent_id-stamped frame
            # unconditionally to resolve its pending navigation.
            await self._refresh(result.ref, intent_id=intent.intent_id)
        elif isinstance(intent, CreateFolder):
            self._broadcast_folder_upsert(result.ref, intent.project_ref, intent_id=intent.intent_id)
        elif isinstance(intent, CreateTag):
            self._broadcast_tag_upsert(result.ref, intent.project_ref, intent_id=intent.intent_id)

    # ---- internals: live fact handlers ----------------------------------

    async def _on_session_fire(self, event: BusEvent) -> None:
        kind = event.payload.get("kind")
        if kind == "deleted":
            # `deleted_sids` carries the whole removed subtree
            # (session_manager.py's delete()), not just `event.sid` —
            # every one of them is evicted here, each with its own
            # SessionRemoved tombstone (`_evict`), so a live subscriber
            # learns of every removal immediately, never by re-listing.
            deleted_sids = event.payload.get("deleted_sids") or [event.sid]
            root_id = event.payload.get("root_id")
            for sid in deleted_sids:
                if isinstance(sid, str) and sid:
                    self._evict(sid)
            # A fork-only delete (root survives) changes the root's tree
            # shape; a whole-root delete needs no SessionTreeChanged since
            # the root itself was just evicted above.
            if isinstance(root_id, str) and root_id and root_id not in deleted_sids:
                self._broadcast_tree_changed(root_id)
            return
        if kind == "running_changed":
            self._update_flags(event.sid, running=bool(event.payload.get("value")))
        elif kind == "error_changed":
            self._update_flags(event.sid, errored=bool(event.payload.get("has_error")))
        elif kind == "queued_prompts_updated":
            self._update_flags(event.sid, queued=bool(event.payload.get("queued_prompts")))
        elif kind == "unread_changed":
            self._update_flags(event.sid, unread_count=int(event.payload.get("unread_count") or 0))
        elif kind == "monitoring_changed":
            self._update_flags(event.sid, monitoring_state=str(event.payload.get("value") or "stopped"))
        elif kind == "msg_ask_result_set":
            # Filtered to the session-bridge delegation picker's own
            # `purpose`, mirroring `chat_adapter.py`'s `_on_ask_result_set`
            # exactly — this adapter only needs to know the pending-
            # interaction COUNT moved, re-derived fresh in `_refresh`.
            ask_result = event.payload.get("ask_result")
            if not isinstance(ask_result, dict) or ask_result.get("purpose") != "delegate_approval":
                return
        await self._refresh(event.sid)
        if kind in _TREE_STRUCTURE_KINDS:
            self._broadcast_tree_changed(event.root_id)

    async def _on_status_projected(self, event: BusEvent) -> None:
        payload = event.payload
        session_id = payload.get("session_id") or event.sid
        if not session_id:
            return
        if payload.get("deleted"):
            # Same tombstone handling as `_on_session_fire`'s "deleted"
            # branch above — cache eviction + SessionRemoved broadcast.
            self._evict(session_id)
            return
        self._update_flags(
            session_id,
            running=bool(payload.get("running")),
            waiting_for_user=bool(payload.get("waiting_for_user")),
            errored=bool(payload.get("errored")),
        )
        await self._refresh(session_id)

    async def _on_lifecycle(self, event: BusEvent) -> None:
        await self._refresh(event.sid)

    async def _on_project_fire(self, event: BusEvent) -> None:
        """`project.fire.*` — the owner-side fact `backend/session_commands.py`
        publishes after a project mutation (no such fact existed anywhere
        in the legacy codebase before this pass; project_store.py itself
        has no bus producer)."""
        path = event.payload.get("path")
        if not isinstance(path, str) or not path:
            return
        if event.payload.get("kind") == "removed":
            cv = self._projection.bump_render()
            self._projection.broadcast(ProjectRemoved(cv=cv, project_ref=path))
            return
        records = store_access.list_projects()
        order = next((i for i, r in enumerate(records) if r.path == path), None)
        if order is None:
            return
        record = records[order]
        counts = store_access.project_session_counts()
        project = Project(
            project_ref=record.path, name=record.name, order=order,
            session_count=counts.get(record.path, 0),
        )
        cv = self._projection.bump_render()
        self._projection.broadcast(ProjectUpsert(cv=cv, project=project))

    async def _on_interaction_fact(self, event: BusEvent) -> None:
        """`interaction.fire.requested`/`interaction.fire.resolved` — the
        tool-approval/worker-creation-approval mutation sites' own facts
        (see `chat_adapter.py`'s matching subscription). This adapter only
        needs the affected session's `pending_interactions` count to move,
        so it just re-`_refresh`es — `store_access.list_pending_interactions`
        is re-queried fresh, never a second incremental counter."""
        session_id = event.root_id
        if isinstance(session_id, str) and session_id:
            await self._refresh(session_id)

    async def _on_session_organization_fire(self, event: BusEvent) -> None:
        """`session_organization.fire.*` — the owner-side fact
        `session_organization_store.publish_organization_fact` publishes
        after a folder/tag/membership mutation, called from BOTH the
        legacy REST routes (`session_listing_api.py`) and the ADR 0008
        `SessionCommandPort` (`session_commands.py`) — one fact source for
        either transport. Folder/tag upserts re-pull the affected catalog
        record (never trust the payload's own shape, same discipline as
        `_on_project_fire`); removals broadcast their tombstone directly
        (nothing left to re-pull) and refresh every session whose summary
        the removal actually changed."""
        kind = event.payload.get("kind")
        if kind == "folder_upserted":
            folder_id = event.payload.get("folder_id")
            project_id = event.payload.get("project_id")
            if isinstance(folder_id, str) and folder_id:
                self._broadcast_folder_upsert(
                    folder_id, project_id if isinstance(project_id, str) else None,
                )
        elif kind == "folder_removed":
            for folder_id in event.payload.get("folder_ids") or []:
                if isinstance(folder_id, str) and folder_id:
                    cv = self._projection.bump_render()
                    self._projection.broadcast(FolderRemoved(cv=cv, folder_ref=folder_id))
            for session_id in event.payload.get("cleared_session_ids") or []:
                if isinstance(session_id, str) and session_id:
                    await self._refresh(session_id)
        elif kind == "tag_upserted":
            tag_id = event.payload.get("tag_id")
            project_id = event.payload.get("project_id")
            if isinstance(tag_id, str) and tag_id:
                self._broadcast_tag_upsert(
                    tag_id, project_id if isinstance(project_id, str) else None,
                )
        elif kind == "tag_removed":
            tag_id = event.payload.get("tag_id")
            if isinstance(tag_id, str) and tag_id:
                cv = self._projection.bump_render()
                self._projection.broadcast(TagRemoved(cv=cv, tag_ref=tag_id))
            for session_id in event.payload.get("affected_session_ids") or []:
                if isinstance(session_id, str) and session_id:
                    await self._refresh(session_id)
        elif kind == "membership_changed":
            session_id = event.payload.get("session_id")
            if isinstance(session_id, str) and session_id:
                await self._refresh(session_id)

    async def _refresh(self, session_id: str, *, intent_id: str | None = None) -> None:
        """Re-map and, on change, broadcast one session's summary.

        `intent_id` bypasses the change-only dedup below when supplied:
        `_run_command`'s post-success stamping (see its docstring) needs a
        caller correlating on `intent_id` to unconditionally receive a
        frame, even on the rare timing where an organic fact already
        broadcast an identical summary moments earlier — a real but
        unchanged-looking event still deserves its own delivery when it is
        the ONE frame a pending intent is waiting to observe."""
        if not session_id:
            return
        record = store_access.get_session_record(session_id)
        if record is None:
            # Defensive backstop only — real deletions are evicted
            # explicitly above (`_on_session_fire`'s "deleted" branch and
            # `_on_status_projected`'s `deleted` flag); this covers a
            # record vanishing without either fact reaching this adapter.
            self._evict(session_id)
            return
        project_ref = _project_ref_for(record.cwd, self._project_paths())
        organization = store_access.get_session_organization(session_id)
        pending_interactions = len(store_access.list_pending_interactions(session_id))
        summary = _map_summary(
            record, self._rollup_for(session_id), project_ref, organization,
            self._flags_for(session_id), pending_interactions,
        )
        with self._cache_lock:
            unchanged = self._last_summary.get(session_id) == summary
            self._last_summary[session_id] = summary
        if unchanged and intent_id is None:
            return
        cv = self._projection.bump_render()
        self._projection.broadcast(SessionSummaryUpsert(cv=cv, summary=summary, intent_id=intent_id))
