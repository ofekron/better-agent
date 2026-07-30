#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "sdk"))

from fastapi import HTTPException

import _test_home

_test_home.isolate("ba-test-")
os.environ["BETTER_AGENT_SKIP_EXTENSION_DEPENDENCY_INSTALL"] = "1"
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
os.environ["BETTER_AGENT_RUNTIME_BROKER"] = "unix:/tmp/better-agent-test.sock"

dist_dir = ROOT.parent / "frontend" / "dist"
created_dist = not dist_dir.exists()
if created_dist:
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text(
        "<!doctype html><title>stub</title>", encoding="utf-8"
    )

import extension_store  # noqa: E402
import installation_profile  # noqa: E402

installation_profile.integrations_enabled = lambda: True
installation_profile.allows = lambda _capability: True


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def _load_marketplace_routes_module():
    routes_path = ROOT.parent / "extensions" / "marketplace" / "backend" / "routes.py"
    spec = importlib.util.spec_from_file_location(
        "marketplace_backend_routes", routes_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _marketplace_router(module, source: dict[str, object]):
    context = type(
        "Context",
        (),
        {
            "source": source,
            "extension_id": extension_store.MARKETPLACE_EXTENSION_ID,
        },
    )()
    return module.create_router(context)


def _catalog_endpoint(router):
    return next(route.endpoint for route in router.routes if route.path == "/catalog")


def test_marketplace_extension_is_seeded_without_agent_mutation_surface() -> None:
    # Reconcile required extensions like app startup does so the marketplace
    # package is seeded before we assert on it.
    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    extensions = extension_store.list_extensions()
    check(
        extension_store.MARKETPLACE_EXTENSION_ID
        in {item["manifest"]["id"] for item in extensions},
        "marketplace is listed in the public extension list",
    )
    hidden_extensions = extension_store.list_extensions(include_hidden=True)
    check(
        extension_store.MARKETPLACE_EXTENSION_ID
        in {item["manifest"]["id"] for item in hidden_extensions},
        "marketplace is available to settings extension list",
    )
    record = extension_store.get_extension(extension_store.MARKETPLACE_EXTENSION_ID)
    check(record is not None, "marketplace extension record is installed")
    check(record["enabled"] is True, "marketplace extension is enabled")
    check(
        record["source"]["type"] == "better_agent_bundled",
        "marketplace extension seeds from bundled package",
    )
    check(
        record["manifest"]["entrypoints"]["mcp"] == [],
        "marketplace exposes no agent-side mutation surface",
    )


def test_marketplace_catalog_lists_bundled_extensions_via_real_route() -> None:
    # Reconcile like app startup does, then drive the real catalog() HTTP
    # route through the marketplace extension's actual persisted `source`
    # dict — not a hand-rolled fake — to prove the local-dev catalog branch
    # a normal bundled install falls into is reachable, not dead code.
    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    record = extension_store.get_extension(extension_store.MARKETPLACE_EXTENSION_ID)
    check(record is not None, "marketplace extension record exists for catalog test")
    source = record["source"]
    check(
        source["type"] == "better_agent_bundled",
        "marketplace extension is a bundled install",
    )
    check(
        bool(source.get("repo_url")),
        "bundled install records a non-empty source.repo_url",
    )
    repo_root = Path(source["repo_url"])
    check(
        (repo_root / "extensions").is_dir(),
        f"source.repo_url points at a repo checkout with an extensions dir: {repo_root}",
    )

    module = _load_marketplace_routes_module()
    router = _marketplace_router(module, source)
    catalog = asyncio.run(_catalog_endpoint(router)(q=""))
    rows = catalog["extensions"]
    check(len(rows) > 0, "real catalog route lists bundled extensions on a fresh install")
    check(all(row.get("id") for row in rows), "every listed catalog row has an extension id")
    check(
        extension_store.MARKETPLACE_EXTENSION_ID in {row["id"] for row in rows},
        "marketplace lists itself in the local-dev catalog",
    )


def test_marketplace_catalog_self_heals_missing_repo_url_on_reconcile() -> None:
    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    with extension_store._store_lock():
        data = extension_store._read_store_unlocked()
        data["extensions"][extension_store.MARKETPLACE_EXTENSION_ID]["source"][
            "repo_url"
        ] = ""
        extension_store._write_store_unlocked(data)

    corrupted = extension_store.get_extension(extension_store.MARKETPLACE_EXTENSION_ID)
    check(
        corrupted["source"]["repo_url"] == "",
        "test setup reproduces a pre-fix install with an empty source.repo_url",
    )

    module = _load_marketplace_routes_module()
    router = _marketplace_router(module, corrupted["source"])
    try:
        asyncio.run(_catalog_endpoint(router)(q=""))
    except HTTPException as exc:
        check(
            exc.status_code == 500,
            "empty source.repo_url reproduces the reported catalog 500",
        )
    else:
        raise AssertionError("catalog should fail with an empty source.repo_url")

    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    healed = extension_store.get_extension(extension_store.MARKETPLACE_EXTENSION_ID)
    check(
        bool(healed["source"]["repo_url"]),
        "next reconcile self-heals a bundled install missing source.repo_url",
    )

    router = _marketplace_router(module, healed["source"])
    catalog = asyncio.run(_catalog_endpoint(router)(q=""))
    check(
        len(catalog["extensions"]) > 0,
        "catalog route succeeds again after self-heal",
    )


def test_marketplace_backend_loads_inside_isolated_extension_host() -> None:
    import extension_backend_loader

    status, body = extension_backend_loader.invoke_extension_backend_sync(
        extension_store.MARKETPLACE_EXTENSION_ID,
        "health",
        method="GET",
        base_url="http://localhost:8000",
    )
    extension_backend_loader.shutdown_persistent_backends()

    check(status == 200, f"marketplace backend loads in isolated host: {body!r}")


def test_marketplace_backend_auth_readiness_never_exposes_token() -> None:
    module = _load_marketplace_routes_module()

    catalog_tokens: list[str] = []
    module._access_token = lambda: "core-owned-access"
    module._ofekdev_catalog = lambda token: (
        catalog_tokens.append(token) or [{"id": "ofek.adv"}],
        "default-v1:1:" + "a" * 64,
    )

    router = _marketplace_router(module, {"type": "marketplace"})
    catalog_endpoint = _catalog_endpoint(router)
    catalog = asyncio.run(catalog_endpoint(q=""))
    check(
        catalog["extensions"] == [{"id": "ofek.adv"}],
        "catalog transport remains available",
    )
    check(catalog_tokens == ["core-owned-access"], "catalog uses backend-held auth")

    ready_endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/auth/protocol-ready"
    )
    ready = asyncio.run(ready_endpoint())
    check(ready == {"authenticated": True}, "core receives only auth readiness")
    check(
        "core-owned-access" not in json.dumps(ready),
        "auth readiness excludes credentials",
    )
    check(
        all("/protocol/" not in route.path for route in router.routes),
        "extension backend exposes no device protocol routes",
    )


def test_marketplace_catalog_search_uses_static_catalog_and_filters_locally() -> None:
    old_base = os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL")
    os.environ["BETTER_AGENT_MARKETPLACE_BASE_URL"] = (
        "https://marketplace.test/api/marketplace"
    )
    calls: list[str] = []
    original_fetch = extension_store._fetch_json

    def fake_fetch(url: str):
        calls.append(url)
        return {
            "extensions": [
                {"id": "ofek.alpha", "name": "Alpha", "description": "Notes"},
                {"id": "ofek.todos", "name": "Todos", "description": "Task tracking"},
                {
                    "id": "ofek.todos-pro",
                    "name": "Todos Pro",
                    "description": "Advanced tasks",
                },
            ]
        }

    extension_store._fetch_json = fake_fetch
    try:
        result = extension_store.search_marketplace_catalog(query="todos", limit=1)
    finally:
        extension_store._fetch_json = original_fetch
        if old_base is None:
            os.environ.pop("BETTER_AGENT_MARKETPLACE_BASE_URL", None)
        else:
            os.environ["BETTER_AGENT_MARKETPLACE_BASE_URL"] = old_base

    check(
        calls == ["https://marketplace.test/api/marketplace/extensions.json"],
        "marketplace catalog fetch uses static extensions.json",
    )
    check(
        [item["id"] for item in result["extensions"]] == ["ofek.todos"],
        "marketplace catalog search filters and limits locally",
    )


def test_internal_marketplace_endpoint_requires_marketplace_extension_and_valid_actions() -> (
    None
):
    import extension_token_registry
    import main
    from fastapi.testclient import TestClient

    client = TestClient(
        main.app,
        client=("127.0.0.1", 50000),
        base_url="http://localhost:8000",
    )
    # Identity is token-derived: act as an extension by sending ITS minted token.
    other_token = extension_token_registry.mint("ofek-dev.coordination")
    marketplace_token = extension_token_registry.mint(
        extension_store.MARKETPLACE_EXTENSION_ID
    )

    response = client.post(
        "/api/internal/marketplace",
        headers={"X-Internal-Token": other_token},
        json={"action": "list_installed"},
    )
    check(response.status_code == 403, "marketplace endpoint rejects other extensions")

    response = client.post(
        "/api/internal/marketplace",
        headers={"X-Internal-Token": marketplace_token},
        json={"action": "list_installed"},
    )
    check(
        response.status_code == 200, "marketplace endpoint lists installed extensions"
    )
    check(
        isinstance(response.json().get("extensions"), list),
        "marketplace list returns extensions",
    )

    response = client.post(
        "/api/internal/marketplace",
        headers={"X-Internal-Token": marketplace_token},
        json={
            "action": "set_enabled",
            "extension_id": "ofek-dev.ask",
            "enabled": "yes",
        },
    )
    check(response.status_code == 400, "marketplace set_enabled requires boolean")

    response = client.post(
        "/api/internal/marketplace",
        headers={"X-Internal-Token": marketplace_token},
        json={"action": "unknown"},
    )
    check(response.status_code == 400, "marketplace endpoint rejects unknown actions")

    import extension_api
    from pydantic import ValidationError

    try:
        extension_api.MarketplaceInstallRequest.model_validate(
            {"repo_url": "https://attacker.example/repo.git"}
        )
    except ValidationError:
        pass
    else:
        raise AssertionError(
            "semantic marketplace install accepts generic install coordinates"
        )
    check(True, "semantic marketplace install rejects generic install coordinates")

    try:
        extension_store.set_enabled(
            extension_store.MARKETPLACE_EXTENSION_ID,
            False,
            required_source_type="marketplace",
        )
    except extension_store.ExtensionError:
        pass
    else:
        raise AssertionError("marketplace management accepts non-marketplace sources")
    check(True, "marketplace management rejects non-marketplace sources")


def test_marketplace_auth_secret_capabilities_are_scoped() -> None:
    import extension_token_registry
    import main
    import marketplace_auth
    import password_manager
    from fastapi.testclient import TestClient

    values: dict[tuple[str, str], str] = {
        ("other-service", "oauth-session"): "hidden",
    }
    original_list = password_manager.list_service_passwords
    original_get = password_manager.get_service_password
    original_store = password_manager.store_service_password
    original_delete = password_manager.delete_service_password

    def fake_list() -> dict[str, list[dict[str, str]]]:
        return {
            "items": [
                {"service": service, "account": account} for service, account in values
            ]
        }

    def fake_get(service: str, account: str) -> str:
        try:
            return values[(service, account)]
        except KeyError as exc:
            raise password_manager.PasswordManagerError("missing") from exc

    def fake_store(payload: dict[str, str]) -> dict[str, str]:
        values[(payload["service"], payload["account"])] = payload["password"]
        return {"service": payload["service"], "account": payload["account"]}

    def fake_delete(payload: dict[str, str]) -> dict[str, str]:
        if values.pop((payload["service"], payload["account"]), None) is None:
            raise password_manager.PasswordManagerError("missing")
        return {"service": payload["service"], "account": payload["account"]}

    password_manager.list_service_passwords = fake_list
    password_manager.get_service_password = fake_get
    password_manager.store_service_password = fake_store
    password_manager.delete_service_password = fake_delete
    try:
        client = TestClient(
            main.app, client=("127.0.0.1", 50000), base_url="http://localhost:8000"
        )
        marketplace_token = extension_token_registry.mint(
            extension_store.MARKETPLACE_EXTENSION_ID
        )
        other_token = extension_token_registry.mint("ofek-dev.coordination")
        endpoint = "/api/internal/capabilities/invoke"

        def invoke(token: str, action: str, payload: dict | None = None):
            return client.post(
                endpoint,
                headers={"X-Internal-Token": token},
                json={
                    "capability": "marketplace",
                    "action": action,
                    "payload": payload or {},
                },
            )

        response = invoke(other_token, "auth-secret.list")
        check(
            response.status_code == 403,
            "marketplace auth secrets reject other extension tokens",
        )

        response = invoke(
            marketplace_token,
            "auth-secret.store",
            {"account": "oauth-session", "value": {"access_token": "secret"}},
        )
        check(response.status_code == 200, "marketplace token stores its auth secret")
        check(
            (marketplace_auth.service_name(), "oauth-session") in values,
            "marketplace auth secrets use the home-scoped service namespace",
        )

        response = invoke(
            marketplace_token,
            "auth-secret.store",
            {"account": "arbitrary-account", "value": {}},
        )
        check(
            response.status_code == 422,
            "marketplace auth secrets reject arbitrary accounts",
        )

        response = invoke(
            marketplace_token, "auth-secret.get", {"account": "oauth-session"}
        )
        check(
            response.json() == {"value": {"access_token": "secret"}},
            "marketplace reads only its scoped secret",
        )

        response = invoke(marketplace_token, "auth-secret.list")
        check(
            response.json() == {"items": [{"account": "oauth-session"}]},
            "marketplace secret listing excludes other services",
        )

        response = invoke(
            marketplace_token, "auth-secret.delete", {"account": "oauth-session"}
        )
        check(
            response.json() == {"deleted": True},
            "marketplace deletes its scoped secret",
        )
    finally:
        password_manager.list_service_passwords = original_list
        password_manager.get_service_password = original_get
        password_manager.store_service_password = original_store
        password_manager.delete_service_password = original_delete


def test_marketplace_service_uses_authenticated_backend_and_validates_uninstall_source() -> (
    None
):
    import marketplace_service
    from fastapi.responses import JSONResponse

    calls: list[tuple[str, str, str]] = []
    original_invoke = (
        marketplace_service.extension_backend_loader.invoke_extension_backend
    )
    original_prepare = extension_store.prepare_marketplace_install

    async def fake_invoke(
        extension_id: str, path: str, *, method: str = "POST", **_kwargs
    ):
        calls.append((extension_id, path, method))
        return JSONResponse(
            {
                "extension_id": "ofek.adv",
                "version": "1.0.0",
                "artifact_url": "https://marketplace.test/ofek.adv.tar.gz",
                "artifact_sha256": "0" * 64,
                "signature": "signed",
                "signature_alg": "ed25519",
            }
        )

    def fake_prepare(extension_id: str, metadata: dict) -> dict:
        check(
            extension_id == metadata["extension_id"],
            "authenticated metadata keeps extension identity",
        )
        return {"manifest": {"id": extension_id}, "preview_token": "a" * 32}

    marketplace_service.extension_backend_loader.invoke_extension_backend = fake_invoke
    extension_store.prepare_marketplace_install = fake_prepare
    try:
        prepared = asyncio.run(marketplace_service.prepare_install("ofek.adv"))
    finally:
        marketplace_service.extension_backend_loader.invoke_extension_backend = (
            original_invoke
        )
        extension_store.prepare_marketplace_install = original_prepare
    check(
        prepared["preview_token"] == "a" * 32,
        "authenticated metadata produces preview token",
    )
    check(
        calls
        == [(extension_store.MARKETPLACE_EXTENSION_ID, "metadata/ofek.adv", "GET")],
        "marketplace preview retrieves metadata through authenticated extension backend",
    )

    calls.clear()
    marketplace_service.extension_backend_loader.invoke_extension_backend = fake_invoke
    try:
        try:
            asyncio.run(
                marketplace_service.uninstall(extension_store.MARKETPLACE_EXTENSION_ID)
            )
        except extension_store.ExtensionError:
            pass
        else:
            raise AssertionError("invalid-source marketplace uninstall was accepted")
    finally:
        marketplace_service.extension_backend_loader.invoke_extension_backend = (
            original_invoke
        )
    check(calls == [], "invalid-source uninstall never calls marketplace backend")


def main() -> int:
    try:
        test_marketplace_extension_is_seeded_without_agent_mutation_surface()
        test_marketplace_catalog_lists_bundled_extensions_via_real_route()
        test_marketplace_catalog_self_heals_missing_repo_url_on_reconcile()
        test_marketplace_backend_loads_inside_isolated_extension_host()
        test_marketplace_backend_auth_readiness_never_exposes_token()
        test_marketplace_catalog_search_uses_static_catalog_and_filters_locally()
        test_internal_marketplace_endpoint_requires_marketplace_extension_and_valid_actions()
        test_marketplace_auth_secret_capabilities_are_scoped()
        test_marketplace_service_uses_authenticated_backend_and_validates_uninstall_source()
    finally:
        if created_dist:
            index = dist_dir / "index.html"
            if (
                index.exists()
                and index.read_text(encoding="utf-8")
                == "<!doctype html><title>stub</title>"
            ):
                index.unlink()
            try:
                dist_dir.rmdir()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
