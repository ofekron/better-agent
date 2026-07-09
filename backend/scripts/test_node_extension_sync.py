from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import _test_home
_test_home.isolate("bc-test-node-ext-sync-")
os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="bc-test-node-ext-sync-os-home-"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import node_extension_sync  # noqa: E402


class _Recorder:
    def __init__(self, nodes: list[dict]):
        self.nodes = nodes
        self.exports = 0
        self.pushes: list[str] = []
        self.export_gate: asyncio.Event | None = None

    def install(self) -> None:
        def export_state() -> dict:
            self.exports += 1
            return {"store": {"extensions": {}}, "artifacts": []}

        async def call_rpc(node_id: str, state: dict) -> None:
            if self.export_gate is not None:
                await self.export_gate.wait()
            self.pushes.append(node_id)

        node_extension_sync._export_state = export_state
        node_extension_sync._snapshot_nodes = lambda: self.nodes
        node_extension_sync._call_rpc = call_rpc
        node_extension_sync._dirty = False
        node_extension_sync._push_task = None


def _worker(node_id: str, state: str = "connected", role: str = "worker_node") -> dict:
    return {"id": node_id, "role": role, "state": state}


async def _drain() -> None:
    task = node_extension_sync._push_task
    if task is not None:
        await task


def test_notify_coalesces_bursts_into_bounded_pushes() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1"), _worker("w2")])
        rec.install()
        rec.export_gate = asyncio.Event()
        for _ in range(5):
            node_extension_sync.notify_extensions_changed()
            await asyncio.sleep(0)
        rec.export_gate.set()
        await _drain()
        if rec.exports > 2:
            raise AssertionError(f"burst of 5 notifies exported {rec.exports} times")
        if rec.pushes.count("w1") < 1 or rec.pushes.count("w2") < 1:
            raise AssertionError(f"workers missed the push: {rec.pushes}")

    asyncio.run(scenario())


def test_notify_without_workers_skips_export_and_rearms() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1", state="disconnected"), _worker("primary", role="primary")])
        rec.install()
        node_extension_sync.notify_extensions_changed()
        await _drain()
        if rec.exports != 0 or rec.pushes:
            raise AssertionError("exported/pushed although no worker is connected")
        rec.nodes[0]["state"] = "connected"
        node_extension_sync.notify_extensions_changed()
        await _drain()
        if rec.pushes != ["w1"]:
            raise AssertionError(f"reconnected worker not pushed: {rec.pushes}")

    asyncio.run(scenario())


def test_connect_listener_pushes_to_that_worker_only() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1"), _worker("w2")])
        rec.install()
        await node_extension_sync.on_node_state("w1", "connected")
        if rec.pushes != ["w1"]:
            raise AssertionError(f"connect push wrong targets: {rec.pushes}")
        await node_extension_sync.on_node_state("w2", "disconnected")
        await node_extension_sync.on_node_state("primary", "connected")
        if rec.pushes != ["w1"]:
            raise AssertionError(f"unexpected extra pushes: {rec.pushes}")

    asyncio.run(scenario())


def test_failed_push_does_not_wedge_next_notify() -> None:
    async def scenario() -> None:
        rec = _Recorder([_worker("w1")])
        rec.install()

        async def failing_rpc(node_id: str, state: dict) -> None:
            raise RuntimeError("node down")

        node_extension_sync._call_rpc = failing_rpc
        node_extension_sync.notify_extensions_changed()
        await _drain()

        async def ok_rpc(node_id: str, state: dict) -> None:
            rec.pushes.append(node_id)

        node_extension_sync._call_rpc = ok_rpc
        node_extension_sync.notify_extensions_changed()
        await _drain()
        if rec.pushes != ["w1"]:
            raise AssertionError(f"push loop wedged after failure: {rec.pushes}")

    asyncio.run(scenario())


if __name__ == "__main__":
    test_notify_coalesces_bursts_into_bounded_pushes()
    test_notify_without_workers_skips_export_and_rearms()
    test_connect_listener_pushes_to_that_worker_only()
    test_failed_push_does_not_wedge_next_notify()
    print("node_extension_sync tests passed")
