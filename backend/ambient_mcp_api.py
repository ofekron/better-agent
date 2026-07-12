from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import ambient_mcp_sources
import ambient_mcp_policy_store
import ambient_user_mcp_store


router = APIRouter(prefix="/api/ambient-mcps", tags=["ambient-mcps"])
_reconcile: Callable[[], Any] | None = None


class LauncherRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str = Field(min_length=1, max_length=4096)
    args: list[str] = Field(default_factory=list, max_length=256)
    env: dict[str, str] = Field(default_factory=dict, max_length=256)


class UserMcpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    launcher: LauncherRequest
    policy: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class AmbientMcpPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    share_all_eligible: bool
    excluded_ids: list[str] = Field(default_factory=list, max_length=4096)


def set_reconciler(reconcile: Callable[[], Any]) -> None:
    global _reconcile
    _reconcile = reconcile


def _required_reconciler() -> Callable[[], Any]:
    if _reconcile is not None:
        return _reconcile
    import extension_store
    return extension_store.reconcile_native_mcp_servers


def _mutation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=503, detail=f"ambient MCP reconciliation failed: {exc}")


@router.get("")
def list_ambient_mcps() -> dict[str, Any]:
    return {
        "capabilities": [item.to_dict() for item in ambient_mcp_sources.capabilities()],
        "policy": ambient_mcp_policy_store.public(),
    }


@router.patch("/policy")
def patch_ambient_mcp_policy(request: AmbientMcpPolicyRequest) -> dict[str, Any]:
    capabilities = {item.id: item for item in ambient_mcp_sources.capabilities()}
    try:
        excluded = {
            ambient_mcp_policy_store.validate_capability_id(item_id)
            for item_id in request.excluded_ids
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    before = ambient_mcp_policy_store.get()
    requested = {
        **before,
        "share_all_eligible": request.share_all_eligible,
        "excluded_ids": sorted(excluded),
    }
    removed = _exposed_ids(before, capabilities) - _exposed_ids(requested, capabilities)
    try:
        _revoke_removed(removed, capabilities)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"ambient MCP revocation failed: {exc}") from exc

    def mutation(policy: dict[str, Any]) -> None:
        policy["share_all_eligible"] = request.share_all_eligible
        policy["excluded_ids"] = sorted(excluded)

    try:
        ambient_mcp_policy_store.mutate_and_reconcile(mutation, _required_reconciler())
    except Exception as exc:
        raise _mutation_error(exc) from exc
    after = ambient_mcp_policy_store.get()
    return {"policy": ambient_mcp_policy_store.public(after)}


def _exposed_ids(
    policy: dict[str, Any], capabilities: dict[str, ambient_mcp_sources.AmbientMcpCapability]
) -> set[str]:
    if not policy["share_all_eligible"]:
        return set()
    excluded = set(policy["excluded_ids"])
    return {
        item_id
        for item_id, item in capabilities.items()
        if item.available and item_id not in excluded
    }


def _revoke_removed(
    removed: set[str], capabilities: dict[str, ambient_mcp_sources.AmbientMcpCapability]
) -> None:
    if not removed:
        return
    import ambient_mcp_broker
    for item_id in removed:
        item = capabilities[item_id]
        if item.ownership == "extension":
            launcher_env = dict((item.launcher or {}).get("env") or {})
            extension_id = str(launcher_env.get("BETTER_CLAUDE_EXTENSION_ID") or "")
            if extension_id:
                server_name = str(
                    launcher_env.get("BETTER_CLAUDE_EXTENSION_MCP_SERVER") or item.name
                )
                ambient_mcp_broker.broker.revoke_extension(
                    extension_id, server_name=server_name
                )
        elif item.ownership == "better-agent-core":
            ambient_mcp_broker.broker.revoke_extension(
                "better-agent-core", server_name=item.name
            )


@router.put("/user/{record_id}")
def put_user_mcp(record_id: str, request: UserMcpRequest) -> dict[str, Any]:
    if record_id != request.id:
        raise HTTPException(status_code=422, detail="path and body MCP ids must match")
    record = request.model_dump()

    def mutation(records: dict[str, dict[str, Any]]) -> dict[str, Any]:
        clean = ambient_user_mcp_store.validate_record(record)
        records[clean["id"]] = clean
        return clean

    try:
        saved = ambient_user_mcp_store.mutate_and_reconcile(mutation, _required_reconciler())
    except HTTPException:
        raise
    except Exception as exc:
        raise _mutation_error(exc) from exc
    return {"record": saved}


@router.delete("/user/{record_id}")
def delete_user_mcp(record_id: str) -> dict[str, Any]:
    def mutation(records: dict[str, dict[str, Any]]) -> bool:
        if record_id not in records:
            raise KeyError(record_id)
        del records[record_id]
        return True

    try:
        ambient_user_mcp_store.mutate_and_reconcile(mutation, _required_reconciler())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="ambient user MCP not found") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise _mutation_error(exc) from exc
    return {"deleted": True}
