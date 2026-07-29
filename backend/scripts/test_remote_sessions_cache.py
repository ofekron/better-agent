from __future__ import annotations

import asyncio
import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-remote-sessions-cache-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _sessions(*ids: str) -> list[dict]:
    return [{"id": sid, "updated_at": "2026-07-29T00:00:00+00:00"} for sid in ids]


async def _run() -> bool:
    from remote_sessions_cache import RemoteSessionsCache

    cache = RemoteSessionsCache(ttl_seconds=60.0)

    if cache.get("node-a") != (None, False, 0):
        print(f"{FAIL} empty cache must report a miss")
        return False

    cache.put("node-a", _sessions("s1", "s2", "s3"))
    version_after_first = cache.version()
    if version_after_first != 1:
        print(f"{FAIL} first put must bump the version: {version_after_first!r}")
        return False

    cached, fresh, total = cache.get("node-a")
    if not fresh or total != 3 or [s["id"] for s in cached] != ["s1", "s2", "s3"]:
        print(f"{FAIL} fresh entry not served correctly: {cached!r} fresh={fresh} total={total}")
        return False

    capped, _fresh, total = cache.get("node-a", limit=2)
    if len(capped) != 2 or total != 3:
        print(f"{FAIL} limit must cap the copy but report the full total: {capped!r} total={total}")
        return False

    capped[0]["id"] = "mutated"
    untouched, _fresh, _total = cache.get("node-a")
    if untouched[0]["id"] != "s1":
        print(f"{FAIL} callers must receive copies, not the cached objects")
        return False

    cache.put("node-a", _sessions("s1", "s2", "s3"))
    if cache.version() != version_after_first:
        print(f"{FAIL} an unchanged list must not bump the version: {cache.version()!r}")
        return False

    cache.put("node-a", _sessions("s1", "s2"))
    if cache.version() != version_after_first + 1:
        print(f"{FAIL} a changed list must bump the version: {cache.version()!r}")
        return False

    stale = RemoteSessionsCache(ttl_seconds=0.0)
    stale.put("node-b", _sessions("old"))
    cached, fresh, _total = stale.get("node-b")
    if fresh or [s["id"] for s in cached] != ["old"]:
        print(f"{FAIL} expired entry must be reported stale but still returned: {cached!r} fresh={fresh}")
        return False

    fetches: list[str] = []
    release = asyncio.Event()

    async def slow_fetch(node_id: str) -> list[dict]:
        fetches.append(node_id)
        await release.wait()
        return _sessions("fresh")

    stale.fetch_live = slow_fetch
    stale.schedule_refresh("node-b")
    stale.schedule_refresh("node-b")
    await asyncio.sleep(0)
    if len(fetches) != 1:
        print(f"{FAIL} concurrent refreshes must be coalesced: {fetches!r}")
        return False
    release.set()
    for _ in range(50):
        await asyncio.sleep(0)
        if [s["id"] for s in stale.get("node-b")[0]] == ["fresh"]:
            break
    if [s["id"] for s in stale.get("node-b")[0]] != ["fresh"]:
        print(f"{FAIL} background refresh never landed: {stale.get('node-b')!r}")
        return False

    release.clear()
    fetches.clear()
    stale.schedule_refresh("node-b")
    await asyncio.sleep(0)
    if len(fetches) != 1:
        print(f"{FAIL} refresh slot was not released after completion: {fetches!r}")
        return False
    release.set()
    await asyncio.sleep(0)

    cache.clear()
    if cache.get("node-a") != (None, False, 0) or cache.version() != 0:
        print(f"{FAIL} clear must drop entries and reset the version")
        return False

    print(f"{PASS} remote-sessions cache serves, versions, coalesces and clears")
    return True


if __name__ == "__main__":
    try:
        ok = asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(0 if ok else 1)
