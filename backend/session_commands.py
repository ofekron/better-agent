"""Concrete `SessionCommandPort` implementation (ADR 0008 command plane).

Sits OUTSIDE the `backend/adapters/` import boundary — same port/adapter
split as `backend/surface_commands.py`'s `ChatCommandPort` implementation
(see that module's docstring and `backend/adapters/command_port.py`) — so
it is free to import `session_detail_api`/`projects_api`, the SAME
mutation functions the legacy REST routes call. This is the single
mutation path: every method here is a thin wrapper that calls the
existing function and shapes its outcome into a `CommandResult`, never a
second, parallel implementation of the mutation itself.

`project_store.py` has no bus producer of its own (no legacy code ever
needed one) — `_publish_project_fact` below is the one new owner-side fact
this pass adds, so `SessionSurfaceAdapter._on_project_fire`
(`backend/adapters/session_adapter.py`) can project a live `ProjectUpsert`/
`ProjectRemoved` instead of requiring a subscriber to re-list.

Folder/tag/session-organization methods below call `session_organization_store`
directly — the SAME module the legacy `/api/session-folders`, `/api/session-
tags`, `/api/sessions/{id}/organization`, and `/api/internal/session-
organization/*` routes in `backend/session_listing_api.py` call — and
publish the fact via that module's own `publish_organization_fact` (its
one publish site, shared by both transports; see that function's
docstring). `delete_folder`'s `delete_sessions` mode needs the same
cascade session-tree deletion the legacy route's `_delete_session_tree`
capability provides — wired in via `build_session_command_port`'s
constructor argument, composed from `main.py`'s own `_delete_session_tree`
(the same callable `session_listing_api.configure()` receives), never a
second implementation of that cascade.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

import projects_api
import session_detail_api
import session_organization_store
from backend.adapters.command_port import CommandResult, SessionCommandPort
from event_bus import BusEvent, bus

logger = logging.getLogger(__name__)


async def _publish_project_fact(path: str, *, kind: str) -> None:
    try:
        await bus.publish(BusEvent(
            type=f"project.fire.{kind}",
            root_id=path,
            sid=path,
            payload={"path": path, "kind": kind},
            persist=False,
        ))
    except Exception:
        logger.exception("project.fire.%s publish failed for %r", kind, path)


def _rejection_from_status(status: int, body: Any) -> CommandResult:
    message = body.get("error") if isinstance(body, dict) else str(body)
    return CommandResult(accepted=False, code=str(status), message=str(message or ""))


async def _call(
    fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any,
) -> tuple[CommandResult, Any]:
    """Invoke one existing `session_detail_api`/`projects_api` async
    mutation function and normalize its outcome. These functions are
    FastAPI route handlers: some raise `HTTPException` on failure, one
    (`rename_session`) instead returns a `(body, status_code)` tuple — both
    shapes are translated into the SAME `CommandResult`, so callers below
    never branch on which convention a given function uses. Returns the
    function's raw return value too (mirroring `_call_store` below) since a
    few callers (e.g. `assign_project`) need the created/returned resource's
    own id for `CommandResult.ref`."""
    try:
        result = await fn(*args, **kwargs)
    except HTTPException as exc:
        return CommandResult(accepted=False, code=str(exc.status_code), message=str(exc.detail)), None
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
        body, status = result
        if status >= 400:
            return _rejection_from_status(status, body), None
        return CommandResult(accepted=True), body
    return CommandResult(accepted=True), result


async def _call_store(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[CommandResult, Any]:
    """Invoke one `session_organization_store` mutation function — sync,
    thread-pooled via `asyncio.to_thread`, same as the legacy REST routes
    in `session_listing_api.py` — and normalize its outcome into a
    `CommandResult`, mirroring `_call` above for the async
    `session_detail_api`/`projects_api` functions. Returns the function's
    raw return value too (a folder/tag dict, or a bool for delete_folder/
    delete_tag) since the fact payloads below need it."""
    try:
        result = await asyncio.to_thread(fn, *args, **kwargs)
    except KeyError:
        return CommandResult(accepted=False, code="404", message="not found"), None
    except ValueError as exc:
        return CommandResult(accepted=False, code="invalid", message=str(exc)), None
    return CommandResult(accepted=True), result


class _SessionCommandPortImpl:
    def __init__(
        self, delete_session_tree: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._delete_session_tree = delete_session_tree

    async def archive_session(self, session_id: str, archived: bool) -> CommandResult:
        result, _ = await _call(session_detail_api.archive_session, session_id, {"archived": archived})
        return result

    async def rename_session(self, session_id: str, title: str) -> CommandResult:
        result, _ = await _call(session_detail_api.rename_session, session_id, {"name": title})
        return result

    async def assign_project(self, session_id: str, project_ref: str | None) -> CommandResult:
        if not project_ref:
            # No legacy mechanism unassigns a session's project — every
            # session always has a cwd, and `move_session_to_project` (the
            # only project-reassignment path that exists) requires a
            # target directory.
            return CommandResult(
                accepted=False,
                code="unsupported",
                message="unassigning a session's project has no legacy mechanism to map onto",
            )
        result, body = await _call(
            session_detail_api.move_session_to_project, session_id, {"cwd": project_ref},
        )
        if result.accepted and isinstance(body, dict) and body.get("id"):
            # `ref` = the NEW session's id (continuation-style move creates
            # one) — the frontend correlates on the `SessionSummaryUpsert`
            # this ref makes `SessionSurfaceAdapter._run_command` emit with
            # a matching `intent_id`, never a synchronous RPC body.
            return replace(result, ref=str(body["id"]))
        return result

    async def create_project(self, name: str, path: str) -> CommandResult:
        if not path.strip():
            return CommandResult(
                accepted=False, code="invalid_path", message="path is required",
            )
        result, _ = await _call(projects_api.create_project, {"path": path, "name": name or None})
        if result.accepted:
            await _publish_project_fact(path, kind="upserted")
        return result

    async def rename_project(self, project_ref: str, name: str) -> CommandResult:
        if not project_ref:
            return CommandResult(
                accepted=False, code="invalid_project_ref", message="project_ref is required",
            )
        # `project_store.add_project` upserts by path — calling the SAME
        # function `create_project` uses, on an already-existing path,
        # updates its `name` in place. One mutation function, two intents.
        result, _ = await _call(
            projects_api.create_project, {"path": project_ref, "name": name},
        )
        if result.accepted:
            await _publish_project_fact(project_ref, kind="upserted")
        return result

    async def delete_project(self, project_ref: str) -> CommandResult:
        if not project_ref:
            return CommandResult(
                accepted=False, code="invalid_project_ref", message="project_ref is required",
            )
        try:
            result = await projects_api.delete_project(path=project_ref, node_id="primary")
        except HTTPException as exc:
            return CommandResult(accepted=False, code=str(exc.status_code), message=str(exc.detail))
        if not result.get("deleted"):
            return CommandResult(accepted=False, code="not_found", message="project not found")
        await _publish_project_fact(project_ref, kind="removed")
        return CommandResult(accepted=True)

    async def mark_opened(self, session_id: str) -> CommandResult:
        result, _ = await _call(session_detail_api.mark_session_opened, session_id)
        return result

    # ---- ADR 0008 folders/tags/session-organization ---------------------

    async def create_folder(
        self, project_ref: str, name: str, parent_folder_ref: str | None,
    ) -> CommandResult:
        if not project_ref:
            return CommandResult(
                accepted=False, code="invalid_project_ref", message="project_ref is required",
            )
        ack, folder = await _call_store(
            session_organization_store.create_folder,
            project_id=project_ref, name=name, parent_folder_id=parent_folder_ref,
        )
        if ack.accepted:
            await session_organization_store.publish_organization_fact(
                "folder_upserted", {"folder_id": folder["id"], "project_id": folder["project_id"]},
            )
            # `ref` = the new folder's server-generated id — see
            # `assign_project`'s matching comment for the correlation
            # mechanism (event-driven, via `FolderUpsert.intent_id`).
            return replace(ack, ref=str(folder["id"]))
        return ack

    async def rename_folder(self, folder_ref: str, name: str) -> CommandResult:
        ack, folder = await _call_store(
            session_organization_store.update_folder, folder_ref, {"name": name},
        )
        if ack.accepted:
            await session_organization_store.publish_organization_fact(
                "folder_upserted", {"folder_id": folder["id"], "project_id": folder["project_id"]},
            )
        return ack

    async def move_folder(self, folder_ref: str, parent_folder_ref: str | None) -> CommandResult:
        ack, folder = await _call_store(
            session_organization_store.update_folder,
            folder_ref, {"parent_folder_id": parent_folder_ref},
        )
        if ack.accepted:
            await session_organization_store.publish_organization_fact(
                "folder_upserted", {"folder_id": folder["id"], "project_id": folder["project_id"]},
            )
        return ack

    async def delete_folder(self, folder_ref: str, mode: str) -> CommandResult:
        if mode not in ("unassign", "delete_sessions"):
            return CommandResult(
                accepted=False, code="invalid_mode",
                message="mode must be unassign or delete_sessions",
            )
        try:
            preview = await asyncio.to_thread(
                session_organization_store.folder_delete_preview, folder_ref,
            )
        except KeyError:
            return CommandResult(accepted=False, code="404", message="folder not found")
        if mode == "delete_sessions":
            if self._delete_session_tree is None:
                return CommandResult(
                    accepted=False, code="unsupported",
                    message="session-tree cascade deletion is not wired on this command port",
                )
            for session_id in preview["session_ids"]:
                await self._delete_session_tree(session_id)
        ack, deleted = await _call_store(
            session_organization_store.delete_folder, folder_ref, mode=mode,
        )
        if ack.accepted and not deleted:
            return CommandResult(accepted=False, code="404", message="folder not found")
        if ack.accepted:
            await session_organization_store.publish_organization_fact(
                "folder_removed",
                {
                    "folder_ids": preview["folder_ids"],
                    # delete_sessions evicts the contained sessions entirely
                    # (via the tombstone the existing session.fire.deleted
                    # handling already broadcasts) — only unassign leaves
                    # live sessions whose folder_ref needs clearing here.
                    "cleared_session_ids": preview["session_ids"] if mode == "unassign" else [],
                },
            )
        return ack

    async def create_tag(
        self, name: str, project_ref: str | None, color: str | None,
    ) -> CommandResult:
        ack, tag = await _call_store(
            session_organization_store.create_tag,
            name=name, project_id=project_ref, color=color,
        )
        if ack.accepted:
            await session_organization_store.publish_organization_fact(
                "tag_upserted", {"tag_id": tag["id"], "project_id": tag["project_id"]},
            )
            # `ref` = the new tag's server-generated id — see
            # `assign_project`'s matching comment.
            return replace(ack, ref=str(tag["id"]))
        return ack

    async def rename_tag(self, tag_ref: str, name: str) -> CommandResult:
        ack, tag = await _call_store(session_organization_store.update_tag, tag_ref, {"name": name})
        if ack.accepted:
            await session_organization_store.publish_organization_fact(
                "tag_upserted", {"tag_id": tag["id"], "project_id": tag["project_id"]},
            )
        return ack

    async def recolor_tag(self, tag_ref: str, color: str) -> CommandResult:
        ack, tag = await _call_store(session_organization_store.update_tag, tag_ref, {"color": color})
        if ack.accepted:
            await session_organization_store.publish_organization_fact(
                "tag_upserted", {"tag_id": tag["id"], "project_id": tag["project_id"]},
            )
        return ack

    async def delete_tag(self, tag_ref: str) -> CommandResult:
        # Computed BEFORE deletion — `delete_tag` itself strips the id from
        # every assignment, so this is the only chance to know who to
        # refresh.
        affected = await asyncio.to_thread(
            session_organization_store.session_ids_with_tag, tag_ref,
        )
        ack, deleted = await _call_store(session_organization_store.delete_tag, tag_ref)
        if ack.accepted and not deleted:
            return CommandResult(accepted=False, code="404", message="tag not found")
        if ack.accepted:
            await session_organization_store.publish_organization_fact(
                "tag_removed", {"tag_id": tag_ref, "affected_session_ids": affected},
            )
        return ack

    async def assign_folder(self, session_id: str, folder_ref: str | None) -> CommandResult:
        ack, _ = await _call_store(
            session_organization_store.set_session_folder, session_id, folder_ref,
        )
        if ack.accepted:
            await session_organization_store.publish_organization_fact(
                "membership_changed", {"session_id": session_id},
            )
        return ack

    async def assign_tags(
        self,
        session_id: str,
        source: str,
        add_tag_refs: tuple[str, ...] | None,
        remove_tag_refs: tuple[str, ...] | None,
        sync_tag_refs: tuple[str, ...] | None,
    ) -> CommandResult:
        has_delta = add_tag_refs is not None or remove_tag_refs is not None
        has_sync = sync_tag_refs is not None
        if has_delta and has_sync:
            return CommandResult(
                accepted=False, code="invalid_mode",
                message="add/remove_tag_refs and sync_tag_refs are mutually exclusive",
            )
        if not has_delta and not has_sync:
            return CommandResult(
                accepted=False, code="invalid_mode",
                message="assign_tags requires add/remove_tag_refs or sync_tag_refs",
            )
        if has_sync:
            ack, _ = await _call_store(
                session_organization_store.sync_session_tags_by_source,
                session_id, tag_ids=list(sync_tag_refs), source=source,
            )
        else:
            ack, _ = await _call_store(
                session_organization_store.patch_session_tags_scoped,
                session_id, source=source,
                add=list(add_tag_refs or ()), remove=list(remove_tag_refs or ()),
            )
        if ack.accepted:
            await session_organization_store.publish_organization_fact(
                "membership_changed", {"session_id": session_id},
            )
        return ack


def build_session_command_port(
    delete_session_tree: Callable[[str], Awaitable[bool]] | None = None,
) -> SessionCommandPort:
    """Factory mirroring `surface_commands.build_chat_command_port`: the
    returned port holds no state beyond the module-level singletons
    (`session_detail_api`/`projects_api`/`session_organization_store`) it
    calls into plus the injected `delete_session_tree` capability, so
    building more than one instance is safe."""
    return _SessionCommandPortImpl(delete_session_tree=delete_session_tree)
