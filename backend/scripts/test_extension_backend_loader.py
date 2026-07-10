#!/usr/bin/env python3
from __future__ import annotations

import asyncio
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
                "import importlib.util",
                "import time",
                "from fastapi import APIRouter, Request",
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


def _seed_core_builtin_without_backend(extension_id: str) -> None:
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_id] = {
        "manifest": {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_id,
            "name": extension_id,
            "version": "1.0.0",
            "description": "",
            "surfaces": ["backend_feature"],
            "entrypoints": {},
            "permissions": {},
            "marketplace": {},
        },
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
        _seed_core_builtin_without_backend(extension_store.extension_id_for_role('machine-nodes'))
        client = TestClient(app)
        _check_projection_response_singleflight()

        response = client.get("/api/extensions/ofek.backend/backend/ping")
        check(response.status_code == 200, "backend extension route dispatches after runtime install")
        check(response.json()["extension_id"] == "ofek.backend", "backend extension receives context")
        check(response.json()["source_repo_url"] == "https://example.test/extensions.git", "backend extension receives source repo")
        check(response.json()["source_extension_path"] == "extensions/backend", "backend extension receives source path")
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
        check(status == 500, "sync extension backend timeout fails closed")
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
        import threading

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
        _seed_core_builtin_without_backend(extension_store.extension_id_for_role('project-structure'))
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
