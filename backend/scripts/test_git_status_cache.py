from __future__ import annotations

import asyncio
import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-git-status-cache-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _run() -> bool:
    import git_api
    import git_status_cache as gsc
    from git_status_cache import GitStatusCache

    calls: list[tuple[str, str]] = []
    released = asyncio.Event()

    async def fake_node_op(node_id: str, method: str, params: dict):
        calls.append((method, params.get("cwd", "")))
        if method == "get_git_status":
            await released.wait()
            return {"is_git": True, "branch": f"main-{len(calls)}"}
        return {"ok": True}

    shipped_ttl = gsc.cache.ttl_seconds
    if shipped_ttl < 15.0:
        print(f"{FAIL} git-status cache TTL is below frontend poll interval: {shipped_ttl!r}")
        return False

    cache = GitStatusCache(ttl_seconds=60.0)
    original_router_cache = git_api.git_status_cache
    original_cache_node_op = gsc.node_op
    original_router_node_op = git_api.node_op
    gsc.node_op = fake_node_op
    git_api.node_op = fake_node_op
    git_api.git_status_cache = cache
    try:
        first = asyncio.create_task(cache.get("primary", "/repo"))
        second = asyncio.create_task(cache.get("primary", "/repo"))
        await asyncio.sleep(0)
        released.set()
        first_result, second_result = await asyncio.gather(first, second)
        get_calls = [call for call in calls if call[0] == "get_git_status"]
        if len(get_calls) != 1:
            print(f"{FAIL} concurrent git-status calls were not coalesced: {calls!r}")
            return False
        if first_result != second_result:
            print(f"{FAIL} coalesced callers received different results: {first_result!r} {second_result!r}")
            return False

        cached = await cache.get("primary", "/repo")
        get_calls = [call for call in calls if call[0] == "get_git_status"]
        if len(get_calls) != 1 or cached != first_result:
            print(f"{FAIL} cached git-status result was not reused: calls={calls!r} cached={cached!r}")
            return False

        await cache.get("primary", "/other")
        await git_api.post_git_commit({"node_id": "primary", "cwd": "/repo", "message": "x"})
        await cache.get("primary", "/other")
        get_calls = [call for call in calls if call[0] == "get_git_status"]
        if len(get_calls) != 2:
            print(f"{FAIL} commit invalidated an unrelated cwd: {calls!r}")
            return False
        await cache.get("primary", "/repo")
        get_calls = [call for call in calls if call[0] == "get_git_status"]
        if len(get_calls) != 3:
            print(f"{FAIL} commit did not invalidate git-status cache: {calls!r}")
            return False

        stale_cache = GitStatusCache(ttl_seconds=0.0)
        git_api.git_status_cache = stale_cache
        calls.clear()
        released.clear()
        refresh = asyncio.create_task(stale_cache.get("primary", "/repo"))
        await asyncio.sleep(0)
        released.set()
        warm = await refresh
        released.clear()
        stale = await asyncio.wait_for(
            stale_cache.get("primary", "/repo"),
            timeout=0.1,
        )
        if stale != warm:
            print(f"{FAIL} expired cache did not return stale value: stale={stale!r} warm={warm!r}")
            return False
        await asyncio.sleep(0)
        get_calls = [call for call in calls if call[0] == "get_git_status"]
        if len(get_calls) != 2:
            print(f"{FAIL} stale git-status did not start exactly one refresh: {calls!r}")
            return False
        released.set()
        await asyncio.sleep(0)

        print(f"{PASS} git-status cache coalesces, reuses, and invalidates")
        return True
    finally:
        git_api.git_status_cache = original_router_cache
        gsc.node_op = original_cache_node_op
        git_api.node_op = original_router_node_op


if __name__ == "__main__":
    try:
        ok = asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(0 if ok else 1)
