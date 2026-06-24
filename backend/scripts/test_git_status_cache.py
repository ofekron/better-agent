from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-git-status-cache-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _run() -> bool:
    import main

    calls: list[tuple[str, str]] = []
    released = asyncio.Event()

    async def fake_file_op(node_id: str, method: str, params: dict):
        calls.append((method, params.get("cwd", "")))
        if method == "get_git_status":
            await released.wait()
            return {"is_git": True, "branch": f"main-{len(calls)}"}
        return {"ok": True}

    original_file_op = main._file_op
    original_ttl = main._GIT_STATUS_TTL_SECONDS
    main._file_op = fake_file_op
    main._GIT_STATUS_TTL_SECONDS = 60.0
    main._clear_git_status_cache()
    try:
        first = asyncio.create_task(main._cached_git_status("primary", "/repo"))
        second = asyncio.create_task(main._cached_git_status("primary", "/repo"))
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

        cached = await main._cached_git_status("primary", "/repo")
        get_calls = [call for call in calls if call[0] == "get_git_status"]
        if len(get_calls) != 1 or cached != first_result:
            print(f"{FAIL} cached git-status result was not reused: calls={calls!r} cached={cached!r}")
            return False

        await main.post_git_commit({"node_id": "primary", "cwd": "/repo", "message": "x"})
        await main._cached_git_status("primary", "/repo")
        get_calls = [call for call in calls if call[0] == "get_git_status"]
        if len(get_calls) != 2:
            print(f"{FAIL} commit did not invalidate git-status cache: {calls!r}")
            return False

        print(f"{PASS} git-status cache coalesces, reuses, and invalidates")
        return True
    finally:
        main._file_op = original_file_op
        main._GIT_STATUS_TTL_SECONDS = original_ttl
        main._clear_git_status_cache()


if __name__ == "__main__":
    try:
        ok = asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(0 if ok else 1)
