from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-rpc-thread-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _ticker(stop: asyncio.Event) -> int:
    ticks = 0
    while not stop.is_set():
        await asyncio.sleep(0.01)
        ticks += 1
    return ticks


def _make_git_repo() -> str:
    root = tempfile.mkdtemp(prefix="bc-rpc-git-", dir=_TMP_HOME)
    subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)
    for i in range(2500):
        Path(root, f"f{i}.txt").write_text("x")
    return root


async def _run() -> bool:
    topo = Path(_TMP_HOME) / "topology.yaml"
    topo.write_text(
        "schema_version: 1\n"
        f"primary: {{id: primary, address: 'ws://localhost:9999', cwd_roots: ['{_TMP_HOME}']}}\n"
        "nodes: {}\n"
    )
    os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = str(topo)
    os.environ["BETTER_CLAUDE_NODE_TOKEN"] = "test"

    import node_rpc_handlers

    repo = _make_git_repo()
    stop = asyncio.Event()
    ticker_task = asyncio.create_task(_ticker(stop))
    try:
        started = time.perf_counter()
        result = await node_rpc_handlers.call_local_or_remote(
            "primary", "get_git_status", {"cwd": repo},
        )
        elapsed = time.perf_counter() - started
        stop.set()
        ticks = await ticker_task
        if not result.get("is_git"):
            print(f"{FAIL} local RPC returned non-git status: {result!r}")
            return False
        if elapsed >= 0.05 and ticks == 0:
            print(f"{FAIL} local RPC blocked event loop for {elapsed:.3f}s")
            return False
        print(f"{PASS} local RPC dispatch yielded during git status ({elapsed:.3f}s, ticks={ticks})")
        return True
    finally:
        stop.set()
        if not ticker_task.done():
            await ticker_task
        shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    try:
        ok = asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(0 if ok else 1)
