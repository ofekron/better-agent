from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordination  # noqa: E402
import runner_better_agent  # noqa: E402
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
    blocker = await coordination.lock_ops(
        key="file-b",
        owner={"source": "blocking-test", "app_session_id": "holder-session"},
    )
    before_expiry = coordination._locks["file-b"]["expires_at"]
    result = await coordination.lock_ops(keys=["file-a", "file-b"], key="", timeout_seconds=0.01)

    check(result.get("success") is False and result.get("error") == "timeout", "multi lock times out")
    check("file-a" not in coordination._locks, "multi lock timeout releases accumulated keys")
    check("file-b" in coordination._locks, "multi lock timeout preserves locks owned by others")
    check(coordination._locks["file-b"]["expires_at"] == before_expiry, "multi lock timeout does not renew blocked lock")
    check(
        ((result.get("holder") or {}).get("owner") or {}).get("source") == "blocking-test",
        "multi lock timeout reports blocking holder source",
    )

    await coordination.lock_ops(key="file-b", release=True, holder_token=str(blocker["holder_token"]))
    coordination._locks.clear()


async def test_single_lock_conflict_reports_holder_metadata_without_token() -> None:
    coordination._locks.clear()
    long_source = "x" * (coordination._OWNER_FIELD_MAX_CHARS + 20)  # type: ignore[attr-defined]
    blocker = await coordination.lock_ops(
        key="file-a",
        owner={"source": long_source, "app_session_id": "session-a", "ignored": "value"},
    )
    result = await coordination.lock_ops(key="file-a")

    check(result.get("success") is False and result.get("error") == "locked", "single lock conflict fails closed")
    holder = result.get("holder") or {}
    owner = holder.get("owner") or {}
    check(
        owner.get("source") == long_source[:coordination._OWNER_FIELD_MAX_CHARS],  # type: ignore[attr-defined]
        "single lock conflict truncates long holder source",
    )
    check(owner.get("app_session_id") == "session-a", "single lock conflict reports holder session")
    check("ignored" not in owner, "single lock conflict omits unsupported owner fields")
    check("holder_token" not in holder and "holder_token" not in result, "single lock conflict does not expose holder token")

    await coordination.lock_ops(key="file-a", release=True, holder_token=str(blocker["holder_token"]))
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


async def test_better_agent_runner_lock_ops_handler_defaults_provider_id() -> None:
    import extension_store

    captured_payloads: list[dict] = []

    def fake_post_loopback_sync(payload: dict, **kwargs) -> dict:
        captured_payloads.append(payload)
        return {
            "success": True,
            "key": payload.get("key"),
            "holder_token": "handler-token",
            "expires_in_seconds": 30,
        }

    original_post = runner_better_agent._post_loopback_sync
    original_ready = extension_store.is_extension_runtime_ready
    try:
        runner_better_agent._post_loopback_sync = fake_post_loopback_sync
        extension_store.is_extension_runtime_ready = lambda extension_id: (
            extension_id == extension_store.BUILTIN_COORDINATION_EXTENSION_ID
        )
        registry = LockRegistry()
        handlers = runner_better_agent._build_loopback_tool_handlers(
            {
                "backend_url": "http://127.0.0.1:1",
                "internal_token": "token",
                "app_session_id": "session-a",
            },
            cwd="/repo",
            model="model-a",
            lock_registry=registry,
        )
        file_path = Path("/tmp/better-agent-handler-lock-test.txt")
        single_text = await handlers["lock_ops"]({
            "arguments": {"key": f"file_edit:{file_path}"},
        })
        multi_text = await handlers["lock_ops"]({
            "arguments": {"keys": ["git_ops:/repo", f"file_edit:{file_path}"]},
        })
        provided_provider_handlers = runner_better_agent._build_loopback_tool_handlers(
            {
                "backend_url": "http://127.0.0.1:1",
                "internal_token": "token",
                "app_session_id": "session-a",
                "provider_id": "provider-a",
            },
            cwd="/repo",
            model="model-a",
            lock_registry=LockRegistry(),
        )
        provider_text = await provided_provider_handlers["lock_ops"]({
            "arguments": {"key": "git_ops:/repo"},
        })
    finally:
        runner_better_agent._post_loopback_sync = original_post
        extension_store.is_extension_runtime_ready = original_ready

    check(
        "name 'provider_id' is not defined" not in single_text,
        "runner lock_ops single-key handler does not NameError when provider_id is absent",
    )
    check(
        "name 'provider_id' is not defined" not in multi_text,
        "runner lock_ops multi-key handler does not NameError when provider_id is absent",
    )
    check(
        "name 'provider_id' is not defined" not in provider_text,
        "runner lock_ops handler does not NameError when provider_id is supplied",
    )
    absent_provider_payloads = captured_payloads[:2]
    check(
        all(((payload.get("owner") or {}).get("provider_id") == "") for payload in absent_provider_payloads),
        "runner lock_ops handler defaults missing provider_id owner metadata to empty string",
    )
    provided_owner = (captured_payloads[-1].get("owner") or {}) if captured_payloads else {}
    check(
        provided_owner.get("provider_id") == "provider-a",
        "runner lock_ops handler propagates run provider_id into owner metadata when supplied",
    )
    check(
        registry.error_for_write(file_path) is None,
        "runner lock_ops handler records successful file_edit locks for Write/Edit gating",
    )


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
    await test_single_lock_conflict_reports_holder_metadata_without_token()
    await test_multi_release_is_atomic()
    await test_better_agent_runner_lock_ops_handler_defaults_provider_id()
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
