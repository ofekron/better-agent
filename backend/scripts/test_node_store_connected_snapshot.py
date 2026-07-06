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
            app_commit_sha="a" * 40,
            app_dirty=True,
        )
        await node_store.register(
            NodeSpec(id="worker-a", role="worker_node", address="", cwd_roots=()),
            object(),
            app_commit_sha="a" * 40,
        )
        original_commit = node_store.app_version.current_commit_sha
        original_dirty = node_store.app_version.current_dirty
        node_store.app_version.current_commit_sha = lambda: "a" * 40
        node_store.app_version.current_dirty = lambda: False
        v2, ids2 = node_store.connected_worker_node_ids_snapshot()
        try:
            if ids2 != ("worker-a", "worker-b"):
                print(f"worker snapshot mismatch: {ids2!r}")
                return 1
            snap = {row["id"]: row for row in node_store.snapshot()}
            if snap["worker-b"]["version_status"] != "ok" or snap["worker-b"]["app_dirty"] is not True:
                print(f"worker version projection mismatch: {snap['worker-b']!r}")
                return 1
            if v2 <= v1 or v1 <= v0:
                print(f"state version did not advance: {(v0, v1, v2)!r}")
                return 1

            await node_store.register(
                NodeSpec(id="worker-c", role="worker_node", address="", cwd_roots=()),
                object(),
                app_commit_sha="b" * 40,
            )
            v_mismatch, ids_mismatch = node_store.connected_worker_node_ids_snapshot()
            if ids_mismatch != ("worker-a", "worker-b"):
                print(f"mismatched worker leaked into ready snapshot: {ids_mismatch!r}")
                return 1
            snap = {row["id"]: row for row in node_store.snapshot()}
            if snap["worker-c"]["version_status"] != "mismatch":
                print(f"mismatch status not projected: {snap['worker-c']!r}")
                return 1

            await node_store.register(
                NodeSpec(id="worker-c", role="worker_node", address="", cwd_roots=()),
                object(),
                app_commit_sha="a" * 40,
            )
            v_rejoin, ids_rejoin = node_store.connected_worker_node_ids_snapshot()
            if ids_rejoin != ("worker-a", "worker-b", "worker-c"):
                print(f"rejoined worker missing from ready snapshot: {ids_rejoin!r}")
                return 1
            if v_rejoin <= v_mismatch:
                print(f"version reconnect did not advance state version: {(v_mismatch, v_rejoin)!r}")
                return 1

            await node_store.unregister("worker-c")
            await node_store.unregister("worker-a")
            v3, ids3 = node_store.connected_worker_node_ids_snapshot()
            if ids3 != ("worker-b",):
                print(f"unregister snapshot mismatch: {ids3!r}")
                return 1
            if v3 <= v2:
                print(f"unregister did not advance version: {(v2, v3)!r}")
                return 1
        finally:
            node_store.app_version.current_commit_sha = original_commit
            node_store.app_version.current_dirty = original_dirty

        print("PASS test_node_store_connected_snapshot")
        return 0
    finally:
        await node_store.stop_offset_flush_loop()
        node_store.reset_for_tests()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
