from __future__ import annotations

import asyncio
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402


TEST_HOME = _test_home.TestHome.acquire("ba-test-catalog-wiring-")

import config_store  # noqa: E402
import model_catalog_refresh  # noqa: E402
import models  # noqa: E402
import provider_auth  # noqa: E402


def test_provider_mutation_and_auth_completion_publish_dirty_facts() -> None:
    provider_facts: list[str] = []
    auth_facts: list[str] = []
    original_provider = model_catalog_refresh.notify_provider_state_changed
    original_auth = model_catalog_refresh.notify_provider_auth_changed
    model_catalog_refresh.notify_provider_state_changed = (
        lambda: provider_facts.append("changed")
    )
    model_catalog_refresh.notify_provider_auth_changed = auth_facts.append
    try:
        record = config_store.add_provider({
            "name": "Codex wiring",
            "kind": "codex",
            "mode": "subscription",
            "runner": "native",
        })
        provider_auth._notify_catalog_auth_changed(record["id"])
    finally:
        model_catalog_refresh.notify_provider_state_changed = original_provider
        model_catalog_refresh.notify_provider_auth_changed = original_auth

    assert provider_facts == ["changed"]
    assert auth_facts == [record["id"]]


def test_auth_status_resolution_invalidates_catalog_authority() -> None:
    auth_facts: list[str] = []
    broadcasts: list[str] = []
    record = config_store.add_provider({
        "name": "Codex auth refresh wiring",
        "kind": "codex",
        "mode": "subscription",
        "runner": "native",
    })
    original_status = provider_auth._status_authenticated
    original_auth = model_catalog_refresh.notify_provider_auth_changed
    provider_auth._status_authenticated = lambda _provider: asyncio.sleep(
        0,
        result=True,
    )
    model_catalog_refresh.notify_provider_auth_changed = auth_facts.append
    try:
        asyncio.run(provider_auth.refresh_auth_status(
            record["id"],
            lambda: asyncio.sleep(0, result=broadcasts.append("changed")),
        ))
    finally:
        provider_auth._status_authenticated = original_status
        model_catalog_refresh.notify_provider_auth_changed = original_auth

    assert broadcasts == ["changed"]
    assert auth_facts == [record["id"]]


def test_backend_lifecycle_owns_catalog_refresh_tasks() -> None:
    source = (BACKEND / "app_lifecycle.py").read_text(encoding="utf-8")
    startup = source.split("async def on_startup():", 1)[1].split(
        "async def on_shutdown():",
        1,
    )[0]
    shutdown = source.split("async def on_shutdown():", 1)[1]

    assert "await model_catalog_refresh.start()" in startup
    assert "model_catalog_refresh.subscribe_fact_sink(" in startup
    assert "await model_catalog_refresh.shutdown()" in shutdown
    assert "_model_catalog_unsubscribe()" in shutdown


def test_codex_and_fugu_manual_refresh_use_catalog_authority_owner() -> None:
    # Catalog-authority-owned kinds (codex, fugu) route their manual refresh
    # through model_catalog_refresh instead of models.refresh_one. Proven
    # behaviorally against providers_api._refresh_provider_models so the test
    # survives refactors of the helper's source layout.
    import providers_api

    catalog_refreshed: list[str] = []
    models_refreshed: list[str] = []
    orig_catalog = model_catalog_refresh.request_refresh_background
    orig_models = models.refresh_one
    model_catalog_refresh.request_refresh_background = (
        lambda provider_id: catalog_refreshed.append(provider_id)
    )

    async def _stub_refresh_one(provider_id: str) -> None:
        models_refreshed.append(provider_id)

    models.refresh_one = _stub_refresh_one
    try:
        codex_result = asyncio.run(
            providers_api._refresh_provider_models({"id": "codex-1", "kind": "codex"})
        )
        fugu_result = asyncio.run(
            providers_api._refresh_provider_models({"id": "fugu-1", "kind": "fugu"})
        )
    finally:
        model_catalog_refresh.request_refresh_background = orig_catalog
        models.refresh_one = orig_models

    # codex delegates to the catalog authority owner; fugu is curated (no-op).
    assert codex_result is None
    assert fugu_result is None
    assert catalog_refreshed == ["codex-1"]
    # Neither catalog-authority kind falls through to a direct models refresh.
    assert models_refreshed == []


def test_legacy_due_refresh_excludes_catalog_authority_owner() -> None:
    state = config_store.list_providers()
    provider_id = state["providers"][-1]["id"]

    assert provider_id not in models._due_provider_ids(0)


if __name__ == "__main__":
    test_provider_mutation_and_auth_completion_publish_dirty_facts()
    test_auth_status_resolution_invalidates_catalog_authority()
    test_backend_lifecycle_owns_catalog_refresh_tasks()
    test_codex_and_fugu_manual_refresh_use_catalog_authority_owner()
    test_legacy_due_refresh_excludes_catalog_authority_owner()
    print("PASS: model catalog refresh lifecycle wiring")
