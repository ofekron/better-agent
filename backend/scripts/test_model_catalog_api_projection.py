#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


TEST_HOME = tempfile.mkdtemp(prefix="ba-test-model-catalog-api-")
os.environ["BETTER_AGENT_HOME"] = TEST_HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _test_routes import walk_routes  # noqa: E402
import config_store  # noqa: E402
import main  # noqa: E402
import providers_api  # noqa: E402
import model_catalog_read_projection  # noqa: E402
import model_catalog_refresh  # noqa: E402
from model_catalog_cache import RetiredModel  # noqa: E402
from model_catalog_refresh_state import CatalogProjection, changed_fact  # noqa: E402


def _provider() -> dict:
    return config_store.add_provider(
        {
            "name": "Codex API projection",
            "kind": "codex",
            "mode": "subscription",
            "base_url": "",
            "config_dir": "",
            "default_model": "",
            "custom_models": [],
        },
    )


def _project(provider: dict, *, status: str = "stale") -> CatalogProjection:
    projection = CatalogProjection(
        provider_id=provider["id"],
        provider_generation=provider["generation"],
        status=status,
        models=("gpt-current", "gpt-lkg"),
        models_current=status == "current",
        retired=(RetiredModel(id="gpt-retired", first_absent_at=42.0),),
        last_refreshed_at=100.0,
        reason="" if status == "current" else "source_changed",
        authority_fingerprint="sha256:authority",
    )
    model_catalog_read_projection.apply_fact(changed_fact(projection))
    return projection


def _codex_provider() -> dict:
    return next(
        provider
        for provider in config_store.list_providers()["providers"]
        if provider["kind"] == "codex"
    )


def test_provider_projection_contract() -> None:
    provider = _provider()
    _project(provider)
    payload = providers_api.provider_models_catalog(provider["id"])

    assert payload["provider_id"] == provider["id"]
    assert payload["provider_generation"] == provider["generation"]
    assert payload["status"] == "stale"
    assert payload["models_current"] is False
    assert payload["reason"] == "source_changed"
    assert payload["authority_fingerprint"] == "sha256:authority"
    assert payload["models"] == ["gpt-current", "gpt-lkg"]
    assert payload["last_known_good"] == {
        "models": ["gpt-current", "gpt-lkg"],
        "last_refreshed_at": 100.0,
    }
    assert payload["retired_models"] == [
        {"id": "gpt-retired", "first_absent_at": 42.0},
    ]


def test_projection_fact_broadcasts_authority_and_invalidates() -> None:
    provider = _codex_provider()
    projection = _project(provider, status="refreshing")
    captured: list[tuple[str, dict]] = []
    # providers_api captures the broadcaster at wire time, so intercept the
    # injected callable rather than the coordinator attribute.
    original = providers_api._broadcast_global

    async def capture(event_type: str, data: dict) -> None:
        captured.append((event_type, data))

    providers_api._broadcast_global = capture
    try:
        asyncio.run(providers_api.broadcast_model_catalog_fact(changed_fact(projection)))
    finally:
        providers_api._broadcast_global = original

    assert captured == [
        (
            "models_catalog_changed",
            {
                "provider_id": provider["id"],
                "kind": "catalog_changed",
                "idempotency_key": changed_fact(projection).idempotency_key,
                "projection": projection.to_dict(),
            },
        ),
    ]


def test_refresh_endpoint_acknowledges_without_waiting_for_discovery() -> None:
    provider = _codex_provider()
    scheduled: list[str] = []
    original = model_catalog_refresh.request_refresh_background

    def schedule(provider_id: str) -> bool:
        scheduled.append(provider_id)
        return True

    model_catalog_refresh.request_refresh_background = schedule
    try:
        response = asyncio.run(
            providers_api.refresh_provider_models_endpoint(provider["id"]),
        )
    finally:
        model_catalog_refresh.request_refresh_background = original

    assert scheduled == [provider["id"]]
    assert response["accepted"] is True
    assert response["provider_id"] == provider["id"]
    assert response["provider_generation"] == provider["generation"]
    assert response["catalog"]["status"] in {
        "pending",
        "refreshing",
        "stale",
        "current",
        "error",
        "unsupported",
        "unavailable",
    }
    route = next(
        route
        for route in walk_routes(main.app.routes)
        if getattr(route, "path", "")
        == "/api/providers/{provider_id}/models/refresh"
    )
    assert route.status_code == 202


def test_catalog_list_is_backend_owned() -> None:
    provider = _codex_provider()
    payload = asyncio.run(providers_api.get_provider_model_catalogs())
    assert providers_api.provider_models_catalog(provider["id"]) in payload["catalogs"]
    assert all(
        catalog["authoritative"] is True
        for catalog in payload["catalogs"]
    )


if __name__ == "__main__":
    try:
        test_provider_projection_contract()
        test_projection_fact_broadcasts_authority_and_invalidates()
        test_refresh_endpoint_acknowledges_without_waiting_for_discovery()
        test_catalog_list_is_backend_owned()
        print("PASS model catalog REST and WS projection")
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
