from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordination  # noqa: E402
from runner_better_agent import LockRegistry  # noqa: E402

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
    check(result.get("waited") is True, "multi lock reports waited=true when it had to block for a holder")
    check(float(result.get("waited_seconds") or 0) > 0, "multi lock reports positive waited_seconds when contended")
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


async def test_immediate_acquire_reports_no_wait() -> None:
    coordination._locks.clear()

    single = await coordination.lock_ops(key="file-a")
    check(single.get("success") is True, "uncontended single-key acquire succeeds")
    check(single.get("waited") is False, "uncontended single-key acquire reports waited=false")
    check(single.get("waited_seconds") == 0.0, "uncontended single-key acquire reports zero waited_seconds")
    await coordination.lock_ops(key="file-a", release=True, holder_token=str(single["holder_token"]))

    multi = await coordination.lock_ops(keys=["file-a", "file-b"], key="", timeout_seconds=1)
    check(multi.get("success") is True, "uncontended multi-key acquire succeeds")
    check(multi.get("waited") is False, "uncontended multi-key acquire reports waited=false")
    await coordination.lock_ops(
        key="", keys=multi["keys"], release=True, holder_token=str(multi["holder_token"])
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


def test_better_agent_runner_requires_own_live_file_lock() -> None:
    registry = LockRegistry()
    target = Path("/tmp/better-agent-lock-test.txt")
    check(
        registry.error_for_write(target) is not None,
        "Better Agent runner blocks writes without a locally acquired file lock",
    )
    registry.record_lock_result({
        "success": True,
        "keys": [f"file_edit:{target}"],
        "holder_token": "token",
        "expires_in_seconds": 30,
    })
    check(
        registry.error_for_write(target) is None,
        "Better Agent runner allows writes after its own lock_ops acquire succeeds",
    )
    registry.record_lock_result({
        "success": True,
        "released": True,
        "keys": [f"file_edit:{target}"],
    })
    check(
        registry.error_for_write(target) is not None,
        "Better Agent runner blocks writes after lock release",
    )


async def main() -> int:
    await test_multi_lock_accumulates_until_all_locked()
    await test_immediate_acquire_reports_no_wait()
    await test_multi_lock_timeout_releases_partial_locks()
    await test_multi_release_is_atomic()
    test_better_agent_runner_requires_own_live_file_lock()
    if _FAILURES:
        print("\nFAILURES:")
        for failure in _FAILURES:
            print(f" - {failure}")
        return 1
    print("\ncoordination lock tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
