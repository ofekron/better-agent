from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402
_test_home.isolate("bc-test-requirements-extension-reconciler-")

from event_bus import BusEvent, bus  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import extension_api  # noqa: E402
import extension_backend_loader  # noqa: E402
import extension_store  # noqa: E402
import requirements_extension_reconciler as reconciler  # noqa: E402


def test_startup_runtime_and_shutdown_lifecycle() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        original_invoke = reconciler._invoke
        original_owner = reconciler.extension_store.extension_id_for_role
        original_evict = reconciler.extension_backend_loader.evict_persistent_backend
        evicted: list[str] = []

        async def invoke(reason: str) -> dict:
            calls.append(reason)
            return {"status": "starting"}

        reconciler._invoke = invoke
        reconciler.extension_store.extension_id_for_role = lambda _role: "requirements-id"
        reconciler.extension_backend_loader.evict_persistent_backend = evicted.append
        try:
            assert await reconciler.bind_and_reconcile() == {"status": "starting"}
            await bus.publish(BusEvent(
                type="provider.runtime_changed",
                root_id="",
                sid="",
                payload={"causes": ["config"]},
                persist=False,
            ))
            assert calls == ["startup", "provider_runtime"]
            await reconciler.shutdown()
            await bus.publish(BusEvent(
                type="provider.runtime_changed",
                root_id="",
                sid="",
                payload={"causes": ["auth"]},
                persist=False,
            ))
            assert calls == ["startup", "provider_runtime"]
            assert evicted == ["requirements-id"]
        finally:
            bus.unsubscribe(reconciler._SUBSCRIBER)
            reconciler._invoke = original_invoke
            reconciler.extension_store.extension_id_for_role = original_owner
            reconciler.extension_backend_loader.evict_persistent_backend = original_evict
            reconciler._shutting_down = True
            reconciler._reconcile_lock = None

    asyncio.run(scenario())


def test_core_contract_route_and_public_header_filter() -> None:
    package = Path(tempfile.mkdtemp(prefix="requirements-core-contract-"))
    extension_id = "test.requirements-core-contract"
    original_integrations = extension_store.installation_profile.integrations_enabled
    (package / "routes.py").write_text(
        "from fastapi import APIRouter, Header, HTTPException\n"
        "def create_router(_context):\n"
        "    router = APIRouter()\n"
        "    @router.post('/internal/reconcile')\n"
        "    def reconcile(body: dict, core_contract: str = Header(default='', "
        "alias='X-Better-Agent-Core-Contract')):\n"
        "        if core_contract != 'requirements-reconcile-v1':\n"
        "            raise HTTPException(status_code=403)\n"
        "        return {'status': 'accepted', 'body': body}\n"
        "    return router\n",
        encoding="utf-8",
    )
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "core_roles": ["requirements"],
        "name": "Requirements core contract",
        "version": "1.0.0",
        "description": "Core contract test fixture",
        "surfaces": ["backend_feature"],
        "entrypoints": {"backend": "routes.py"},
        "permissions": {"backend_routes": True},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    try:
        extension_store.installation_profile.integrations_enabled = lambda: True
        extension_store._install_from_package_dir(
            package_dir=package,
            source={
                "type": "test",
                "repo_url": "",
                "extension_path": package.name,
                "ref": "",
                "commit_sha": "requirements-core-contract",
            },
            force_enabled=True,
            persist=True,
        )
        app = FastAPI()
        app.include_router(extension_api.router)
        with TestClient(app) as client:
            public = client.post(
                f"/api/extensions/{extension_id}/backend/internal/reconcile",
                headers={
                    "X-Better-Agent-Core-Contract": "requirements-reconcile-v1",
                },
                json={"reason": "startup"},
            )
        assert public.status_code == 403

        async def invoke_core():
            return await extension_backend_loader.invoke_core_backend_contract(
                "requirements.reconcile",
                body_bytes=b'{"reason":"startup"}',
            )

        core = asyncio.run(invoke_core())
        assert core.status_code == 200
        assert json.loads(bytes(core.body)) == {
            "status": "accepted",
            "body": {"reason": "startup"},
        }
    finally:
        extension_backend_loader.shutdown_persistent_backends()
        extension_store.installation_profile.integrations_enabled = original_integrations
        shutil.rmtree(package, ignore_errors=True)


def test_shutdown_waits_for_inflight_reconcile_and_drops_new_facts() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        started = asyncio.Event()
        release = asyncio.Event()
        evicted: list[str] = []
        original_invoke = reconciler._invoke
        original_owner = reconciler.extension_store.extension_id_for_role
        original_evict = reconciler.extension_backend_loader.evict_persistent_backend

        async def invoke(reason: str) -> dict:
            calls.append(reason)
            if reason == "provider_runtime":
                started.set()
                await release.wait()
            return {"status": "starting"}

        reconciler._invoke = invoke
        reconciler.extension_store.extension_id_for_role = lambda _role: "requirements-id"
        reconciler.extension_backend_loader.evict_persistent_backend = evicted.append
        try:
            await reconciler.bind_and_reconcile()
            first_fact = asyncio.create_task(bus.publish(BusEvent(
                type="provider.runtime_changed",
                root_id="",
                sid="",
                payload={"causes": ["config"]},
                persist=False,
            )))
            await asyncio.wait_for(started.wait(), timeout=1)
            shutting_down = asyncio.create_task(reconciler.shutdown())
            await asyncio.sleep(0)
            assert not shutting_down.done()
            assert evicted == []
            await bus.publish(BusEvent(
                type="provider.runtime_changed",
                root_id="",
                sid="",
                payload={"causes": ["auth"]},
                persist=False,
            ))
            release.set()
            await first_fact
            await shutting_down
            assert calls == ["startup", "provider_runtime"]
            assert evicted == ["requirements-id"]
        finally:
            release.set()
            bus.unsubscribe(reconciler._SUBSCRIBER)
            reconciler._invoke = original_invoke
            reconciler.extension_store.extension_id_for_role = original_owner
            reconciler.extension_backend_loader.evict_persistent_backend = original_evict
            reconciler._shutting_down = True
            reconciler._reconcile_lock = None

    asyncio.run(scenario())


def test_wiring_orders_startup_after_extension_reconcile_and_shutdown_before_teardown() -> None:
    source = (BACKEND / "app_lifecycle.py").read_text(encoding="utf-8")
    startup = source.index("requirements_extension_reconciler.bind_and_reconcile")
    extension_reconcile = source.index('"extension_reconciliation"')
    shutdown = source.index("requirements_extension_reconciler.shutdown")
    teardown = source.index("extension_api.shutdown_hot_path_executors")
    assert extension_reconcile < startup
    assert shutdown < teardown


if __name__ == "__main__":
    test_startup_runtime_and_shutdown_lifecycle()
    test_core_contract_route_and_public_header_filter()
    test_shutdown_waits_for_inflight_reconcile_and_drops_new_facts()
    test_wiring_orders_startup_after_extension_reconcile_and_shutdown_before_teardown()
    print("OK: requirements extension reconciler")
