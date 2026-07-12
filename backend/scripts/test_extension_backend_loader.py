#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from starlette.requests import ClientDisconnect

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-test-extension-backend-"))
import _test_home
_test_home.isolate("ba-test-")
os.environ["BETTER_CLAUDE_INTERNAL_TOKEN"] = "test-extension-backend-token"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import extension_api  # noqa: E402
import extension_backend_loader  # noqa: E402
import extension_store  # noqa: E402
import project_update_store  # noqa: E402
from paths import encode_cwd  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


class _DisconnectingRequest:
    async def stream(self):
        yield b"partial"
        raise ClientDisconnect()


def _assert_client_closed(exc: Exception, message: str) -> None:
    check(
        getattr(exc, "status_code", None) == extension_backend_loader._CLIENT_CLOSED_REQUEST_STATUS,  # type: ignore[attr-defined]
        message,
    )


def _configure_internal_llm_defaults() -> None:
    import config_store

    provider = config_store.list_providers()["providers"][0]
    config_store.set_internal_llm_assignments({
        "default_session": {
            "provider_id": provider["id"],
            "model": provider["default_model"],
            "reasoning_effort": provider.get("default_reasoning_effort") or "",
        }
    })


def _seed_extension() -> Path:
    package = TMP_HOME / "package"
    (package / "backend").mkdir(parents=True)
    (package / "ui").mkdir(parents=True)
    (package / "backend" / "routes.py").write_text(
        "\n".join(
            [
                "import os",
                "import asyncio",
                "import importlib.util",
                "import sys",
                "import threading",
                "import time",
                "from fastapi import APIRouter, Request",
                "",
                "_sentinel_ready = 0",
                "_sentinel_gate = asyncio.Event()",
                "",
                "def create_router(context):",
                "    router = APIRouter()",
                "    @router.api_route('/', methods=['GET'])",
                "    def root():",
                "        return {'root': True}",
                "    @router.get('/ping')",
                "    def ping():",
                "        return {",
                "            'extension_id': context.extension_id,",
                "            'install_path': str(context.install_path),",
                "            'source_repo_url': context.source.get('repo_url'),",
                "            'source_extension_path': context.source.get('extension_path'),",
                "        }",
                "    @router.get('/headers')",
                "    def headers(request: Request):",
                "        return {",
                "            'authorization': request.headers.get('authorization'),",
                "            'cookie': request.headers.get('cookie'),",
                "            'x_internal_token': request.headers.get('x-internal-token'),",
                "            'accept': request.headers.get('accept'),",
                "        }",
                "    @router.get('/sdk-env')",
                "    def sdk_env():",
                "        return {",
                "            'sdk_importable': importlib.util.find_spec('better_agent_sdk') is not None,",
                "            'core_importable': importlib.util.find_spec('extension_store') is not None,",
                "            'extension_id': os.environ.get('BETTER_CLAUDE_EXTENSION_ID'),",
                "            'backend_url': os.environ.get('BETTER_CLAUDE_BACKEND_URL'),",
                "            'has_internal_token': bool(os.environ.get('BETTER_CLAUDE_INTERNAL_TOKEN')),",
                "            'active_checkout': os.environ.get('BETTER_AGENT_ACTIVE_CHECKOUT'),",
                "        }",
                "    @router.post('/mutate-env')",
                "    def mutate_env():",
                "        os.environ['BA_EXTENSION_MUTATED_PARENT'] = '1'",
                "        return {'mutated_inside_extension_process': os.environ.get('BA_EXTENSION_MUTATED_PARENT')}",
                "    @router.get('/slow')",
                "    def slow():",
                "        (context.install_path / 'slow.pid').write_text(str(os.getpid()), encoding='utf-8')",
                "        time.sleep(10)",
                "        return {'ok': True}",
                "    @router.get('/sleep2')",
                "    def sleep2():",
                "        time.sleep(2)",
                "        return {'ok': True}",
                "    @router.get('/sleep-short')",
                "    def sleep_short():",
                "        time.sleep(0.05)",
                "        return {'ok': True}",
                "    @router.get('/block-loop')",
                "    async def block_loop():",
                "        time.sleep(0.2)",
                "        return {'ok': True}",
                "    @router.get('/background-gil')",
                "    async def background_gil():",
                "        def consume():",
                "            prior = sys.getswitchinterval()",
                "            sys.setswitchinterval(0.15)",
                "            try:",
                "                until = time.monotonic() + 0.35",
                "                while time.monotonic() < until:",
                "                    pass",
                "            finally:",
                "                sys.setswitchinterval(prior)",
                "        worker = threading.Thread(target=consume)",
                "        worker.start()",
                "        await asyncio.sleep(0.3)",
                "        worker.join()",
                "        return {'ok': True}",
                "    async def sentinel_barrier():",
                "        global _sentinel_ready",
                "        _sentinel_ready += 1",
                "        if _sentinel_ready >= 2:",
                "            _sentinel_gate.set()",
                "        await _sentinel_gate.wait()",
                "    @router.get('/sentinel-block')",
                "    async def sentinel_block():",
                "        await sentinel_barrier()",
                "        time.sleep(0.3)",
                "        return {'ok': True}",
                "    @router.get('/sentinel-peer')",
                "    async def sentinel_peer():",
                "        await sentinel_barrier()",
                "        await asyncio.sleep(0.35)",
                "        return {'ok': True}",
                "    @router.post('/echo')",
                "    async def echo(request: Request):",
                "        body = await request.body()",
                "        return {'size': len(body), 'marker': body[:32].decode('ascii')}",
                "    @router.get('/boom')",
                "    def boom():",
                "        raise RuntimeError('secret path /tmp/better-agent-secret')",
                "    return router",
            ]
        ),
        encoding="utf-8",
    )
    (package / "ui" / "index.js").write_text("export const ok = true;\n", encoding="utf-8")
    (package / "backend" / "secret.txt").write_text("secret\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"]["ofek.backend"] = {
        "manifest": {
            "kind": extension_store.MANIFEST_KIND,
            "id": "ofek.backend",
            "name": "Backend",
            "version": "1.0.0",
            "description": "",
            "surfaces": ["backend_feature", "frontend_feature"],
            "entrypoints": {
                "backend": "backend/routes.py",
                "frontend": "ui/index.js",
                "backend_timeouts": {"sleep2": 5},
                "mcp": [],
                "provider_capabilities": [],
            },
            "permissions": {"backend_routes": True, "internal_loopback": True},
            "marketplace": {
                "product_id": "",
                "subscription_required": False,
                "entitlement_url": "",
            },
        },
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/extensions.git",
            "extension_path": "extensions/backend",
            "ref": "",
            "commit_sha": "abc",
            "install_path": str(package),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    extension_store._save(data)  # type: ignore[attr-defined]
    return package


def _seed_module_backend_extension() -> Path:
    package = TMP_HOME / "module-package"
    module_dir = package / "compiled_backend"
    module_dir.mkdir(parents=True)
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "routes.py").write_text(
        "\n".join(
            [
                "from fastapi import APIRouter",
                "",
                "def create_router(context):",
                "    router = APIRouter()",
                "    @router.get('/ping')",
                "    def ping():",
                "        return {",
                "            'extension_id': context.extension_id,",
                "            'install_path': str(context.install_path),",
                "        }",
                "    return router",
            ]
        ),
        encoding="utf-8",
    )
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"]["ofek.compiled-backend"] = {
        "manifest": {
            "kind": extension_store.MANIFEST_KIND,
            "id": "ofek.compiled-backend",
            "name": "Compiled Backend",
            "version": "1.0.0",
            "description": "",
            "surfaces": ["backend_feature"],
            "entrypoints": {
                "backend": "",
                "backend_module": "compiled_backend.routes",
                "frontend": "",
                "mcp": [],
                "provider_capabilities": [],
            },
            "permissions": {"backend_routes": True},
            "marketplace": {
                "product_id": "",
                "subscription_required": False,
                "entitlement_url": "",
            },
        },
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "artifact",
            "repo_url": "https://example.test/compiled.tar.gz",
            "extension_path": "",
            "ref": "",
            "commit_sha": "compiled",
            "install_path": str(package),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    extension_store._save(data)  # type: ignore[attr-defined]
    return package


def _seed_core_builtin_without_backend(*, extension_id: str, core_role: str) -> None:
    manifest = extension_store.validate_manifest({
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": f"{core_role} test fixture",
        "version": "1.0.0",
        "description": "Test-owned core role provider",
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "permissions": {},
        "core_roles": [core_role],
        "marketplace": {},
    })
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_id] = {
        "manifest": manifest,
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "core_builtin",
            "repo_url": "",
            "extension_path": "",
            "ref": "",
            "commit_sha": "core",
            "install_path": str(TMP_HOME),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    extension_store._save(data)  # type: ignore[attr-defined]


def _check_store_rejects_mismatched_manifest_identity() -> None:
    data = extension_store._load()  # type: ignore[attr-defined]
    original_ids = set(data["extensions"])
    record = next(iter(data["extensions"].values())).copy()
    record["manifest"] = {**record["manifest"], "id": "test.wrong-identity"}
    data["extensions"]["test.mismatched-record"] = record
    try:
        extension_store._save(data)  # type: ignore[attr-defined]
    except extension_store.ExtensionError:
        pass
    else:
        raise AssertionError("store accepted a record whose manifest id differs from its key")
    check(
        set(extension_store._load()["extensions"]) == original_ids,  # type: ignore[attr-defined]
        "malformed extension identity fails closed before persistence",
    )


async def _check_projection_response_singleflight_case(
    *,
    name: str,
    route,
    key_attr: str,
    build_attr: str,
    payload,
    request_count: int,
) -> None:
    original_key = getattr(extension_store, key_attr)
    original_build = getattr(extension_store, build_attr)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def fake_key():
        return ("singleflight-test", name)

    def fake_build():
        nonlocal calls
        calls += 1
        entered.set()
        if not release.wait(timeout=2):
            raise AssertionError("projection build was not released")
        return payload

    extension_api._projection_response_cache.clear()  # type: ignore[attr-defined]
    extension_api._projection_response_inflight_by_loop.clear()  # type: ignore[attr-defined]
    setattr(extension_store, key_attr, fake_key)
    setattr(extension_store, build_attr, fake_build)
    try:
        tasks = [asyncio.create_task(route()) for _ in range(request_count)]
        check(await asyncio.to_thread(entered.wait, 1), f"{name} projection build entered")
        release.set()
        responses = await asyncio.gather(*tasks)
    finally:
        setattr(extension_store, key_attr, original_key)
        setattr(extension_store, build_attr, original_build)
        extension_api._projection_response_cache.clear()  # type: ignore[attr-defined]
        extension_api._projection_response_inflight_by_loop.clear()  # type: ignore[attr-defined]
    check(calls == 1, f"{name} duplicate cold requests build once")
    bodies = [response.body for response in responses]
    check(all(body == bodies[0] for body in bodies), f"{name} duplicate cold requests share bytes")
    check(
        all(response.media_type == "application/json" for response in responses),
        f"{name} duplicate cold requests return json media type",
    )


async def _check_projection_response_failure_cleanup() -> None:
    original_key = extension_store.frontend_entrypoints_cache_key
    original_build = extension_store.frontend_entrypoints
    calls = 0

    def fake_key():
        return ("singleflight-failure",)

    def fake_build():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return []

    extension_api._projection_response_cache.clear()  # type: ignore[attr-defined]
    extension_api._projection_response_inflight_by_loop.clear()  # type: ignore[attr-defined]
    extension_store.frontend_entrypoints_cache_key = fake_key  # type: ignore[assignment]
    extension_store.frontend_entrypoints = fake_build  # type: ignore[assignment]
    try:
        try:
            await extension_api.get_frontend_entrypoints()
        except RuntimeError:
            pass
        else:
            raise AssertionError("failed projection build should propagate")
        response = await extension_api.get_frontend_entrypoints()
    finally:
        extension_store.frontend_entrypoints_cache_key = original_key  # type: ignore[assignment]
        extension_store.frontend_entrypoints = original_build  # type: ignore[assignment]
        extension_api._projection_response_cache.clear()  # type: ignore[attr-defined]
        extension_api._projection_response_inflight_by_loop.clear()  # type: ignore[attr-defined]
    check(calls == 2, "failed projection build is removed from in-flight map")
    check(response.body == b'{"entrypoints":[]}', "projection succeeds after failure cleanup")


async def _check_projection_response_cancellation_shield() -> None:
    original_key = extension_store.frontend_entrypoints_cache_key
    original_build = extension_store.frontend_entrypoints
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def fake_key():
        return ("singleflight-cancel",)

    def fake_build():
        nonlocal calls
        calls += 1
        entered.set()
        if not release.wait(timeout=2):
            raise AssertionError("projection build was not released")
        return [{"extension_id": "x"}]

    extension_api._projection_response_cache.clear()  # type: ignore[attr-defined]
    extension_api._projection_response_inflight_by_loop.clear()  # type: ignore[attr-defined]
    extension_store.frontend_entrypoints_cache_key = fake_key  # type: ignore[assignment]
    extension_store.frontend_entrypoints = fake_build  # type: ignore[assignment]
    try:
        leader = asyncio.create_task(extension_api.get_frontend_entrypoints())
        check(await asyncio.to_thread(entered.wait, 1), "cancellation projection build entered")
        follower = asyncio.create_task(extension_api.get_frontend_entrypoints())
        await asyncio.sleep(0)
        follower.cancel()
        try:
            await follower
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled projection waiter should raise CancelledError")
        release.set()
        response = await leader
    finally:
        extension_store.frontend_entrypoints_cache_key = original_key  # type: ignore[assignment]
        extension_store.frontend_entrypoints = original_build  # type: ignore[assignment]
        extension_api._projection_response_cache.clear()  # type: ignore[attr-defined]
        extension_api._projection_response_inflight_by_loop.clear()  # type: ignore[attr-defined]
    check(calls == 1, "cancelled projection waiter does not cancel shared build")
    check(response.body == b'{"entrypoints":[{"extension_id":"x"}]}', "leader receives projection after waiter cancellation")


def _check_projection_response_singleflight() -> None:
    async def _run() -> None:
        await _check_projection_response_singleflight_case(
            name="frontend-entrypoints",
            route=extension_api.get_frontend_entrypoints,
            key_attr="frontend_entrypoints_cache_key",
            build_attr="frontend_entrypoints",
            payload=[{"extension_id": "a"}],
            request_count=19,
        )
        await _check_projection_response_singleflight_case(
            name="ui-hooks",
            route=extension_api.get_ui_hooks,
            key_attr="ui_hooks_cache_key",
            build_attr="ui_hooks",
            payload={"quick_buttons": [], "pages": []},
            request_count=6,
        )
        await _check_projection_response_failure_cleanup()
        await _check_projection_response_cancellation_shield()

    asyncio.run(_run())


def main() -> int:
    try:
        app = FastAPI()
        app.include_router(extension_api.router)
        _configure_internal_llm_defaults()
        package = _seed_extension()
        module_package = _seed_module_backend_extension()
        _seed_core_builtin_without_backend(
            extension_id="test.machine-nodes",
            core_role="machine-nodes",
        )
        _check_store_rejects_mismatched_manifest_identity()
        client = TestClient(app)
        _check_projection_response_singleflight()

        response = client.get("/api/extensions/ofek.backend/backend/ping")
        check(response.status_code == 200, "backend extension route dispatches after runtime install")
        check(response.json()["extension_id"] == "ofek.backend", "backend extension receives context")
        check(response.json()["source_repo_url"] == "https://example.test/extensions.git", "backend extension receives source repo")
        check(response.json()["source_extension_path"] == "extensions/backend", "backend extension receives source path")
        spec = extension_store.backend_entrypoint_spec("ofek.backend")
        assert spec is not None
        direct = extension_backend_loader._roundtrip(  # type: ignore[attr-defined]
            extension_backend_loader._get_handle(spec),  # type: ignore[attr-defined]
            spec,
            "http://testserver",
            {"method": "GET", "path": "/ping", "query_string": "", "headers": [], "body": ""},
            5.0,
        )
        direct_result = json.loads(direct.line)
        direct_timing = extension_backend_loader._validated_child_timing(  # type: ignore[attr-defined]
            direct_result, request_id=direct.request_id, roundtrip_ms=direct.elapsed_ms
        )
        check(direct_result["id"] == direct.request_id, "persistent host binds timing and response to request id")
        check(direct_timing is not None, "persistent host emits a valid versioned timing envelope")
        check(direct_timing.queue_dispatch_ms >= 0, "persistent host measures queue-to-dispatch phase")
        blocked = extension_backend_loader._roundtrip(  # type: ignore[attr-defined]
            extension_backend_loader._get_handle(spec),  # type: ignore[attr-defined]
            spec,
            "http://testserver",
            {"method": "GET", "path": "/block-loop", "query_string": "", "headers": [], "body": ""},
            5.0,
        )
        blocked_result = json.loads(blocked.line)
        blocked_timing = extension_backend_loader._validated_child_timing(  # type: ignore[attr-defined]
            blocked_result, request_id=blocked.request_id, roundtrip_ms=blocked.elapsed_ms,
        )
        check(blocked_timing is not None, "blocking child timing validates")
        check(
            blocked_timing.scheduler_max_delay_ms >= 150.0,
            "final scheduler sample preserves overdue loop-blocking evidence",
        )
        background = extension_backend_loader._roundtrip(  # type: ignore[attr-defined]
            extension_backend_loader._get_handle(spec),  # type: ignore[attr-defined]
            spec,
            "http://testserver",
            {"method": "GET", "path": "/background-gil", "query_string": "", "headers": [], "body": ""},
            5.0,
        )
        background_result = json.loads(background.line)
        background_timing = extension_backend_loader._validated_child_timing(  # type: ignore[attr-defined]
            background_result, request_id=background.request_id, roundtrip_ms=background.elapsed_ms,
        )
        check(background_timing is not None, "background-GIL timing validates")
        check(background_timing.cohort_process_cpu_ms >= 200.0, "background GIL registers host CPU")
        check(background_timing.scheduler_max_delay_ms >= 100.0, "background GIL delays child sentinel")
        old_threshold = extension_store.EXTENSION_SLOW_CALL_SECONDS
        extension_store.EXTENSION_SLOW_CALL_SECONDS = 0.1
        try:
            check(
                background_timing.attributable_asgi_ms == 0.0,
                "background host CPU never becomes request-owned attribution",
            )
        finally:
            extension_store.EXTENSION_SLOW_CALL_SECONDS = old_threshold

        concurrent_direct: dict[str, tuple[str, int]] = {}

        def _direct_echo(marker: str, size: int) -> None:
            payload = (marker.encode("ascii") + b"x" * size)[:size]
            result = extension_backend_loader._roundtrip(  # type: ignore[attr-defined]
                extension_backend_loader._get_handle(spec),  # type: ignore[attr-defined]
                spec,
                "http://testserver",
                {
                    "method": "POST",
                    "path": "/echo",
                    "query_string": "",
                    "headers": [["content-type", "application/octet-stream"]],
                    "body": base64.b64encode(payload).decode("ascii"),
                },
                5.0,
            )
            decoded = json.loads(result.line)
            body = json.loads(base64.b64decode(decoded["body"]))
            timing = extension_backend_loader._validated_child_timing(  # type: ignore[attr-defined]
                decoded, request_id=result.request_id, roundtrip_ms=result.elapsed_ms
            )
            check(timing is not None, f"concurrent {marker} response timing validates")
            concurrent_direct[marker] = (body["marker"], body["size"])

        echo_threads = [
            threading.Thread(target=_direct_echo, args=("alpha", 1024)),
            threading.Thread(target=_direct_echo, args=("bravo", 1024 * 1024)),
        ]
        for thread in echo_threads:
            thread.start()
        for thread in echo_threads:
            thread.join(timeout=10)
        check(concurrent_direct["alpha"] == ("alpha" + "x" * 27, 1024), "concurrent small response keeps request-id association")
        check(concurrent_direct["bravo"] == ("bravo" + "x" * 27, 1024 * 1024), "large payload keeps request-id association")

        sentinel_timings: dict[str, object] = {}

        def _direct_sentinel(name: str, path: str) -> None:
            result = extension_backend_loader._roundtrip(  # type: ignore[attr-defined]
                extension_backend_loader._get_handle(spec),  # type: ignore[attr-defined]
                spec,
                "http://testserver",
                {"method": "GET", "path": path, "query_string": "", "headers": [], "body": ""},
                5.0,
            )
            decoded = json.loads(result.line)
            timing = extension_backend_loader._validated_child_timing(  # type: ignore[attr-defined]
                decoded, request_id=result.request_id, roundtrip_ms=result.elapsed_ms,
            )
            check(timing is not None, f"concurrent sentinel {name} timing validates")
            sentinel_timings[name] = timing

        sentinel_threads = [
            threading.Thread(target=_direct_sentinel, args=("block", "/sentinel-block")),
            threading.Thread(target=_direct_sentinel, args=("peer", "/sentinel-peer")),
        ]
        for thread in sentinel_threads:
            thread.start()
        for thread in sentinel_threads:
            thread.join(timeout=5)
        for timing in sentinel_timings.values():
            check(timing.concurrent_requests == 2, "concurrent sentinel records cohort size")
            check(timing.cohort_overlap_ms >= 250.0, "concurrent sentinel records overlap duration")
            check(timing.scheduler_max_delay_ms >= 250.0, "concurrent sentinel preserves overdue sample")

        valid_timing = direct_result["timing"]
        for invalid in (
            {**valid_timing, "version": 4},
            {**valid_timing, "request_id": "wrong"},
            {**valid_timing, "asgi_ns": True},
            {**valid_timing, "asgi_ns": -1},
            {**valid_timing, "asgi_ns": float("nan")},
            {**valid_timing, "asgi_ns": (direct.elapsed_ms * 10.0 + 10.0) * 1_000_000},
        ):
            check(
                extension_backend_loader._validated_child_timing(  # type: ignore[attr-defined]
                    {**direct_result, "timing": invalid},
                    request_id=direct.request_id,
                    roundtrip_ms=direct.elapsed_ms,
                ) is None,
                "invalid or inconsistent child timing never attributes slowness",
            )

        async def _check_parent_delay_not_attributed() -> None:
            original_roundtrip = extension_backend_loader._roundtrip  # type: ignore[attr-defined]
            original_record = extension_store.record_slow_backend_call
            slow_samples: list[float] = []

            def delayed_parent(*_args, **_kwargs):
                time.sleep(0.05)
                rid = "parent-delay"
                body = base64.b64encode(b'{"ok":true}').decode("ascii")
                envelope = {
                    "id": rid,
                    "status": 200,
                    "headers": [],
                    "body": body,
                    "timing": {
                        "version": 3,
                        "request_id": rid,
                        "process_epoch_ns": 1,
                        "queue_dispatch_ns": 1000,
                        "decode_ns": 1000,
                        "build_ns": 1000,
                        "asgi_ns": 1_000_000,
                        "response_collect_ns": 1000,
                        "response_encode_ns": 1000,
                        "cohort_process_cpu_ns": 500_000,
                        "scheduler_max_delay_ns": 1000,
                        "cohort_overlap_ns": 0,
                        "concurrent_requests": 1,
                    },
                }
                return extension_backend_loader._RoundtripResult(  # type: ignore[attr-defined]
                    json.dumps(envelope).encode("utf-8"), rid, 50.0
                )

            def capture(_extension_id: str, *, activation_id: str, elapsed_seconds: float):
                slow_samples.append(elapsed_seconds)
                return []

            extension_backend_loader._roundtrip = delayed_parent  # type: ignore[attr-defined]
            extension_store.record_slow_backend_call = capture  # type: ignore[assignment]
            old_threshold = extension_store.EXTENSION_SLOW_CALL_SECONDS
            extension_store.EXTENSION_SLOW_CALL_SECONDS = 0.01
            try:
                for _ in range(3):
                    response = await extension_backend_loader._invoke_backend(  # type: ignore[attr-defined]
                        spec,
                        method="GET",
                        path="ping",
                        body_bytes=b"",
                        query_b64="",
                        safe_headers=[],
                        base_url="http://testserver",
                    )
                    check(response.status_code == 200, "parent-delayed fast child still returns")
            finally:
                extension_store.EXTENSION_SLOW_CALL_SECONDS = old_threshold
                extension_store.record_slow_backend_call = original_record  # type: ignore[assignment]
                extension_backend_loader._roundtrip = original_roundtrip  # type: ignore[attr-defined]
            check(not slow_samples, "parent transport delay does not count as child ASGI slowness")

        asyncio.run(_check_parent_delay_not_attributed())

        starved = extension_backend_loader._ChildTiming(  # type: ignore[attr-defined]
            0.0, 0.0, 0.0, 8_000.0, 0.0, 0.0, 100.0, 3_000.0, 0.0, 1,
        )
        check(
            starved.attributable_asgi_ms == 0.0,
            "system scheduler starvation is not attributed to the extension route",
        )
        cpu_bound = extension_backend_loader._ChildTiming(  # type: ignore[attr-defined]
            0.0, 0.0, 0.0, 8_000.0, 0.0, 0.0, 6_000.0, 3_000.0, 0.0, 1,
        )
        check(
            cpu_bound.attributable_asgi_ms == 0.0,
            "process-wide CPU never proves single-request ownership",
        )
        overlapped = extension_backend_loader._ChildTiming(  # type: ignore[attr-defined]
            0.0, 0.0, 0.0, 8_000.0, 0.0, 0.0, 6_000.0, 3_000.0, 3_000.0, 2,
        )
        check(
            overlapped.attributable_asgi_ms == 0.0,
            "material cohort overlap excludes ambiguous scheduler-starved attribution",
        )
        tiny_overlap = extension_backend_loader._ChildTiming(  # type: ignore[attr-defined]
            0.0, 0.0, 0.0, 8_000.0, 0.0, 0.0, 6_000.0, 3_000.0, 1.0, 2,
        )
        check(
            tiny_overlap.attributable_asgi_ms == 0.0,
            "scheduler starvation stays host-attributed without request-owned CPU",
        )

        async def _check_slow_child_is_attributed() -> None:
            original_record = extension_store.record_slow_backend_call
            original_activation_identity = extension_store.activation_identity
            original_get_handle = extension_backend_loader._get_handle  # type: ignore[attr-defined]
            slow_samples: list[float] = []
            capture_order: list[str] = []

            def capture(_extension_id: str, *, activation_id: str, elapsed_seconds: float):
                slow_samples.append(elapsed_seconds)
                return []

            def capture_activation(extension_id: str) -> str:
                capture_order.append("activation")
                return original_activation_identity(extension_id)

            def capture_handle(current_spec):
                capture_order.append("handle")
                return original_get_handle(current_spec)

            extension_store.record_slow_backend_call = capture  # type: ignore[assignment]
            extension_store.activation_identity = capture_activation  # type: ignore[assignment]
            extension_backend_loader._get_handle = capture_handle  # type: ignore[attr-defined]
            old_threshold = extension_store.EXTENSION_SLOW_CALL_SECONDS
            extension_store.EXTENSION_SLOW_CALL_SECONDS = 0.01
            try:
                response = await extension_backend_loader._invoke_backend(  # type: ignore[attr-defined]
                    spec,
                    method="GET",
                    path="sleep-short",
                    body_bytes=b"",
                    query_b64="",
                    safe_headers=[],
                    base_url="http://testserver",
                )
            finally:
                extension_store.EXTENSION_SLOW_CALL_SECONDS = old_threshold
                extension_store.record_slow_backend_call = original_record  # type: ignore[assignment]
                extension_store.activation_identity = original_activation_identity  # type: ignore[assignment]
                extension_backend_loader._get_handle = original_get_handle  # type: ignore[attr-defined]
            check(response.status_code == 200, "slow child ASGI route returns")
            check(len(slow_samples) == 1 and slow_samples[0] >= 0.04, "slow child ASGI duration is attributed")
            check(capture_order[:2] == ["activation", "handle"], "activation is captured before backend resolution")

        asyncio.run(_check_slow_child_is_attributed())
        # Bare base (no trailing slash) must dispatch DIRECTLY to the extension's
        # root handler. follow_redirects=False is essential: unpatched code only
        # 307-redirects to the slashed path, which the live app's static mount
        # preempts into a 404 "Not Found" — the actual page-load bug.
        response = client.get("/api/extensions/ofek.backend/backend", follow_redirects=False)
        check(
            response.status_code == 200 and response.json().get("root") is True,
            "bare extension backend base (no trailing slash) reaches root handler",
        )
        response = client.get(
            "/api/extensions/ofek.backend/backend/headers",
            headers={
                "Authorization": "Bearer secret",
                "Cookie": "session=secret",
                "X-Internal-Token": "secret",
                "Accept": "application/json",
            },
        )
        check(response.status_code == 200, "backend extension receives scrubbed request")
        body = response.json()
        check(body["authorization"] is None, "authorization header is stripped")
        check(body["cookie"] is None, "cookie header is stripped")
        check(body["x_internal_token"] is None, "internal token header is stripped")
        check(body["accept"] == "application/json", "non-sensitive header is preserved")
        response = client.get("/api/extensions/ofek.backend/backend/sdk-env")
        check(response.status_code == 200, "backend extension SDK env route dispatches")
        body = response.json()
        check(body["sdk_importable"] is True, "backend extension can import better_agent_sdk")
        check(body["core_importable"] is False, "backend extension cannot import core backend modules")
        check(body["extension_id"] == "ofek.backend", "backend extension receives SDK extension id env")
        check(body["backend_url"].startswith("http://testserver"), "backend extension receives backend URL env")
        check(body["has_internal_token"] is True, "backend extension with internal_loopback receives SDK token env")
        sentinel = "/tmp/bc-test-active-checkout-sentinel"
        prior_active_checkout = os.environ.get("BETTER_AGENT_ACTIVE_CHECKOUT")
        os.environ["BETTER_AGENT_ACTIVE_CHECKOUT"] = sentinel
        host_env = extension_backend_loader._host_env()
        check("BETTER_AGENT_ACTIVE_CHECKOUT" not in host_env, "_host_env hides launcher checkout state")
        extension_backend_loader.evict_persistent_backend("ofek.backend")
        response = client.get("/api/extensions/ofek.backend/backend/sdk-env")
        check(response.status_code == 200, "sdk-env dispatches with launcher checkout hidden")
        check(not response.json().get("active_checkout"), "extension subprocess cannot read active checkout")
        if prior_active_checkout is None:
            os.environ.pop("BETTER_AGENT_ACTIVE_CHECKOUT", None)
        else:
            os.environ["BETTER_AGENT_ACTIVE_CHECKOUT"] = prior_active_checkout
        extension_backend_loader.evict_persistent_backend("ofek.backend")
        response = client.get("/api/extensions/ofek.compiled-backend/backend/ping")
        check(response.status_code == 200, "module backend extension route dispatches")
        check(response.json()["extension_id"] == "ofek.compiled-backend", "module backend receives context")
        check(response.json()["install_path"] == str(module_package.resolve()), "module backend receives install path")
        response = client.get("/api/extensions/ofek.backend/backend/boom")
        check(response.status_code == 500, "backend extension exceptions return 500")
        check("secret" not in response.text.lower(), "backend extension exception detail is hidden")
        os.environ.pop("BA_EXTENSION_MUTATED_PARENT", None)
        response = client.post("/api/extensions/ofek.backend/backend/mutate-env")
        check(response.status_code == 200, "backend extension subprocess can handle mutation route")
        check(os.environ.get("BA_EXTENSION_MUTATED_PARENT") is None, "extension process cannot mutate parent env")
        response = client.post("/api/extensions/ofek.backend/backend/mutate-env", content=b"x" * (2 * 1024 * 1024 + 1))
        check(response.status_code == 413, "oversized extension backend request is rejected")
        try:
            asyncio.run(extension_backend_loader._read_limited_body(_DisconnectingRequest()))  # type: ignore[attr-defined]
        except Exception as exc:
            _assert_client_closed(exc, "disconnecting extension backend request is reported as client-closed")
        else:
            raise AssertionError("disconnecting extension backend request should not produce a body")
        original_invoke = extension_backend_loader._invoke_backend  # type: ignore[attr-defined]

        async def fail_invoke(*args, **kwargs):
            raise AssertionError("disconnected request reached extension backend invocation")

        extension_backend_loader._invoke_backend = fail_invoke  # type: ignore[attr-defined]
        try:
            asyncio.run(
                extension_backend_loader.dispatch_extension_backend_request(
                    "ofek.backend",
                    "mutate-env",
                    _DisconnectingRequest(),  # type: ignore[arg-type]
                    backend_spec={"extension_id": "ofek.backend"},
                )
            )
        except Exception as exc:
            _assert_client_closed(exc, "disconnecting dispatch stops before extension backend invocation")
        else:
            raise AssertionError("disconnecting extension backend dispatch should fail closed")
        finally:
            extension_backend_loader._invoke_backend = original_invoke  # type: ignore[attr-defined]
        old_timeout = extension_backend_loader._HOST_TIMEOUT_SECONDS  # type: ignore[attr-defined]
        extension_backend_loader._HOST_TIMEOUT_SECONDS = 1.0  # type: ignore[attr-defined]
        try:
            response = client.get("/api/extensions/ofek.backend/backend/slow")
        finally:
            extension_backend_loader._HOST_TIMEOUT_SECONDS = old_timeout  # type: ignore[attr-defined]
        check(response.status_code == 504, "slow extension backend request times out")
        # Multiplexed transport abandons the timed-out request but keeps the
        # process alive so concurrent/subsequent requests keep working.
        pid = int((package / "slow.pid").read_text(encoding="utf-8"))
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
        check(alive, "timed-out request leaves the extension process alive (no kill)")
        old_timeout = extension_backend_loader._HOST_TIMEOUT_SECONDS  # type: ignore[attr-defined]
        extension_backend_loader._HOST_TIMEOUT_SECONDS = 1.0  # type: ignore[attr-defined]
        started = time.monotonic()
        try:
            status, _ = extension_backend_loader.invoke_extension_backend_sync(
                "ofek.backend",
                "slow",
                method="GET",
                base_url="http://testserver",
            )
        finally:
            extension_backend_loader._HOST_TIMEOUT_SECONDS = old_timeout  # type: ignore[attr-defined]
        check(status == 504, "sync extension backend timeout remains distinguishable")
        check(time.monotonic() - started < 3.0, "sync extension backend timeout does not block caller")

        # Per-route backend timeout: with the global host gate at 1s, a route
        # that sleeps 2s would 504 — but its manifest backend_timeouts entry
        # (sleep2 -> 5s) must override the default and let it complete.
        old_timeout = extension_backend_loader._HOST_TIMEOUT_SECONDS  # type: ignore[attr-defined]
        extension_backend_loader._HOST_TIMEOUT_SECONDS = 1.0  # type: ignore[attr-defined]
        try:
            response = client.get("/api/extensions/ofek.backend/backend/sleep2")
            check(response.status_code == 200, "per-route backend_timeouts overrides the host default")
            check(response.json()["ok"] is True, "per-route timed route returns its body")
            # A route with no per-route entry still uses the (patched 1s) default.
            response = client.get("/api/extensions/ofek.backend/backend/slow")
            check(response.status_code == 504, "route without per-route timeout keeps the host default")
        finally:
            extension_backend_loader._HOST_TIMEOUT_SECONDS = old_timeout  # type: ignore[attr-defined]

        # Resolver unit checks: longest-prefix match, default fallback, no cap.
        spec = {"backend_timeouts": {"a/b": 7, "a": 3, "default": 2}}
        check(extension_backend_loader._resolve_host_timeout(spec, "a/b") == 7.0, "exact route match wins")  # type: ignore[attr-defined]
        check(extension_backend_loader._resolve_host_timeout(spec, "a/b/c") == 7.0, "longest segment-prefix wins")  # type: ignore[attr-defined]
        check(extension_backend_loader._resolve_host_timeout(spec, "a/x") == 3.0, "shorter prefix matches when longer does not")  # type: ignore[attr-defined]
        check(extension_backend_loader._resolve_host_timeout(spec, "z") == 2.0, "default applies with no prefix match")  # type: ignore[attr-defined]
        check(
            extension_backend_loader._resolve_host_timeout({}, "anything")  # type: ignore[attr-defined]
            == extension_backend_loader._HOST_TIMEOUT_SECONDS,  # type: ignore[attr-defined]
            "no backend_timeouts falls back to host default",
        )
        check(
            extension_backend_loader._resolve_host_timeout({"backend_timeouts": {"x": 10 ** 9}}, "x")  # type: ignore[attr-defined]
            == float(10 ** 9),
            "large per-route timeout is honored verbatim (no cap)",
        )

        # Manifest validation: reject bad backend_timeouts, accept good ones.
        try:
            extension_store._validate_backend_timeouts({"x": "nope"})  # type: ignore[attr-defined]
        except extension_store.ExtensionError:
            pass
        else:
            raise AssertionError("non-numeric backend_timeouts must be rejected")
        try:
            extension_store._validate_backend_timeouts({"x": 0})  # type: ignore[attr-defined]
        except extension_store.ExtensionError:
            pass
        else:
            raise AssertionError("non-positive backend_timeouts must be rejected")
        validated = extension_store._validate_backend_timeouts({"/sessions/search/": 930, "default": 30})  # type: ignore[attr-defined]
        check(validated == {"sessions/search": 930.0, "default": 30.0}, "valid backend_timeouts are slash-normalized")

        try:
            extension_store._validate_backend_retry_on_exit({"x": True})  # type: ignore[attr-defined]
        except extension_store.ExtensionError:
            pass
        else:
            raise AssertionError("non-array backend_retry_on_exit must be rejected")
        try:
            extension_store._validate_backend_retry_on_exit(["../x"])  # type: ignore[attr-defined]
        except extension_store.ExtensionError:
            pass
        else:
            raise AssertionError("path traversal backend_retry_on_exit must be rejected")
        validated_retry = extension_store._validate_backend_retry_on_exit(["/assistant/ensure/", "assistant/ensure"])  # type: ignore[attr-defined]
        check(validated_retry == ("assistant/ensure",), "valid backend_retry_on_exit entries are slash-normalized and deduped")
        check(
            extension_backend_loader._allows_backend_exit_retry(  # type: ignore[attr-defined]
                {"backend_retry_on_exit": ["assistant/ensure"]},
                "assistant/ensure",
            ),
            "backend_retry_on_exit exact route allows retry",
        )
        check(
            not extension_backend_loader._allows_backend_exit_retry(  # type: ignore[attr-defined]
                {"backend_retry_on_exit": ["assistant/ensure"]},
                "assistant/ensure/child",
            ),
            "backend_retry_on_exit does not prefix-match routes",
        )

        # Concurrency: a fast route must return while a slow route is in flight
        # on the SAME extension subprocess — requests are multiplexed, not
        # serialized behind one lock. Pre-multiplex this fast call would queue
        # behind the 2s /sleep2 and take ~2s.
        concurrent_results: dict[str, tuple[int, float]] = {}

        def _fire(key: str, route: str) -> None:
            t0 = time.monotonic()
            r = client.get(f"/api/extensions/ofek.backend/backend/{route}")
            concurrent_results[key] = (r.status_code, time.monotonic() - t0)

        run_start = time.monotonic()
        bg = threading.Thread(target=_fire, args=("slow", "sleep2"))
        bg.start()
        time.sleep(0.4)  # let /sleep2 reach the extension and start sleeping
        _fire("fast", "ping")
        fast_total = time.monotonic() - run_start
        check(concurrent_results["fast"][0] == 200, "fast route returns 200 while a slow route is in flight")
        check(fast_total < 1.8, "fast route is not head-of-line-blocked by the in-flight slow route")
        bg.join()
        check(concurrent_results["slow"][0] == 200, "concurrent slow route also completes")

        async def _check_get_coalescing() -> None:
            calls = 0
            entered = asyncio.Event()
            release = asyncio.Event()
            original = extension_backend_loader._invoke_backend  # type: ignore[attr-defined]

            async def fake_invoke(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                from fastapi.responses import Response
                return Response(content=b'{"ok":true}', media_type="application/json")

            extension_backend_loader._GET_INFLIGHT.clear()  # type: ignore[attr-defined]
            extension_backend_loader._invoke_backend = fake_invoke  # type: ignore[attr-defined]
            spec = {"extension_id": "ofek.backend"}
            try:
                task1 = asyncio.create_task(
                    extension_backend_loader._invoke_backend_get_coalesced(  # type: ignore[attr-defined]
                        spec,
                        method="GET",
                        path="workers",
                        body_bytes=b"",
                        query_b64="",
                        safe_headers=[("accept", "application/json"), ("x-request-id", "one")],
                        base_url="http://testserver",
                    )
                )
                await asyncio.wait_for(entered.wait(), timeout=1)
                task2 = asyncio.create_task(
                    extension_backend_loader._invoke_backend_get_coalesced(  # type: ignore[attr-defined]
                        spec,
                        method="GET",
                        path="workers",
                        body_bytes=b"",
                        query_b64="",
                        safe_headers=[("accept", "application/json"), ("x-request-id", "two")],
                        base_url="http://testserver",
                    )
                )
                release.set()
                r1, r2 = await asyncio.gather(task1, task2)
            finally:
                extension_backend_loader._invoke_backend = original  # type: ignore[attr-defined]
                extension_backend_loader._GET_INFLIGHT.clear()  # type: ignore[attr-defined]
            check(calls == 1, "duplicate in-flight extension GET invokes backend once")
            check(r1 is not r2, "duplicate in-flight extension GET returns independent responses")
            check(r1.body == r2.body == b'{"ok":true}', "duplicate in-flight extension GET shares response body")

        asyncio.run(_check_get_coalescing())

        response = client.get("/api/extensions/frontend-entrypoints")
        check(response.status_code == 200, "frontend entrypoint catalog returns")
        entrypoints = response.json()["entrypoints"]
        entrypoint = next((item for item in entrypoints if item["extension_id"] == "ofek.backend"), None)
        check(entrypoint is not None, "frontend entrypoint should be listed")
        check(
            entrypoint["entrypoint_url"].startswith("/api/extensions/ofek.backend/frontend/ui/index.js"),
            "frontend entrypoint URL is scoped",
        )

        response = client.get("/api/extensions/ofek.backend/frontend/ui/index.js")
        check(response.status_code == 200 and "ok = true" in response.text, "frontend bundle asset served")
        try:
            extension_store.resolve_frontend_asset("ofek.backend", "backend/secret.txt")
        except extension_store.ExtensionError:
            pass
        else:
            raise AssertionError("frontend asset escaped bundle root")

        # Trusted-by-install: a non-builtin extension needs consent before enable.
        extension_store.grant_consent("ofek.backend")
        extension_store.set_enabled("ofek.backend", False)
        extension_backend_loader._clear_spec_cache("ofek.backend")  # type: ignore[attr-defined]
        status, body = extension_backend_loader.invoke_extension_backend_sync(
            "ofek.backend", "ping"
        )
        check(status == 503 and b"retry_after" in body, "disabled internal backend is retryable 503")

        async def _disabled_internal_status() -> None:
            try:
                await extension_backend_loader.invoke_extension_backend("ofek.backend", "ping")
            except Exception as exc:
                check(getattr(exc, "status_code", None) == 503, "async disabled backend is 503")
                check(getattr(exc, "headers", {}).get("Retry-After") == "60", "503 carries Retry-After")
            else:
                raise AssertionError("disabled internal backend unexpectedly dispatched")

        asyncio.run(_disabled_internal_status())
        status, _ = extension_backend_loader.invoke_named_core_destination_sync(
            "unregistered.destination", body_bytes=b"{}"
        )
        check(status == 404, "unknown named destination fails closed")
        response = client.get("/api/extensions/ofek.backend/backend/ping")
        check(response.status_code == 404, "disabled extension backend route fails closed")
        response = client.get("/api/extensions/ofek.backend/frontend/ui/index.js")
        check(response.status_code == 404, "disabled extension frontend asset fails closed")
        extension_store.set_enabled("ofek.backend", True)
        response = client.get("/api/extensions/ofek.backend/backend/ping")
        check(response.status_code == 200, "re-enabled extension backend route dispatches without restart")

        response = client.get(f"/api/extensions/{extension_store.extension_id_for_role('machine-nodes')}/backend/pending_nodes")
        check(response.status_code == 200, "core built-in backend compatibility route returns")
        check(response.json() == {"pending_nodes": []}, "machine-node pending fallback returns empty snapshot")
        _seed_core_builtin_without_backend(
            extension_id="test.project-structure",
            core_role="project-structure",
        )
        project_id = encode_cwd(str(TMP_HOME))
        project_update_store.append(project_id, "changed")
        response = client.post(
            f"/api/extensions/{extension_store.extension_id_for_role('project-structure')}/backend/project-updates/counts-batch",
            json={"cwds": [str(TMP_HOME)]},
        )
        check(
            response.status_code == 200 and response.json().get(project_id) == 1,
            "project-structure core counts-batch route returns unseen count",
        )
        extension_store.set_enabled(extension_store.extension_id_for_role('machine-nodes'), False)
        response = client.get(f"/api/extensions/{extension_store.extension_id_for_role('machine-nodes')}/backend/pending_nodes")
        check(response.status_code == 404, "disabled core built-in compatibility route fails closed")

        extension_backend_loader._clear_spec_cache()  # type: ignore[attr-defined]
        calls = 0
        original_spec = extension_store.backend_entrypoint_spec

        def counted_spec(extension_id: str):
            nonlocal calls
            calls += 1
            return original_spec(extension_id)

        extension_store.backend_entrypoint_spec = counted_spec  # type: ignore[assignment]
        try:
            for _ in range(3):
                response = client.get("/api/extensions/ofek.no-backend/backend/missing")
                check(response.status_code == 404, "missing backend surface still returns 404")
        finally:
            extension_store.backend_entrypoint_spec = original_spec  # type: ignore[assignment]
            extension_backend_loader._clear_spec_cache()  # type: ignore[attr-defined]
        check(calls == 1, "missing backend surface lookup is cached")

        check(package.exists(), "fixture package remains until cleanup")
    finally:
        os.environ.pop("BETTER_CLAUDE_INTERNAL_TOKEN", None)
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
