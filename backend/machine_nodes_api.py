"""Internal HTTP surface for the multi-machine node topology.

Registration approval, revocation and restart of worker nodes. Every
route is gated on internal authority plus the machine-nodes extension's
runtime readiness.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Header, HTTPException

import perf
from i18n import t
from internal_guards import require_role_internal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["machine-nodes"])

_ROLE = "machine-nodes"


@router.post("/api/internal/machine-nodes/list")
async def internal_get_nodes(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    """Snapshot of the multi-machine topology + live connection state.
    Returns an empty list (no topology configured) for single-machine
    deployments rather than raising. Frontend uses this to render
    node-status badges and the per-worker node selector."""
    try:
        import node_provider_credential_sync
        import node_store
        snapshot = await asyncio.to_thread(node_store.snapshot)
        return await asyncio.to_thread(
            node_provider_credential_sync.project_node_snapshots,
            snapshot,
        )
    except Exception:
        logger.exception("get_nodes failed")
        return []

def _local_node_id_or_primary() -> str:
    """Resolve the local node's id without raising. Single-machine
    deploys (no topology.yaml) get the legacy `"primary"` sentinel."""
    try:
        from topology import local_node_id as _lid
        return _lid()
    except Exception:
        return "primary"

@router.post("/api/internal/machine-nodes/local-node-id")
async def internal_get_local_node_id(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    """Tells the frontend which node snapshot entry corresponds
    to "this backend's host" — used to render the "(host)"
    badge in pickers and to compute `is_local` for default-pick rules."""
    return {"node_id": _local_node_id_or_primary()}

@router.post("/api/internal/machine-nodes/pending")
async def internal_list_pending_nodes(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    """Worker-nodes currently awaiting registration approval. Used by the
    frontend on mount / WS reconnect to (re)render the approval popup.

    Secrets never leave the server — only the display fingerprint does."""
    import node_link
    with perf.timed("internal.machine_nodes.pending"):
        pending = node_link.public_pending_nodes_cached()
        if pending is None:
            pending = await asyncio.to_thread(node_link.public_pending_nodes)
        return {
            "pending_nodes": pending,
        }

@router.post("/api/internal/machine-nodes/approve")
async def internal_approve_pending_node(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    """Approve a node registration: persist it to the registry (so future
    reconnects auto-authenticate with its secret) and, if the node is
    holding its WS open right now, let it connect immediately."""
    node_id = (body or {}).get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise HTTPException(status_code=400, detail="node_id is required")
    import node_link
    rec, reason = await node_link.approve_registration(node_id)
    if reason == "missing":
        raise HTTPException(status_code=404, detail=t("error.node_request_not_found"))
    if reason == "expired":
        raise HTTPException(status_code=410, detail=t("error.node_request_expired"))
    if reason == "already_resolved":
        return {"status": rec.get("status"), "record": node_link._public_rec(rec), "idempotent": True}
    return {"status": "approved", "record": node_link._public_rec(rec)}

@router.post("/api/internal/machine-nodes/deny")
async def internal_deny_pending_node(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    node_id = (body or {}).get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise HTTPException(status_code=400, detail="node_id is required")
    import node_link
    rec, reason = await node_link.deny_registration(node_id)
    if reason == "missing":
        raise HTTPException(status_code=404, detail=t("error.node_request_not_found"))
    if reason == "expired":
        raise HTTPException(status_code=410, detail=t("error.node_request_expired"))
    if reason == "already_resolved":
        return {"status": rec.get("status"), "record": node_link._public_rec(rec), "idempotent": True}
    return {"status": "denied", "record": node_link._public_rec(rec)}

@router.post("/api/internal/machine-nodes/revoke")
async def internal_revoke_node(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    node_id = (body or {}).get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise HTTPException(status_code=400, detail="node_id is required")
    """Revoke a node: drops it from the dynamic registry or static
    topology.yaml. Cleans up node_store state and fires WS broadcast."""
    import node_registry_store
    import node_provider_credential_sync
    import node_store
    import topology

    if node_registry_store.remove(node_id):
        await asyncio.to_thread(node_provider_credential_sync.remove_node, node_id)
        await node_store.forget(node_id)
        return {"status": "revoked", "node_id": node_id}

    try:
        removed = topology.remove_node(node_id)
    except topology.TopologyError:
        raise HTTPException(
            status_code=500,
            detail="topology.yaml is malformed — cannot delete node",
        )
    if not removed:
        raise HTTPException(status_code=404, detail=t("error.node_request_not_found"))

    await asyncio.to_thread(node_provider_credential_sync.remove_node, node_id)
    await node_store.forget(node_id)
    return {"status": "revoked", "node_id": node_id}

@router.post("/api/internal/machine-nodes/restart")
async def internal_restart_node(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    node_id = (body or {}).get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise HTTPException(status_code=400, detail="node_id is required")
    """Tell a connected worker-node to restart its process."""
    import node_link

    try:
        await node_link.send_restart(node_id)
    except node_link.NodeOffline:
        raise HTTPException(
            status_code=409,
            detail="Node is not connected",
        )
    return {"status": "restart_sent", "node_id": node_id}
