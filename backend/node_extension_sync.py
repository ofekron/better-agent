"""Event-driven extension sync to worker nodes.

The extension store is owned by the primary; worker nodes hold a pushed
projection of it (`import_extension_sync_state`). This module is the node
subsystem's subscriber for two facts:

- "extension state changed" (`notify_extensions_changed`, published by
  extension_api after any mutation and by housekeeping after auto-update):
  coalesces bursts into one export and pushes it to every connected worker.
- "node connected" (`on_node_state`, registered as a node_store listener):
  pushes the current state to the worker that just (re)connected so it never
  runs a stale projection after downtime.

Push failures are logged and dropped: the next store change or the node's
next reconnect re-publishes the full state, so a dead node cannot wedge the
loop or trigger retry storms.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_SYNC_RPC_TIMEOUT_S = 180.0

_dirty = False
_push_task: asyncio.Task | None = None


def _export_state() -> dict:
    import extension_store

    return extension_store.export_extension_sync_state()


def _snapshot_nodes() -> list[dict]:
    import node_store

    return node_store.snapshot()


async def _call_rpc(node_id: str, state: dict) -> None:
    from node_rpc_handlers import call_local_or_remote

    await call_local_or_remote(
        node_id,
        "sync_extension_config",
        {"extension_state": state},
        timeout=_SYNC_RPC_TIMEOUT_S,
        version_ready_required=True,
    )


def _connected_worker_ids(nodes: list[dict]) -> list[str]:
    return [
        str(node.get("id") or "")
        for node in nodes
        if node.get("id")
        and node.get("id") != "primary"
        and node.get("role") == "worker_node"
        and node.get("state") == "connected"
    ]


def notify_extensions_changed() -> None:
    """Publish the "extension state changed" fact.

    Safe to call from any coroutine on the main loop; bursts coalesce into
    a single export+push pass (plus one follow-up pass if changes landed
    while a push was in flight).
    """
    global _dirty, _push_task
    _dirty = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("extension sync notify outside event loop; skipped")
        return
    if _push_task is None or _push_task.done():
        _push_task = loop.create_task(_push_until_clean(), name="extension-sync-push")


async def _push_until_clean() -> None:
    global _dirty
    while _dirty:
        _dirty = False
        try:
            worker_ids = _connected_worker_ids(await asyncio.to_thread(_snapshot_nodes))
            if not worker_ids:
                return
            state = await asyncio.to_thread(_export_state)
        except Exception:
            logger.exception("extension sync export failed")
            return
        for node_id in worker_ids:
            try:
                await _call_rpc(node_id, state)
            except Exception:
                logger.exception("extension auto-sync to node %s failed", node_id)


async def on_node_state(node_id: str, state: str) -> None:
    """node_store listener: push current extension state to a worker on connect."""
    if state != "connected" or node_id == "primary":
        return
    try:
        nodes = await asyncio.to_thread(_snapshot_nodes)
        if node_id not in _connected_worker_ids(nodes):
            return
        payload = await asyncio.to_thread(_export_state)
        await _call_rpc(node_id, payload)
    except Exception:
        logger.exception("extension sync on connect failed for node %s", node_id)
