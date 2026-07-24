from __future__ import annotations

from typing import Any

from better_agent_sdk import Client
from better_agent_sdk.surfaces import OperationSpec, build_mcp_server

_TIMEOUT = 24 * 60 * 60.0


def _propose(*, action: str, name: str, description: str, type: str, content: str,
             suggested_scope_type: str, suggested_scope_path: str, target_slug: str = "") -> dict[str, Any]:
    proposal: dict[str, Any] = {
        "action": action,
        "name": name,
        "description": description,
        "type": type,
        "content": content,
        "scope_type": suggested_scope_type,
        "scope_path": suggested_scope_path,
    }
    if target_slug:
        proposal["target_slug"] = target_slug
    client = Client()
    result = client.request_memory_proposal(proposal, timeout_seconds=_TIMEOUT)
    if not result.get("success") or not result.get("approved"):
        return {
            "success": bool(result.get("success")),
            "approved": bool(result.get("approved")),
            "error": result.get("error"),
        }
    written = client.write_memory(result["memory_proposal"])
    if not written.get("success"):
        return {"success": False, "approved": True, "error": written.get("error")}
    return {"success": True, "approved": True, "memory": written.get("memory")}


def propose_memory_add(
    name: str,
    description: str,
    type: str,
    content: str,
    suggested_scope_type: str,
    suggested_scope_path: str = "",
) -> dict[str, Any]:
    """Propose adding a new memory. Blocks until the user approves (optionally
    editing any field, including the scope) or rejects it in the chat UI --
    the memory is only written to disk on approval.

    `name` is a lowercase-kebab-case slug (e.g. "shared-git-index-races").
    `type` is one of: user, feedback, project, reference.
    `suggested_scope_type` is one of: global, project, folder -- your best
    guess at how broadly this memory applies; the user can change it.
    `suggested_scope_path` is the absolute project/folder path; required
    unless suggested_scope_type is "global".
    """
    return _propose(
        action="add",
        name=name,
        description=description,
        type=type,
        content=content,
        suggested_scope_type=suggested_scope_type,
        suggested_scope_path=suggested_scope_path,
    )


def propose_memory_edit(
    target_slug: str,
    scope_type: str,
    scope_path: str,
    description: str,
    type: str,
    content: str,
) -> dict[str, Any]:
    """Propose editing an existing memory identified by its slug and current
    scope. Blocks until the user approves (optionally editing further) or
    rejects it in the chat UI."""
    return _propose(
        action="edit",
        name=target_slug,
        description=description,
        type=type,
        content=content,
        suggested_scope_type=scope_type,
        suggested_scope_path=scope_path,
        target_slug=target_slug,
    )


def get_memories(cwd: str) -> dict[str, Any]:
    """Read every memory visible from `cwd`: global memories plus any
    project/folder-scoped memories whose scope is an ancestor of (or equal
    to) `cwd`. Read-only -- no approval is required."""
    try:
        return Client().list_memories(cwd)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _specs() -> tuple[OperationSpec, ...]:
    return (
        OperationSpec("propose_memory_add", propose_memory_add),
        OperationSpec("propose_memory_edit", propose_memory_edit),
        OperationSpec("get_memories", get_memories),
    )


def build_server():
    # local=True: these handlers only proxy to /api/internal/memory/* over
    # Client() (already gated by X-Internal-Token + internal_loopback), so
    # there's no need for the RuntimeTransport/operation_catalog broker other
    # runtime_mcp extensions (e.g. marketplace) use to reach core capability
    # handlers -- there's no separate core capability here to broker to.
    return build_mcp_server("ofek-dev-memory", _specs(), local=True)


def main() -> int:
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
