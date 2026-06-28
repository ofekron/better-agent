from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-node-store-snapshot-")
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import node_store  # noqa: E402
from topology import NodeSpec  # noqa: E402


async def _main() -> int:
    try:
        node_store.reset_for_tests()
        v0, ids0 = node_store.connected_worker_node_ids_snapshot()
        if ids0 != ():
            print(f"initial ids not empty: {ids0!r}")
            return 1

        await node_store.register(
            NodeSpec(id="primary", role="primary", address="local", cwd_roots=()),
            object(),
        )
        v1, ids1 = node_store.connected_worker_node_ids_snapshot()
        if ids1 != ():
            print(f"primary leaked into worker snapshot: {ids1!r}")
            return 1

        await node_store.register(
            NodeSpec(id="worker-b", role="worker_node", address="", cwd_roots=()),
            object(),
        )
        await node_store.register(
            NodeSpec(id="worker-a", role="worker_node", address="", cwd_roots=()),
            object(),
        )
        v2, ids2 = node_store.connected_worker_node_ids_snapshot()
        if ids2 != ("worker-a", "worker-b"):
            print(f"worker snapshot mismatch: {ids2!r}")
            return 1
        if v2 <= v1 or v1 <= v0:
            print(f"state version did not advance: {(v0, v1, v2)!r}")
            return 1

        await node_store.unregister("worker-a")
        v3, ids3 = node_store.connected_worker_node_ids_snapshot()
        if ids3 != ("worker-b",):
            print(f"unregister snapshot mismatch: {ids3!r}")
            return 1
        if v3 <= v2:
            print(f"unregister did not advance version: {(v2, v3)!r}")
            return 1

        print("PASS test_node_store_connected_snapshot")
        return 0
    finally:
        await node_store.stop_offset_flush_loop()
        node_store.reset_for_tests()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
