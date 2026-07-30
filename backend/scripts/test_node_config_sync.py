from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import _test_home
_test_home.isolate("bc-test-node-config-sync-")
os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="bc-test-node-config-sync-os-home-"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import node_config_sync  # noqa: E402

# Captured before any _Recorder swaps SURFACES out for fakes.
_REAL_SURFACES = node_config_sync.SURFACES


class _Recorder:
    """Swaps the real surfaces for fakes and records exports/pushes."""

    def __init__(self, nodes: list[dict], surface_names: tuple[str, ...] = ("extensions", "providers", "harness")):
        self.nodes = nodes
        self.exports: dict[str, int] = {name: 0 for name in surface_names}
        self.pushes: list[tuple[str, str]] = []  # (surface, node_id)
        self.ready_nodes: list[str] = []
        self.export_gate: asyncio.Event | None = None
        self.failing_exports: set[str] = set()
        self.surface_names = surface_names

    def install(self) -> None:
        surfaces = tuple(
            node_config_sync._Surface(
                name,
                f"sync_{name}",
                f"{name}_state",
                self._make_export(name),
            )
            for name in self.surface_names
        )
        node_config_sync.SURFACES = surfaces
        node_config_sync._SURFACES_BY_NAME = {s.name: s for s in surfaces}
        node_config_sync._snapshot_nodes = lambda: self.nodes
        node_config_sync._call_rpc = self._call_rpc
        node_config_sync._dirty = set()
        node_config_sync._push_task = None
        node_config_sync._connect_tasks = {}
        node_config_sync._loop = None
        node_config_sync._set_runtime_ready = (
            lambda node_id, *_: self.ready_nodes.append(node_id)
        )

    def _make_export(self, name: str):
        def export() -> dict:
            if name in self.failing_exports:
                raise RuntimeError(f"{name} exporter is broken")
            self.exports[name] += 1
            return {"surface": name}
        return export

    async def _call_rpc(self, node_id: str, surface, state: dict) -> None:
        if self.export_gate is not None:
            await self.export_gate.wait()
        self.pushes.append((surface.name, node_id))

    def pushed_nodes(self, surface: str) -> list[str]:
        return [node for name, node in self.pushes if name == surface]


def _worker(node_id: str, state: str = "connected", role: str = "worker_node") -> dict:
    return {"id": node_id, "role": role, "state": state}


async def _drain() -> None:
    tasks = [
        task
        for task in [
            node_config_sync._push_task,
            *node_config_sync._connect_tasks.values(),
        ]
        if task is not None
    ]
    if tasks:
        await asyncio.gather(*tasks)


def test_notify_coalesces_bursts_into_bounded_pushes() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1"), _worker("w2")])
        rec.install()
        rec.export_gate = asyncio.Event()
        for _ in range(5):
            node_config_sync.notify_changed("extensions")
            await asyncio.sleep(0)
        rec.export_gate.set()
        await _drain()
        if rec.exports["extensions"] > 2:
            raise AssertionError(f"burst of 5 notifies exported {rec.exports['extensions']} times")
        pushed = rec.pushed_nodes("extensions")
        if pushed.count("w1") < 1 or pushed.count("w2") < 1:
            raise AssertionError(f"workers missed the push: {rec.pushes}")
        # An unrelated surface must not be dragged along by the burst.
        if rec.exports["providers"] != 0:
            raise AssertionError("providers exported although only extensions changed")

    asyncio.run(scenario())


def test_notify_without_workers_skips_export_and_rearms() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1", state="disconnected"), _worker("primary", role="primary")])
        rec.install()
        node_config_sync.notify_changed("harness")
        await _drain()
        if rec.exports["harness"] != 0 or rec.pushes:
            raise AssertionError("exported/pushed although no worker is connected")
        rec.nodes[0]["state"] = "connected"
        node_config_sync.notify_changed("harness")
        await _drain()
        if rec.pushed_nodes("harness") != ["w1"]:
            raise AssertionError(f"reconnected worker not pushed: {rec.pushes}")

    asyncio.run(scenario())


def test_connect_listener_pushes_every_surface_to_that_worker_only() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1"), _worker("w2")])
        rec.install()
        await node_config_sync.on_node_state("w1", "connected")
        await _drain()
        if sorted(rec.pushes) != [("extensions", "w1"), ("harness", "w1"), ("providers", "w1")]:
            raise AssertionError(f"connect did not push every surface to w1 only: {rec.pushes}")
        if rec.ready_nodes != ["w1"]:
            raise AssertionError(f"worker became runnable before all projections: {rec.ready_nodes}")
        before = list(rec.pushes)
        await node_config_sync.on_node_state("w2", "disconnected")
        await node_config_sync.on_node_state("primary", "connected")
        if rec.pushes != before:
            raise AssertionError(f"unexpected extra pushes: {rec.pushes}")

    asyncio.run(scenario())


def test_failed_push_does_not_wedge_next_notify() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1")])
        rec.install()

        async def failing_rpc(node_id: str, surface, state: dict) -> None:
            raise RuntimeError("node down")

        node_config_sync._call_rpc = failing_rpc
        node_config_sync.notify_changed("providers")
        await _drain()

        node_config_sync._call_rpc = rec._call_rpc
        node_config_sync.notify_changed("providers")
        await _drain()
        if rec.pushed_nodes("providers") != ["w1"]:
            raise AssertionError(f"push loop wedged after failure: {rec.pushes}")

    asyncio.run(scenario())


def test_one_broken_exporter_does_not_block_other_surfaces() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1")])
        rec.install()
        rec.failing_exports = {"extensions"}
        await node_config_sync.on_node_state("w1", "connected")
        await _drain()
        if rec.pushed_nodes("extensions"):
            raise AssertionError("broken surface was pushed anyway")
        if rec.pushed_nodes("providers") != ["w1"] or rec.pushed_nodes("harness") != ["w1"]:
            raise AssertionError(f"healthy surfaces were skipped: {rec.pushes}")
        if rec.ready_nodes:
            raise AssertionError(f"worker became runnable after failed projection: {rec.ready_nodes}")

    asyncio.run(scenario())


def test_notify_from_worker_thread_reaches_the_bound_loop() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1")])
        rec.install()
        node_config_sync.bind_loop()
        # Store mutations run under asyncio.to_thread, so the publisher has no
        # running loop of its own.
        await asyncio.to_thread(node_config_sync.notify_changed, "providers")
        for _ in range(100):
            await asyncio.sleep(0)
            if node_config_sync._push_task is not None:
                break
        await _drain()
        if rec.pushed_nodes("providers") != ["w1"]:
            raise AssertionError(f"off-loop notify never pushed: {rec.pushes}")

    asyncio.run(scenario())


def test_bind_loop_off_loop_never_raises() -> None:
    """main.py wires the listener at import time, where no loop is running.

    A raising bind_loop there aborted the whole machine-node wiring block, so
    the connect listener was never registered and nodes silently stopped
    receiving any config.
    """
    node_config_sync._loop = None
    node_config_sync.bind_loop()  # module scope: no running loop
    if node_config_sync._loop is not None:
        raise AssertionError("bind_loop bound a loop that was not running")


def test_connect_listener_binds_the_loop_itself() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1")])
        rec.install()
        node_config_sync._loop = None
        await node_config_sync.on_node_state("w1", "connected")
        await _drain()
        if node_config_sync._loop is None:
            raise AssertionError("connect listener did not self-bind the loop")

    asyncio.run(scenario())


def test_connect_listener_does_not_wait_for_projection_rpc() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1")])
        rec.install()
        rec.export_gate = asyncio.Event()
        await asyncio.wait_for(
            node_config_sync.on_node_state("w1", "connected"),
            timeout=0.5,
        )
        await asyncio.sleep(0)
        task = node_config_sync._connect_tasks.get("w1")
        if task is None or task.done():
            raise AssertionError("connected projection was not running in the background")
        rec.export_gate.set()
        await _drain()

    asyncio.run(scenario())


def test_disconnect_cancels_connected_projection() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1")])
        rec.install()
        rec.export_gate = asyncio.Event()
        await node_config_sync.on_node_state("w1", "connected")
        await asyncio.sleep(0)
        task = node_config_sync._connect_tasks.get("w1")
        if task is None:
            raise AssertionError("connected projection task was not tracked")
        await node_config_sync.on_node_state("w1", "disconnected")
        await asyncio.gather(task, return_exceptions=True)
        if not task.cancelled() or node_config_sync._connect_tasks:
            raise AssertionError("disconnect left a connected projection task running")

    asyncio.run(scenario())


def test_extension_surface_gets_a_longer_rpc_timeout() -> None:
    """Applying extensions installs packages on the node; the default RPC
    timeout expired mid-install and left the node half-populated."""
    by_name = {s.name: s for s in _REAL_SURFACES}
    if by_name["extensions"].timeout_s <= by_name["providers"].timeout_s:
        raise AssertionError("extension sync must allow more time than a config push")


def test_unknown_surface_is_rejected() -> None:
    rec = _Recorder([_worker("w1")])
    rec.install()
    try:
        node_config_sync.notify_changed("nope")
    except ValueError:
        return
    raise AssertionError("unknown surface name was accepted")


if __name__ == "__main__":
    test_notify_coalesces_bursts_into_bounded_pushes()
    test_notify_without_workers_skips_export_and_rearms()
    test_connect_listener_pushes_every_surface_to_that_worker_only()
    test_failed_push_does_not_wedge_next_notify()
    test_one_broken_exporter_does_not_block_other_surfaces()
    test_notify_from_worker_thread_reaches_the_bound_loop()
    test_bind_loop_off_loop_never_raises()
    test_connect_listener_binds_the_loop_itself()
    test_connect_listener_does_not_wait_for_projection_rpc()
    test_disconnect_cancels_connected_projection()
    test_extension_surface_gets_a_longer_rpc_timeout()
    test_unknown_surface_is_rejected()
    print("node_config_sync tests passed")
