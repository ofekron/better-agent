from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402


TEST_HOME = _test_home.TestHome.acquire("ba-test-catalog-runtime-projection-")

import config_store  # noqa: E402
import model_catalog_refresh  # noqa: E402
import models  # noqa: E402
from scripts.codex_model_discovery_test_support import (  # noqa: E402
    codex_on_path,
    provider,
    write_executable,
)


async def _next_projection(provider_id: str, predicate) -> None:
    async with asyncio.timeout(5):
        while True:
            projection = model_catalog_refresh.catalog_refresh_owner.snapshot(
                provider_id,
            )
            if projection is not None and predicate(projection):
                return
            await asyncio.sleep(0)


async def _scenario(root: Path) -> None:
    executable = root / "bin" / "codex"
    record = provider(root)
    provider_id = record["id"]
    dynamic_model = "dynamic-only-runtime-model"
    write_executable(
        executable,
        {"models": [{"slug": dynamic_model, "visibility": "list"}]},
    )

    cold = models.models_catalog(provider_id)
    assert cold["models"] == []
    assert cold["last_fetch_state"] == "warming"
    assert cold["catalog_status"] == "pending"
    assert not cold["models_current"]

    with codex_on_path(executable):
        await model_catalog_refresh.start()
        await model_catalog_refresh.catalog_refresh_owner.wait_ready()
        current = models.models_catalog(provider_id)
        assert current["models"] == [dynamic_model]
        assert models.available_models(provider_id) == [dynamic_model]
        assert current["last_fetch_state"] == "ok"
        assert current["catalog_status"] == "current"
        assert current["models_current"]
        assert current["last_refreshed_at"] > 0

        replacement = root / "bin" / "codex.next"
        write_executable(
            replacement,
            {"models": []},
            exit_code=9,
        )
        os.replace(replacement, executable)
        await _next_projection(
            provider_id,
            lambda projection: projection.status == "error",
        )

        stale = models.models_catalog(provider_id)
        assert stale["models"] == [dynamic_model]
        assert stale["last_fetch_state"] == "failing"
        assert stale["catalog_status"] == "error"
        assert not stale["models_current"]
        assert stale["last_refreshed_at"] == current["last_refreshed_at"]

    await model_catalog_refresh.shutdown()


def test_fugu_cold_start_does_not_project_configured_default() -> None:
    from provider_fugu import FUGU_MODELS

    record = provider(Path(TEST_HOME.path), kind="fugu")
    config_store.update_provider(
        record["id"], {"default_model": "fugu-configured-default"}
    )
    catalog = models.models_catalog(record["id"])
    assert catalog["models"] == list(FUGU_MODELS)
    assert "fugu-configured-default" not in catalog["models"]
    assert models.available_models(record["id"]) == list(FUGU_MODELS)


def test_runtime_reads_project_d3_dynamic_and_lkg_catalogs() -> None:
    with tempfile.TemporaryDirectory() as raw:
        asyncio.run(_scenario(Path(raw)))


if __name__ == "__main__":
    test_fugu_cold_start_does_not_project_configured_default()
    test_runtime_reads_project_d3_dynamic_and_lkg_catalogs()
    print("PASS: D3 runtime catalog read projection")
