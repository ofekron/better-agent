from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import _test_home
_TMP = _test_home.isolate("bc-coordination-locks-")

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
    coordination._clear_for_tests()  # type: ignore[attr-defined]
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
    check(result.get("waited_keys") == ["file-b"], "multi lock reports the precise waited key")
    check(result.get("blocked_keys") == ["file-b"], "multi lock returns blocked_keys for compatibility with reread scoping")
    check(0 < int(result.get("expires_in_seconds") or 0) <= coordination._DEFAULT_LOCK_LEASE_SECONDS, "multi lock reports remaining lease, not a fresh constant")
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
    coordination._clear_for_tests()  # type: ignore[attr-defined]


async def test_immediate_acquire_reports_no_wait() -> None:
    coordination._clear_for_tests()  # type: ignore[attr-defined]

    single = await coordination.lock_ops(key="file-a")
    check(single.get("success") is True, "uncontended single-key acquire succeeds")
    check(single.get("waited") is False, "uncontended single-key acquire reports waited=false")
    check(single.get("waited_seconds") == 0.0, "uncontended single-key acquire reports zero waited_seconds")
    await coordination.lock_ops(key="file-a", release=True, holder_token=str(single["holder_token"]))

    multi = await coordination.lock_ops(keys=["file-a", "file-b"], key="", timeout_seconds=1)
    check(multi.get("success") is True, "uncontended multi-key acquire succeeds")
    check(multi.get("waited") is False, "uncontended multi-key acquire reports waited=false")
    check(multi.get("waited_keys") == [], "uncontended multi-key acquire reports no waited_keys")
    await coordination.lock_ops(
        key="", keys=multi["keys"], release=True, holder_token=str(multi["holder_token"])
    )
    coordination._clear_for_tests()  # type: ignore[attr-defined]


async def test_multi_lock_timeout_releases_partial_locks() -> None:
    coordination._clear_for_tests()  # type: ignore[attr-defined]
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
    check(result.get("blocked_keys") == ["file-b"], "multi lock timeout reports precise blocked key")

    await coordination.lock_ops(key="file-b", release=True, holder_token=str(blocker["holder_token"]))
    coordination._clear_for_tests()  # type: ignore[attr-defined]


async def test_single_lock_conflict_reports_holder_metadata_without_token() -> None:
    coordination._clear_for_tests()  # type: ignore[attr-defined]
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
    check(isinstance(holder.get("created_at"), float), "single lock conflict reports holder created_at metadata")
    check(isinstance(holder.get("renewed_at"), float), "single lock conflict reports holder renewed_at metadata")
    check("holder_token" not in holder and "holder_token" not in result, "single lock conflict does not expose holder token")

    await coordination.lock_ops(key="file-a", release=True, holder_token=str(blocker["holder_token"]))
    coordination._clear_for_tests()  # type: ignore[attr-defined]


async def test_multi_release_is_atomic() -> None:
    coordination._clear_for_tests()  # type: ignore[attr-defined]
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


async def test_non_lease_ops_ignore_invalid_lease_seconds() -> None:
    coordination._clear_for_tests()  # type: ignore[attr-defined]
    acquired = await coordination.lock_ops(key="file-a")
    token = str(acquired["holder_token"])
    validated = await coordination.lock_ops(
        key="file-a", op="validate", holder_token=token, lease_seconds="not-a-number"
    )
    check(validated.get("success") is True, "validate ignores irrelevant invalid lease_seconds")
    released = await coordination.lock_ops(
        key="file-a", release=True, holder_token=token, lease_seconds="not-a-number"
    )
    check(released.get("success") is True, "release ignores irrelevant invalid lease_seconds")
    coordination._clear_for_tests()  # type: ignore[attr-defined]


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


async def test_renew_validate_and_reattach_by_trusted_owner() -> None:
    coordination._clear_for_tests()  # type: ignore[attr-defined]
    owner = {"principal_extension_id": "core", "app_session_id": "session-a", "cwd": "/repo", "provider_id": "p"}
    other = {"principal_extension_id": "core", "app_session_id": "session-b", "cwd": "/repo", "provider_id": "p"}
    acquired = await coordination.lock_ops(key="file-a", owner=owner, lease_seconds=5)
    token = str(acquired["holder_token"])

    validated = await coordination.lock_ops(key="file-a", op="validate", holder_token=token)
    check(validated.get("success") is True, "validate accepts a live holder token")

    before_renewed_at = float(coordination._locks["file-a"].get("renewed_at") or 0)
    await asyncio.sleep(0.001)
    renewed = await coordination.lock_ops(key="file-a", op="renew", holder_token=token, lease_seconds=30)
    check(renewed.get("success") is True, "renew accepts a live holder token")
    check(20 <= int(renewed.get("expires_in_seconds") or 0) <= 30, "renew reports the renewed remaining lease")
    check(float(coordination._locks["file-a"].get("renewed_at") or 0) > before_renewed_at, "renew stamps renewed_at metadata")

    reattached = await coordination.lock_ops(key="file-a", op="reattach", owner=owner)
    check(reattached.get("success") is True and reattached.get("holder_token") == token, "same trusted owner can reattach to its live lock")

    denied = await coordination.lock_ops(key="file-a", op="reattach", owner=other)
    check(denied.get("success") is False and denied.get("error") == "locked", "different trusted owner cannot reattach")

    listed = await coordination.lock_ops(key="", op="list_owned", owner=owner)
    check(listed.get("keys") == ["file-a"], "list_owned returns locks for the trusted owner")

    released = await coordination.lock_ops(key="file-a", op="release_owned", owner=owner)
    check(released.get("success") is True and released.get("released") is True, "release_owned releases same-owner locks without holder token")
    coordination._clear_for_tests()  # type: ignore[attr-defined]


async def test_multi_key_success_reports_remaining_ttl_after_wait() -> None:
    coordination._clear_for_tests()  # type: ignore[attr-defined]
    blocker = await coordination.lock_ops(key="file-b")

    async def release_blocker() -> None:
        await asyncio.sleep(0.2)
        await coordination.lock_ops(key="file-b", release=True, holder_token=str(blocker["holder_token"]))

    acquire_task = asyncio.create_task(
        coordination.lock_ops(keys=["file-a", "file-b"], key="", timeout_seconds=1, lease_seconds=5)
    )
    release_task = asyncio.create_task(release_blocker())
    result = await acquire_task
    await release_task

    check(result.get("success") is True and result.get("waited") is True, "multi-key acquire waits in TTL regression")
    check(result.get("waited_keys") == ["file-b"], "multi-key TTL regression records waited key")
    check(0 < int(result.get("expires_in_seconds") or 0) < 5, "multi-key acquire reports remaining TTL after wait")
    await coordination.lock_ops(key="", keys=result["keys"], release=True, holder_token=str(result["holder_token"]))
    coordination._clear_for_tests()  # type: ignore[attr-defined]


async def test_multi_key_reacquires_partial_locks_that_expire_while_waiting() -> None:
    coordination._clear_for_tests()  # type: ignore[attr-defined]
    now = {"value": 100.0}
    original_now = coordination._now  # type: ignore[attr-defined]
    coordination._now = lambda: now["value"]  # type: ignore[attr-defined]
    try:
        blocker = await coordination.lock_ops(key="file-b", lease_seconds=30)

        async def release_after_partial_expiry() -> None:
            await asyncio.sleep(0.03)
            now["value"] += 6.0
            await coordination.lock_ops(key="file-b", release=True, holder_token=str(blocker["holder_token"]))

        acquire_task = asyncio.create_task(
            coordination.lock_ops(keys=["file-a", "file-b"], key="", timeout_seconds=20, lease_seconds=5)
        )
        release_task = asyncio.create_task(release_after_partial_expiry())
        result = await acquire_task
        await release_task

        check(result.get("success") is True, "multi-key acquire succeeds after partial lock expires while waiting")
        check(
            all(
                coordination._locks.get(key, {}).get("holder_token") == result.get("holder_token")
                for key in result.get("keys", [])
            ),
            "multi-key acquire returns success only when every requested key is durably held",
        )
        check(
            all(
                float(coordination._locks.get(key, {}).get("expires_at") or 0) > now["value"]
                for key in result.get("keys", [])
            ),
            "multi-key reacquired locks have live expiry after partial expiry",
        )
        await coordination.lock_ops(key="", keys=result["keys"], release=True, holder_token=str(result["holder_token"]))
    finally:
        coordination._now = original_now  # type: ignore[attr-defined]
        coordination._clear_for_tests()  # type: ignore[attr-defined]


async def test_lock_survives_in_memory_registry_loss() -> None:
    coordination._clear_for_tests()  # type: ignore[attr-defined]
    acquired = await coordination.lock_ops(key="file-restart", lease_seconds=30)
    token = str(acquired["holder_token"])

    coordination._drop_memory_for_tests()  # type: ignore[attr-defined]
    blocked = await coordination.lock_ops(key="file-restart")
    check(
        blocked.get("success") is False and blocked.get("error") == "locked",
        "lock_ops preserves live locks when in-memory registry is lost",
    )

    coordination._drop_memory_for_tests()  # type: ignore[attr-defined]
    validated = await coordination.lock_ops(key="file-restart", op="validate", holder_token=token)
    check(validated.get("success") is True, "lock_ops validates a durable lock after memory reset")
    await coordination.lock_ops(key="file-restart", release=True, holder_token=token)
    coordination._clear_for_tests()  # type: ignore[attr-defined]


async def test_lock_blocks_competing_process() -> None:
    coordination._clear_for_tests()  # type: ignore[attr-defined]
    acquired = await coordination.lock_ops(key="file-cross-process", lease_seconds=30)
    token = str(acquired["holder_token"])
    root = Path(__file__).resolve().parents[2]
    code = """
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "backend"))
import coordination

async def main():
    result = await coordination.lock_ops(key="file-cross-process")
    print(json.dumps(result))

asyncio.run(main())
"""
    child = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    check(child.returncode == 0, "competing process can call lock_ops against the same home")
    result = json.loads(child.stdout.strip().splitlines()[-1])
    check(
        result.get("success") is False and result.get("error") == "locked",
        "lock_ops blocks a competing process while durable lock is live",
    )
    await coordination.lock_ops(key="file-cross-process", release=True, holder_token=token)
    coordination._clear_for_tests()  # type: ignore[attr-defined]


async def test_runner_write_gate_validates_backend_liveness() -> None:
    target = Path("/tmp/better-agent-lock-test.txt")
    key = f"file_edit:{target}"
    registry = LockRegistry()
    registry.record_lock_result({
        "success": True,
        "keys": [key],
        "holder_token": "stale-token",
        "expires_in_seconds": 30,
    })

    original_post = runner_better_agent._post_loopback_sync

    def fake_not_locked(payload: dict, **kwargs) -> dict:
        check(payload.get("op") == "validate", "write gate asks backend to validate the local token")
        return {"success": False, "error": "not_locked", "key": payload.get("key")}

    try:
        runner_better_agent._post_loopback_sync = fake_not_locked
        err = await runner_better_agent._validate_backend_file_lock(
            backend_url="http://backend",
            internal_token="tok",
            app_session_id="session-a",
            cwd=Path("/tmp"),
            key=key,
            token=registry.token_for_key(key),
            lock_registry=registry,
        )
    finally:
        runner_better_agent._post_loopback_sync = original_post

    check(err is not None and "not live in the backend" in err, "write gate blocks when backend no longer has the local lock")
    check(registry.token_for_key(key) == "", "write gate drops stale local token after backend validation fails")


async def test_runner_write_gate_reattaches_same_owner_lock() -> None:
    target = Path("/tmp/better-agent-reattach-test.txt")
    key = f"file_edit:{target}"
    registry = LockRegistry()
    calls: list[dict] = []
    original_post = runner_better_agent._post_loopback_sync

    def fake_reattach(payload: dict, **kwargs) -> dict:
        calls.append(payload)
        if payload.get("op") == "validate":
            return {"success": False, "error": "holder_token_required", "key": payload.get("key")}
        if payload.get("op") == "reattach":
            return {
                "success": True,
                "key": payload.get("key"),
                "keys": [payload.get("key")],
                "holder_token": "reattached-token",
                "expires_in_seconds": 30,
            }
        return {"success": False, "error": "unexpected_op"}

    try:
        runner_better_agent._post_loopback_sync = fake_reattach
        err = await runner_better_agent._validate_backend_file_lock(
            backend_url="http://backend",
            internal_token="tok",
            app_session_id="session-a",
            cwd=Path("/tmp"),
            key=key,
            token="",
            lock_registry=registry,
        )
    finally:
        runner_better_agent._post_loopback_sync = original_post

    check(err is None, "write gate allows same-owner backend reattach")
    check([call.get("op") for call in calls] == ["validate", "reattach"], "write gate validates before reattaching")
    check(registry.token_for_key(key) == "reattached-token", "write gate records the reattached token")


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
    await test_non_lease_ops_ignore_invalid_lease_seconds()
    await test_better_agent_runner_lock_ops_handler_defaults_provider_id()
    await test_renew_validate_and_reattach_by_trusted_owner()
    await test_multi_key_success_reports_remaining_ttl_after_wait()
    await test_multi_key_reacquires_partial_locks_that_expire_while_waiting()
    await test_lock_survives_in_memory_registry_loss()
    await test_lock_blocks_competing_process()
    await test_runner_write_gate_validates_backend_liveness()
    await test_runner_write_gate_reattaches_same_owner_lock()
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
