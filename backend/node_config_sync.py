"""Event-driven projection of primary-owned config onto worker nodes.

The primary owns extensions, provider config, and harness profiles. A worker
node holds a pushed projection of each. This module is the node subsystem's
single subscriber for the facts that affect those projections:

- "<surface> changed" (`notify_changed`, published by whichever store/route
  mutated it): coalesces bursts into one export and pushes that surface to
  every connected worker.
- "node connected" (`on_node_state`, registered as a node_store listener):
  pushes every surface to the worker that just (re)connected, so it never
  runs a stale projection after downtime.

Adding a surface means adding one `_Surface` entry — the connect path, the
change path, coalescing, and failure isolation all come for free.

Push failures are logged and dropped: the next change to that surface or the
node's next reconnect re-publishes it in full, so a dead or wedged node
cannot stall the loop or trigger retry storms.

Provider credentials are deliberately NOT part of this projection. The
broadcast export is credential-free; API keys sync only through the explicit
per-node route, which requires an operator-selected node and a secure
transport check.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

_SYNC_RPC_TIMEOUT_S = 180.0
# Applying an extension payload installs every package's dependencies on the
# node. On a fresh node that is tens of pip installs, which routinely outruns
# the default timeout — and a timeout there leaves the node half-populated.
_EXTENSION_SYNC_RPC_TIMEOUT_S = 1800.0


@dataclass(frozen=True)
class _Surface:
    name: str
    rpc: str
    param: str
    export: Callable[[], dict[str, Any]]
    timeout_s: float = _SYNC_RPC_TIMEOUT_S


def _export_extensions() -> dict[str, Any]:
    import extension_store

    return extension_store.export_extension_sync_state()


def _export_providers() -> dict[str, Any]:
    import config_store

    # No api_key_ids: the broadcast projection never carries credentials.
    return config_store.export_provider_sync_state()


def _export_harness() -> dict[str, Any]:
    import harness_profile_store

    return harness_profile_store.export_harness_sync_state()


SURFACES: tuple[_Surface, ...] = (
    _Surface(
        "extensions",
        "sync_extension_config",
        "extension_state",
        _export_extensions,
        _EXTENSION_SYNC_RPC_TIMEOUT_S,
    ),
    _Surface("providers", "sync_provider_config", "provider_state", _export_providers),
    _Surface("harness", "sync_harness_profile", "harness_state", _export_harness),
)

_SURFACES_BY_NAME = {surface.name: surface for surface in SURFACES}

_dirty: set[str] = set()
_push_task: asyncio.Task[None] | None = None
_connect_tasks: dict[str, asyncio.Task[None]] = {}
_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Record the main loop so `notify_changed` works from worker threads.

    Store mutations run under `asyncio.to_thread`, so the publisher often has
    no running loop of its own. Binding is self-service: every on-loop entry
    point calls this, so there is no import-time ordering to get wrong (an
    import-time bind would raise `no running event loop` and take its caller
    down with it).
    """
    global _loop
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
    _loop = loop


def _snapshot_nodes() -> list[dict]:
    import node_store

    return node_store.snapshot()


def _connected_worker_ids(nodes: list[dict]) -> list[str]:
    return [
        str(node.get("id") or "")
        for node in nodes
        if node.get("id")
        and node.get("id") != "primary"
        and node.get("role") == "worker_node"
        and node.get("state") == "connected"
    ]


async def _call_rpc(node_id: str, surface: _Surface, state: dict[str, Any]) -> None:
    from node_rpc_handlers import call_local_or_remote

    await call_local_or_remote(
        node_id,
        surface.rpc,
        {surface.param: state},
        timeout=surface.timeout_s,
        version_ready_required=True,
    )


def notify_changed(surface_name: str) -> None:
    """Publish the "<surface_name> changed" fact.

    Safe to call from the main loop or from a worker thread; bursts coalesce
    into a single export+push pass per surface (plus one follow-up pass if
    further changes landed while a push was in flight).
    """
    if surface_name not in _SURFACES_BY_NAME:
        raise ValueError(f"unknown node config sync surface {surface_name!r}")
    _dirty.add(surface_name)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        _schedule_push(running)
        return
    loop = _loop
    if loop is None or loop.is_closed():
        logger.warning("node config sync notify before loop bind; skipped")
        return
    loop.call_soon_threadsafe(_schedule_push, loop)


def _schedule_push(loop: asyncio.AbstractEventLoop) -> None:
    global _push_task
    bind_loop(loop)
    if _push_task is None or _push_task.done():
        _push_task = loop.create_task(_push_until_clean(), name="node-config-sync-push")


async def _push_surface(surface: _Surface, node_ids: list[str]) -> None:
    try:
        state = await asyncio.to_thread(surface.export)
    except Exception:
        # A broken exporter must not take the other surfaces down with it.
        logger.exception("node config sync export failed for surface %s", surface.name)
        return
    for node_id in node_ids:
        try:
            await _call_rpc(node_id, surface, state)
            logger.info("node config sync of %s to node %s ok", surface.name, node_id)
        except Exception:
            logger.exception(
                "node config sync of %s to node %s failed", surface.name, node_id
            )


async def _push_until_clean() -> None:
    while _dirty:
        pending = sorted(_dirty)
        _dirty.clear()
        try:
            node_ids = _connected_worker_ids(await asyncio.to_thread(_snapshot_nodes))
        except Exception:
            logger.exception("node config sync could not snapshot nodes")
            return
        if not node_ids:
            return
        for name in pending:
            await _push_surface(_SURFACES_BY_NAME[name], node_ids)


async def _project_connected_node(node_id: str) -> None:
    try:
        nodes = await asyncio.to_thread(_snapshot_nodes)
    except Exception:
        logger.exception("node config sync could not snapshot nodes for %s", node_id)
        return
    if node_id not in _connected_worker_ids(nodes):
        logger.warning(
            "node config sync skipped for %s: not a connected worker in the snapshot",
            node_id,
        )
        return
    logger.info("node config sync: projecting all surfaces onto %s", node_id)
    for surface in SURFACES:
        await _push_surface(surface, [node_id])


def _forget_connect_task(node_id: str, task: asyncio.Task[None]) -> None:
    if _connect_tasks.get(node_id) is task:
        _connect_tasks.pop(node_id, None)


def _cancel_connect_projection(node_id: str) -> None:
    task = _connect_tasks.pop(node_id, None)
    if task is not None and not task.done():
        task.cancel()


def _schedule_connect_projection(
    loop: asyncio.AbstractEventLoop,
    node_id: str,
) -> None:
    _cancel_connect_projection(node_id)
    task = loop.create_task(
        _project_connected_node(node_id),
        name=f"node-config-sync-connect-{node_id}",
    )
    _connect_tasks[node_id] = task
    task.add_done_callback(
        lambda completed, target=node_id: _forget_connect_task(target, completed)
    )


async def on_node_state(node_id: str, state: str) -> None:
    """node_store listener: project every surface onto a worker on connect."""
    loop = asyncio.get_running_loop()
    bind_loop(loop)
    if state == "disconnected":
        _cancel_connect_projection(node_id)
        return
    if state != "connected" or node_id == "primary":
        return
    _schedule_connect_projection(loop, node_id)


async def shutdown() -> None:
    global _push_task, _loop
    tasks = [
        task
        for task in [_push_task, *_connect_tasks.values()]
        if task is not None and not task.done()
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _push_task = None
    _connect_tasks.clear()
    _dirty.clear()
    _loop = None
