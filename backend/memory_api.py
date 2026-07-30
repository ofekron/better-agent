"""Memory proposal validation and the whole memory HTTP surface — the
internal agent-facing routes and the user-facing browse/edit routes.

This module owns the memory-proposal shape. Agent proposals, the
user-input approval flow and the public PUT route all validate through
`validate_memory_proposal` rather than keeping their own copy — one
validator, so a rule tightened for one caller tightens for all of them.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Header, HTTPException

import memory_store
from internal_guards import require_internal

router = APIRouter(tags=["memory"])

MEMORY_TYPES = ("user", "feedback", "project", "reference")
MEMORY_SCOPE_TYPES = ("global", "project", "folder")
MEMORY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def validate_memory_proposal(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="memory_proposal must be an object")
    action = str(raw.get("action") or "add").strip()
    if action not in ("add", "edit"):
        raise HTTPException(status_code=400, detail="memory_proposal.action must be add or edit")
    name = str(raw.get("name") or "").strip().lower()
    if not MEMORY_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="memory_proposal.name must be lowercase kebab-case, 1-80 chars",
        )
    description = str(raw.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="memory_proposal.description is required")
    if "\n" in description or "\r" in description:
        # Frontmatter is one field per line; an embedded newline (e.g.
        # "ok\n---\nHACKED: yes") terminates the YAML block early and lets
        # the rest of the description masquerade as frontmatter/content.
        raise HTTPException(status_code=400, detail="memory_proposal.description must be a single line")
    mem_type = str(raw.get("type") or "").strip()
    if mem_type not in MEMORY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"memory_proposal.type must be one of {', '.join(MEMORY_TYPES)}",
        )
    content = str(raw.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="memory_proposal.content is required")
    scope_type = str(raw.get("scope_type") or "").strip()
    if scope_type not in MEMORY_SCOPE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"memory_proposal.scope_type must be one of {', '.join(MEMORY_SCOPE_TYPES)}",
        )
    scope_path = str(raw.get("scope_path") or "").strip()
    if "\n" in scope_path or "\r" in scope_path:
        raise HTTPException(status_code=400, detail="memory_proposal.scope_path must be a single line")
    if scope_type in ("project", "folder") and not scope_path:
        raise HTTPException(
            status_code=400,
            detail="memory_proposal.scope_path is required for project/folder scope",
        )
    if scope_type == "global":
        scope_path = ""
    proposal = {
        "action": action,
        "name": name[:80],
        "description": description[:300],
        "type": mem_type,
        "content": content[:8000],
        "scope_type": scope_type,
        "scope_path": scope_path[:1000],
    }
    if action == "edit":
        target_slug = str(raw.get("target_slug") or "").strip().lower()
        if not MEMORY_NAME_RE.match(target_slug):
            raise HTTPException(
                status_code=400,
                detail="memory_proposal.target_slug must be lowercase kebab-case, 1-80 chars",
            )
        proposal["target_slug"] = target_slug[:80]
    return proposal


@router.get("/api/memory/all")
async def get_all_memory_scopes():
    """Every known scope (for the settings memory browser -- unlike
    `/api/memory/scopes`, not filtered to ancestors of a given cwd)."""
    global_memories = await asyncio.to_thread(
        memory_store.list_memories, scope_type="global", scope_path="",
    )
    known_scopes = await asyncio.to_thread(memory_store.list_scopes)
    scoped = []
    for scope in known_scopes:
        memories = await asyncio.to_thread(
            memory_store.list_memories,
            scope_type=scope["type"],
            scope_path=scope["path"],
        )
        scoped.append({"type": scope["type"], "path": scope["path"], "memories": memories})
    return {"global": global_memories, "scopes": scoped}


@router.get("/api/memory/scopes")
async def get_memory_scopes(cwd: str | None = None):
    """User-facing browse surface: every memory visible from `cwd` (global
    plus ancestor project/folder scopes), for the settings memory editor.
    Direct user edits/deletes below never require agent approval -- only
    agent-initiated additions/edits go through the user-input approval gate."""
    resolved_cwd = str(cwd or "").strip()
    if not resolved_cwd:
        return {"scopes": {"global": await asyncio.to_thread(
            memory_store.list_memories, scope_type="global", scope_path="",
        )}}
    scopes = await asyncio.to_thread(memory_store.memories_for_cwd, resolved_cwd)
    return {"scopes": scopes}


@router.put("/api/memory/{scope_type}/{slug}")
async def put_memory(scope_type: str, slug: str, body: dict):
    # Reuses the exact same validator (and its length caps) as agent-proposed
    # writes, so a direct user edit can't write unbounded content that an
    # agent proposal couldn't.
    try:
        proposal = validate_memory_proposal({
            "action": "add",
            "name": slug,
            "description": body.get("description"),
            "type": body.get("type"),
            "content": body.get("content"),
            "scope_type": scope_type,
            "scope_path": body.get("scope_path"),
        })
    except HTTPException as exc:
        raise HTTPException(status_code=400, detail=str(exc.detail))
    try:
        memory = await asyncio.to_thread(
            memory_store.write_memory,
            scope_type=proposal["scope_type"],
            scope_path=proposal["scope_path"],
            slug=proposal["name"],
            description=proposal["description"],
            mem_type=proposal["type"],
            content=proposal["content"],
        )
    except memory_store.MemoryStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, "memory": memory}


@router.delete("/api/memory/{scope_type}/{slug}")
async def delete_memory_route(scope_type: str, slug: str, scope_path: str = ""):
    try:
        deleted = await asyncio.to_thread(
            memory_store.delete_memory,
            scope_type=scope_type,
            scope_path=scope_path,
            slug=slug,
        )
    except memory_store.MemoryStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="memory not found")
    return {"success": True}


@router.post("/api/internal/memory/write")
async def internal_memory_write(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Persist an already-approved memory proposal. Called by the memory
    extension's MCP server after the user approves (and possibly edits) a
    `propose_memory_add`/`propose_memory_edit` pending request -- never
    called directly by an agent without going through that approval gate."""
    require_internal()
    try:
        proposal = validate_memory_proposal(body.get("memory_proposal"))
    except HTTPException as exc:
        return {"success": False, "error": str(exc.detail)}
    try:
        memory = await asyncio.to_thread(
            memory_store.write_memory,
            scope_type=proposal["scope_type"],
            scope_path=proposal["scope_path"],
            slug=proposal.get("target_slug") or proposal["name"],
            description=proposal["description"],
            mem_type=proposal["type"],
            content=proposal["content"],
        )
    except memory_store.MemoryStoreError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "memory": memory}


@router.post("/api/internal/memory/list")
async def internal_memory_list(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_internal()
    cwd = str(body.get("cwd") or "").strip()
    if not cwd:
        return {"success": False, "error": "cwd is required"}
    scopes = await asyncio.to_thread(memory_store.memories_for_cwd, cwd)
    return {"success": True, "scopes": scopes}


@router.post("/api/internal/memory/delete")
async def internal_memory_delete(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_internal()
    scope_type = str(body.get("scope_type") or "").strip()
    scope_path = str(body.get("scope_path") or "").strip()
    slug = str(body.get("slug") or "").strip()
    try:
        deleted = await asyncio.to_thread(
            memory_store.delete_memory,
            scope_type=scope_type,
            scope_path=scope_path,
            slug=slug,
        )
    except memory_store.MemoryStoreError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": deleted}
