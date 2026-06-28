from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordination  # noqa: E402

_FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _FAILURES.append(msg)
        print(f"  FAIL: {msg}")
        return
    print(f"  ok:   {msg}")


async def test_multi_lock_accumulates_until_all_locked() -> None:
    coordination._locks.clear()
    blocker = await coordination.lock_ops(key="file-b")

    async def release_blocker() -> None:
        await asyncio.sleep(0.03)
        await coordination.lock_ops(
            key="file-b",
            release=True,
            holder_token=str(blocker["holder_token"]),
        )

    acquire_task = asyncio.create_task(
        coordination.lock_ops(keys=["file-a", "file-b", "file-c"], key="", timeout_seconds=1)
    )
    release_task = asyncio.create_task(release_blocker())
    await asyncio.sleep(0.01)

    token = None
    async with coordination._locks_guard:
        token = coordination._locks.get("file-a", {}).get("holder_token")
        accumulated = {"file-a", "file-c"}.issubset(coordination._locks.keys())

    result = await acquire_task
    await release_task

    check(accumulated and token, "multi lock accumulates available keys while waiting")
    check(result.get("success") is True, "multi lock waits until all requested keys are locked")
    check(result.get("keys") == ["file-a", "file-b", "file-c"], "multi lock returns requested keys")
    check(
        all(
            coordination._locks[key]["holder_token"] == result["holder_token"]
            for key in result["keys"]
        ),
        "multi lock uses one holder token for every acquired key",
    )
    await coordination.lock_ops(
        key="",
        keys=result["keys"],
        release=True,
        holder_token=str(result["holder_token"]),
    )
    coordination._locks.clear()


async def test_multi_lock_timeout_releases_partial_locks() -> None:
    coordination._locks.clear()
    blocker = await coordination.lock_ops(key="file-b")
    result = await coordination.lock_ops(keys=["file-a", "file-b"], key="", timeout_seconds=0.01)

    check(result.get("success") is False and result.get("error") == "timeout", "multi lock times out")
    check("file-a" not in coordination._locks, "multi lock timeout releases accumulated keys")
    check("file-b" in coordination._locks, "multi lock timeout preserves locks owned by others")

    await coordination.lock_ops(key="file-b", release=True, holder_token=str(blocker["holder_token"]))
    coordination._locks.clear()


async def test_multi_release_is_atomic() -> None:
    coordination._locks.clear()
    acquired = await coordination.lock_ops(keys=["file-a", "file-b"], key="")
    result = await coordination.lock_ops(
        keys=["file-a", "file-b"],
        key="",
        release=True,
        holder_token="wrong",
    )

    check(result.get("success") is False and result.get("error") == "invalid_holder_token", "multi release rejects wrong token")
    check({"file-a", "file-b"}.issubset(coordination._locks.keys()), "failed multi release leaves all locks held")

    result = await coordination.lock_ops(
        keys=["file-a", "file-b"],
        key="",
        release=True,
        holder_token=str(acquired["holder_token"]),
    )
    check(result.get("success") is True and result.get("released") is True, "multi release frees all keys")
    check(not coordination._locks, "multi release removes acquired locks")


async def main() -> int:
    await test_multi_lock_accumulates_until_all_locked()
    await test_multi_lock_timeout_releases_partial_locks()
    await test_multi_release_is_atomic()
    if _FAILURES:
        print("\nFAILURES:")
        for failure in _FAILURES:
            print(f" - {failure}")
        return 1
    print("\ncoordination lock tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
