from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from fastapi import Header, Request

import _test_home

HOME = Path(_test_home.isolate("ba-test-scheduler-hot-"))
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import extension_api
import extension_store
import extension_token_registry
import main
import orchestrator
from bounded_async_executor import AdmissionOverloaded, BoundedAsyncExecutor


def _record(extension_id: str, *roles: str) -> dict:
    return {
        "manifest": {"id": extension_id, "core_roles": list(roles)},
        "enabled": True,
        "entitlement": {"status": "not_required"},
    }


def _write_store(home: Path, extensions: dict) -> None:
    path = home / "extensions" / "extensions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": extension_store.STORE_SCHEMA_VERSION,
        "extensions": extensions,
        "deleted_extensions": {},
    }), encoding="utf-8")


def test_role_projection_warm_and_invalidates() -> None:
    _write_store(HOME, {"one": _record("one", "scheduler")})
    extension_store._clear_projection_cache()
    assert extension_store.core_role_owners()["scheduler"] == "one"
    original = extension_store._read_store_unlocked
    extension_store._read_store_unlocked = lambda: (_ for _ in ()).throw(
        AssertionError("warm role lookup reread the store")
    )
    try:
        assert extension_store.extension_id_for_role("scheduler") == "one"
        assert extension_store.core_role_owners()["scheduler"] == "one"
    finally:
        extension_store._read_store_unlocked = original

    store_path = extension_store._store_path()
    original_stat = store_path.stat()
    _write_store(HOME, {"two": _record("two", "scheduler")})
    assert store_path.stat().st_size == original_stat.st_size
    os.utime(store_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    time.sleep(extension_store._STORE_FINGERPRINT_TTL_SECONDS + 0.02)
    assert extension_store.extension_id_for_role("scheduler") == "two"

    other = Path(tempfile.mkdtemp(prefix="ba-role-home-"))
    try:
        _write_store(other, {"home": _record("home", "scheduler")})
        os.environ["BETTER_AGENT_HOME"] = str(other)
        assert extension_store.extension_id_for_role("scheduler") == "home"
    finally:
        os.environ["BETTER_AGENT_HOME"] = str(HOME)
        shutil.rmtree(other, ignore_errors=True)

    _write_store(HOME, {
        "a": _record("a", "scheduler"),
        "b": _record("b", "scheduler"),
    })
    extension_store._clear_projection_cache()
    try:
        extension_store.core_role_owners()
    except extension_store.ExtensionError:
        pass
    else:
        raise AssertionError("duplicate role owners must fail closed")


async def test_scheduler_executor_isolated_and_bounded() -> None:
    entered = threading.Event()
    release = threading.Event()
    original = extension_api._scheduler_session_snapshot

    def blocked(_sid: str):
        entered.set()
        release.wait(2)
        return True, []

    extension_api._scheduler_session_snapshot = blocked
    try:
        tasks = [asyncio.create_task(extension_api._run_scheduler_read(str(i))) for i in range(10)]
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(0.15)
        rejected = [task for task in tasks if task.done() and isinstance(task.exception(), AdmissionOverloaded)]
        assert len(rejected) == 2, len(rejected)
        ticks = 0
        deadline = time.perf_counter() + 0.05
        while time.perf_counter() < deadline:
            ticks += 1
            await asyncio.sleep(0)
        assert ticks > 10
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(isinstance(item, AdmissionOverloaded) for item in results) == 2
        assert sum(item == (True, []) for item in results) == 8
    finally:
        release.set()
        extension_api._scheduler_session_snapshot = original


async def test_multi_role_dispatch_is_path_specific() -> None:
    owner = "multi.owner"
    original_roles = extension_store.core_role_owners
    original_enabled = extension_store.is_extension_enabled_cached
    original_scheduler = extension_api._dispatch_scheduler_core_backend
    original_routines = extension_api._dispatch_routines_core_backend
    calls: list[str] = []

    async def scheduler(path, _request):
        calls.append("scheduler")
        return "scheduler-response" if path.endswith("/schedules") else None

    async def routines(path, _request):
        calls.append("routines")
        return "routines-response" if path == "routines" else None

    extension_store.core_role_owners = lambda: {
        "scheduler": owner,
        "routines": owner,
    }
    extension_store.is_extension_enabled_cached = lambda _extension_id: True
    extension_api._dispatch_scheduler_core_backend = scheduler
    extension_api._dispatch_routines_core_backend = routines
    request = type("Request", (), {"method": "GET"})()
    try:
        assert await extension_api._dispatch_core_builtin_backend(
            owner, "routines", request,
        ) == "routines-response"
        assert calls == ["scheduler", "routines"]
        calls.clear()
        assert await extension_api._dispatch_core_builtin_backend(
            owner, "sessions/sid/schedules", request,
        ) == "scheduler-response"
        assert calls == ["scheduler"]
    finally:
        extension_store.core_role_owners = original_roles
        extension_store.is_extension_enabled_cached = original_enabled
        extension_api._dispatch_scheduler_core_backend = original_scheduler
        extension_api._dispatch_routines_core_backend = original_routines


async def test_extension_content_reads_are_off_loop() -> None:
    _write_store(HOME, {"slow.owner": _record("slow.owner", "scheduler")})
    extension_store._clear_projection_cache()
    original_read_bytes = Path.read_bytes

    def delayed_read_bytes(self, *args, **kwargs):
        if self == extension_store._store_path():
            time.sleep(0.05)
        return original_read_bytes(self, *args, **kwargs)

    async def responsive(awaitable):
        task = asyncio.create_task(awaitable)
        ticks = 0
        while not task.done():
            ticks += 1
            await asyncio.sleep(0)
        assert ticks > 10
        return await task

    Path.read_bytes = delayed_read_bytes
    try:
        extension_store._STORE_FINGERPRINT_CACHE = None
        await responsive(extension_api.list_extensions(False))
        extension_store._STORE_FINGERPRINT_CACHE = None
        await responsive(extension_api._backend_entrypoint_spec_async("slow.owner"))
        extension_store._STORE_FINGERPRINT_CACHE = None
        extension_store._clear_projection_cache()
        request = type("Request", (), {"method": "GET"})()
        assert await responsive(extension_api._dispatch_core_builtin_backend(
            "slow.owner", "not-a-scheduler-path", request,
        )) is None
    finally:
        Path.read_bytes = original_read_bytes


async def test_project_gates_load_roles_off_loop() -> None:
    entered = threading.Event()
    original_roles = extension_store.core_role_owners
    original_runtime = main._require_builtin_runtime_extension
    original_builtin = main._require_builtin_extension

    def delayed_roles():
        entered.set()
        time.sleep(0.05)
        return {"project-structure": "project.owner"}

    extension_store.core_role_owners = delayed_roles
    main._require_builtin_runtime_extension = lambda _owner: None
    main._require_builtin_extension = lambda _owner: None
    try:
        for gate in (
            main._require_project_structure_internal_async,
            main._require_project_updates_internal_async,
        ):
            state = type("State", (), {
                "internal_token": "token",
                "internal_principal": orchestrator.PrincipalAuthority(
                    "extension", "project.owner",
                ),
            })()
            request = type("Request", (), {"state": state})()
            with main.coordinator.bind_principal(
                "token",
                state.internal_principal,
                allow_downstream=True,
            ):
                task = asyncio.create_task(gate(request, "token"))
            assert await asyncio.to_thread(entered.wait, 1)
            ticks = 0
            while not task.done():
                ticks += 1
                await asyncio.sleep(0)
            assert ticks > 10
            await task
            entered.clear()
    finally:
        extension_store.core_role_owners = original_roles
        main._require_builtin_runtime_extension = original_runtime
        main._require_builtin_extension = original_builtin


def test_scheduler_snapshot_validates_loaded_identity() -> None:
    import session_manager
    from stores import schedule_store

    original_get = session_manager.manager.get
    original_list = schedule_store.list_for_session
    listed: list[str] = []
    schedule_store.list_for_session = lambda sid: listed.append(sid) or []
    try:
        session_manager.manager.get = lambda _sid: {"id": "replacement-sid"}
        assert extension_api._scheduler_session_snapshot("old-session-id") == (False, [])
        assert listed == []
        assert extension_api._scheduler_session_snapshot("replacement-sid") == (True, [])
        assert listed == ["replacement-sid"]
    finally:
        session_manager.manager.get = original_get
        schedule_store.list_for_session = original_list


def test_real_middleware_request_authority_boundary() -> None:
    from fastapi.testclient import TestClient

    token = extension_token_registry.mint("project.owner")
    original_allowed = main.coordinator.extension_internal_loopback_allowed_async
    original_role = extension_store.extension_id_for_role
    original_builtin = main._require_builtin_extension

    async def allowed(_extension_id):
        return True

    async def sync_gate_endpoint(
        request: Request,
        x_internal_token: str = Header(..., alias="X-Internal-Token"),
    ):
        original_sync_resolve = main.coordinator.resolve_principal
        main.coordinator.resolve_principal = lambda _token: (_ for _ in ()).throw(
            AssertionError("sync gate fell back to token/disk authorization")
        )
        try:
            main._require_project_updates_internal(request, x_internal_token)
            return {"ok": True}
        finally:
            main.coordinator.resolve_principal = original_sync_resolve

    async def grandchild_endpoint(
        request: Request,
        x_internal_token: str = Header(..., alias="X-Internal-Token"),
    ):
        assert main.coordinator.request_principal(request, x_internal_token) is not None
        extension_token_registry.revoke("project.owner")

        async def child():
            return await main.coordinator.request_principal_async(request, x_internal_token)

        return {"authorized": await asyncio.create_task(child()) is not None}

    main.app.add_api_route(
        "/api/internal/test-request-sync-authority",
        sync_gate_endpoint,
        methods=["GET"],
    )
    main.app.add_api_route(
        "/api/internal/test-request-grandchild-authority",
        grandchild_endpoint,
        methods=["GET"],
    )
    test_paths = {
        "/api/internal/test-request-sync-authority",
        "/api/internal/test-request-grandchild-authority",
    }
    test_routes = [
        route for route in main.app.router.routes
        if getattr(route, "path", None) in test_paths
    ]
    main.app.router.routes[:] = [
        route for route in main.app.router.routes if route not in test_routes
    ]
    main.app.router.routes[0:0] = test_routes
    main.coordinator.extension_internal_loopback_allowed_async = allowed
    extension_store.extension_id_for_role = lambda role: (
        "project.owner" if role == "project-structure" else original_role(role)
    )
    main._require_builtin_extension = lambda _owner: None
    try:
        client = TestClient(main.app)
        try:
            headers = {"X-Internal-Token": token}
            response = client.get(
                "/api/internal/test-request-sync-authority",
                headers=headers,
            )
            assert response.status_code == 200, response.text
            response = client.get(
                "/api/internal/test-request-grandchild-authority",
                headers=headers,
            )
            assert response.status_code == 200, response.text
            assert response.json() == {"authorized": False}
        finally:
            client.close()
    finally:
        main.coordinator.extension_internal_loopback_allowed_async = original_allowed
        extension_store.extension_id_for_role = original_role
        main._require_builtin_extension = original_builtin


def test_internal_endpoints_have_no_legacy_sync_auth_calls() -> None:
    source = (BACKEND / "main.py").read_text(encoding="utf-8")
    for legacy in (
        "coordinator.is_internal_caller(",
        "coordinator.principal_extension_id(",
        "coordinator.resolve_principal(",
    ):
        assert legacy not in source


async def test_token_auth_freshness_and_loop_responsiveness() -> None:
    coordinator = object.__new__(orchestrator.Coordinator)
    coordinator.internal_token = "core-current"
    coordinator._prev_token = None
    coordinator._prev_token_grace_expires_at = 0.0

    extension_token = extension_token_registry.mint("ext.one")
    assert await coordinator.resolve_principal_async(extension_token) == ("extension", "ext.one")
    extension_token_registry.revoke("ext.one")
    assert await coordinator.resolve_principal_async(extension_token) is None

    token_path = orchestrator._internal_token_path()
    token_path.write_text("core-current", encoding="utf-8")
    assert await coordinator.resolve_principal_async("core-current") == ("core", None)
    token_stat = token_path.stat()
    token_path.write_text("core-rotated", encoding="utf-8")
    assert token_path.stat().st_size == token_stat.st_size
    os.utime(token_path, ns=(token_stat.st_atime_ns, token_stat.st_mtime_ns))
    assert await coordinator.resolve_principal_async("core-current") is None
    assert await coordinator.resolve_principal_async("core-rotated") == ("core", None)

    original_read_text = Path.read_text

    def delayed_read_text(self, *args, **kwargs):
        if self in {token_path, extension_token_registry._path()}:
            time.sleep(0.05)
        return original_read_text(self, *args, **kwargs)

    Path.read_text = delayed_read_text
    try:
        task = asyncio.create_task(coordinator.resolve_principal_async("invalid"))
        ticks = 0
        while not task.done():
            ticks += 1
            await asyncio.sleep(0)
        assert ticks > 10
        assert await task is None
    finally:
        Path.read_text = original_read_text

    coordinator.rotate_internal_token(grace_seconds=60)
    assert await coordinator.resolve_principal_async("core-current") == ("core", None)
    assert await coordinator.resolve_principal_async(coordinator.internal_token) == ("core", None)

    authority = orchestrator.PrincipalAuthority("extension", "ext.bound")
    original_resolve_fresh = extension_token_registry.resolve_fresh
    extension_token_registry.resolve_fresh = lambda _token: (_ for _ in ()).throw(
        AssertionError("bound request authority reread token registry")
    )
    try:
        with coordinator.bind_principal("bound-token", authority):
            assert coordinator.resolve_principal("bound-token") == authority
            assert await coordinator.resolve_principal_async("bound-token") == authority
            assert coordinator.is_internal_caller("bound-token")
            assert coordinator.principal_extension_id("bound-token") == "ext.bound"
    finally:
        extension_token_registry.resolve_fresh = original_resolve_fresh

    inherited_token = extension_token_registry.mint("ext.inherited")
    inherited_authority = orchestrator.PrincipalAuthority("extension", "ext.inherited")
    continue_child = asyncio.Event()

    async def inherited_child():
        await continue_child.wait()
        return await coordinator.resolve_principal_async(inherited_token)

    with coordinator.bind_principal(inherited_token, inherited_authority):
        child = asyncio.create_task(inherited_child())
        assert coordinator.resolve_principal(inherited_token) == inherited_authority
    extension_token_registry.revoke("ext.inherited")
    continue_child.set()
    assert await child is None


async def test_auth_executor_is_bounded() -> None:
    coordinator = object.__new__(orchestrator.Coordinator)
    coordinator.internal_token = "unused"
    coordinator._prev_token = None
    coordinator._prev_token_grace_expires_at = 0.0
    entered = threading.Event()
    release = threading.Event()
    original = extension_token_registry.resolve_fresh

    def blocked(_token: str):
        entered.set()
        release.wait(2)
        return None

    extension_token_registry.resolve_fresh = blocked
    try:
        tasks = [asyncio.create_task(coordinator.resolve_principal_async(f"invalid-{i}")) for i in range(18)]
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(0.15)
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(isinstance(item, AdmissionOverloaded) for item in results) == 2
        assert sum(item is None for item in results) == 16
    finally:
        release.set()
        extension_token_registry.resolve_fresh = original


def test_token_preserved_metadata_mutation() -> None:
    token = extension_token_registry.mint("ext.metadata")
    path = extension_token_registry._path()
    data = json.loads(path.read_text(encoding="utf-8"))
    before = path.stat()
    replacement = "z" * len(token)
    data["ext.metadata"] = replacement
    path.write_text(json.dumps(data), encoding="utf-8")
    assert path.stat().st_size == before.st_size
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert extension_token_registry.resolve_fresh(token) is None
    assert extension_token_registry.resolve_fresh(replacement) == "ext.metadata"


def test_executor_is_cross_loop_safe() -> None:
    executor = BoundedAsyncExecutor(
        name="test.cross_loop",
        max_workers=1,
        capacity=1,
        timeout_seconds=0.1,
    )
    assert asyncio.run(executor.run(lambda: 1)) == 1
    assert asyncio.run(executor.run(lambda: 2)) == 2
    asyncio.run(executor.shutdown())
    try:
        asyncio.run(executor.run(lambda: 3))
    except RuntimeError:
        pass
    else:
        raise AssertionError("shutdown executor accepted new work")


def test_executor_waiter_lifecycle() -> None:
    async def cancellation_case() -> None:
        executor = BoundedAsyncExecutor(
            name="test.cancel_waiter", max_workers=1, capacity=1, timeout_seconds=1,
        )
        entered = threading.Event()
        release = threading.Event()
        running = asyncio.create_task(executor.run(lambda: entered.set() or release.wait(2)))
        assert await asyncio.to_thread(entered.wait, 1)
        waiter = asyncio.create_task(executor.run(lambda: "unexpected"))
        await asyncio.sleep(0)
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        release.set()
        await running
        assert executor.depth() == 0
        await executor.shutdown()

    async def shutdown_waiter_case() -> None:
        executor = BoundedAsyncExecutor(
            name="test.shutdown_waiter", max_workers=1, capacity=1, timeout_seconds=1,
        )
        entered = threading.Event()
        release = threading.Event()
        running = asyncio.create_task(executor.run(lambda: entered.set() or release.wait(2)))
        assert await asyncio.to_thread(entered.wait, 1)
        waiter = asyncio.create_task(executor.run(lambda: "unexpected"))
        await asyncio.sleep(0)
        shutdown = asyncio.create_task(executor.shutdown())
        result = await asyncio.gather(waiter, return_exceptions=True)
        assert isinstance(result[0], RuntimeError)
        release.set()
        await running
        await shutdown

    asyncio.run(cancellation_case())
    asyncio.run(shutdown_waiter_case())


def test_executor_cross_loop_waiter() -> None:
    executor = BoundedAsyncExecutor(
        name="test.cross_loop_waiter",
        max_workers=1,
        capacity=1,
        timeout_seconds=1,
    )
    entered = threading.Event()
    release = threading.Event()
    holder_result: list[object] = []

    def holder_loop() -> None:
        holder_result.append(asyncio.run(executor.run(
            lambda: entered.set() or release.wait(2) or "holder",
        )))

    thread = threading.Thread(target=holder_loop)
    thread.start()
    assert entered.wait(1)

    async def waiter_loop() -> None:
        waiter = asyncio.create_task(executor.run(lambda: "waiter"))
        await asyncio.sleep(0)
        release.set()
        assert await waiter == "waiter"

    asyncio.run(waiter_loop())
    thread.join(2)
    assert not thread.is_alive()
    assert holder_result == [True]
    asyncio.run(executor.shutdown())


def test_concurrent_mint_single_owner() -> None:
    values: list[str] = []
    threads = [threading.Thread(target=lambda: values.append(extension_token_registry.mint("ext.concurrent"))) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(values)) == 1


if __name__ == "__main__":
    try:
        test_role_projection_warm_and_invalidates()
        asyncio.run(test_scheduler_executor_isolated_and_bounded())
        asyncio.run(test_multi_role_dispatch_is_path_specific())
        asyncio.run(test_extension_content_reads_are_off_loop())
        asyncio.run(test_project_gates_load_roles_off_loop())
        test_scheduler_snapshot_validates_loaded_identity()
        asyncio.run(test_token_auth_freshness_and_loop_responsiveness())
        asyncio.run(test_auth_executor_is_bounded())
        test_token_preserved_metadata_mutation()
        test_executor_is_cross_loop_safe()
        test_executor_waiter_lifecycle()
        test_executor_cross_loop_waiter()
        test_concurrent_mint_single_owner()
        test_real_middleware_request_authority_boundary()
        test_internal_endpoints_have_no_legacy_sync_auth_calls()
        print("PASS scheduler/extension hot paths")
    finally:
        asyncio.run(extension_api.shutdown_hot_path_executors())
        asyncio.run(orchestrator.shutdown_auth_executor())
        shutil.rmtree(HOME, ignore_errors=True)
