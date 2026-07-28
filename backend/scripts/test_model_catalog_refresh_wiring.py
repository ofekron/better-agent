from __future__ import annotations

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


def test_backend_lifecycle_owns_catalog_refresh_tasks() -> None:
    source = (BACKEND / "main.py").read_text(encoding="utf-8")
    startup = source.split("async def on_startup():", 1)[1].split(
        "async def on_shutdown():",
        1,
    )[0]
    shutdown = source.split("async def on_shutdown():", 1)[1]

    assert "await model_catalog_refresh.start()" in startup
    assert "await model_catalog_refresh.shutdown()" in shutdown


def test_legacy_due_refresh_excludes_catalog_authority_owner() -> None:
    state = config_store.list_providers()
    provider_id = state["providers"][-1]["id"]

    assert provider_id not in models._due_provider_ids(0)


if __name__ == "__main__":
    test_provider_mutation_and_auth_completion_publish_dirty_facts()
    test_backend_lifecycle_owns_catalog_refresh_tasks()
    test_legacy_due_refresh_excludes_catalog_authority_owner()
    print("PASS: model catalog refresh lifecycle wiring")
