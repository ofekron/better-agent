#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

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


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


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


def main() -> int:
    try:
        app = FastAPI()
        app.include_router(extension_api.router)
        _configure_internal_llm_defaults()
        package = _seed_extension()
        module_package = _seed_module_backend_extension()
        _seed_core_builtin_without_backend(extension_store.BUILTIN_MACHINE_NODES_EXTENSION_ID)
        client = TestClient(app)

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
        old_timeout = extension_backend_loader._HOST_TIMEOUT_SECONDS  # type: ignore[attr-defined]
        extension_backend_loader._HOST_TIMEOUT_SECONDS = 1.0  # type: ignore[attr-defined]
        try:
            response = client.get("/api/extensions/ofek.backend/backend/slow")
        finally:
            extension_backend_loader._HOST_TIMEOUT_SECONDS = old_timeout  # type: ignore[attr-defined]
        check(response.status_code == 504, "slow extension backend request times out")
        pid = int((package / "slow.pid").read_text(encoding="utf-8"))
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except OSError:
            pass
        else:
            raise AssertionError("timed-out extension process still alive")
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

        response = client.get("/api/extensions/frontend-entrypoints")
        check(response.status_code == 200, "frontend entrypoint catalog returns")
        entrypoints = response.json()["entrypoints"]
        entrypoint = next((item for item in entrypoints if item["extension_id"] == "ofek.backend"), None)
        check(entrypoint is not None, "frontend entrypoint should be listed")
        check(entrypoint["entrypoint_url"] == "/api/extensions/ofek.backend/frontend/ui/index.js", "frontend entrypoint URL is scoped")

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

        response = client.get(f"/api/extensions/{extension_store.BUILTIN_MACHINE_NODES_EXTENSION_ID}/backend/pending_nodes")
        check(response.status_code == 200, "core built-in backend compatibility route returns")
        check(response.json() == {"pending_nodes": []}, "machine-node pending fallback returns empty snapshot")
        extension_store.set_enabled(extension_store.BUILTIN_MACHINE_NODES_EXTENSION_ID, False)
        response = client.get(f"/api/extensions/{extension_store.BUILTIN_MACHINE_NODES_EXTENSION_ID}/backend/pending_nodes")
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
