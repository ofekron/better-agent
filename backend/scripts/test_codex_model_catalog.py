from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

_test_home.isolate("bc-test-codex-model-catalog-")

import models  # noqa: E402
import model_catalog_read_projection  # noqa: E402
from model_catalog_refresh_state import (  # noqa: E402
    CatalogProjection,
    changed_fact,
)
import config_store  # noqa: E402


def test_fresh_codex_default_uses_cli_visible_model():
    codex_provider = next(
        p for p in config_store._seed_default_state()["providers"]
        if p["kind"] == "codex"
    )
    assert codex_provider["default_model"] == "gpt-5.5"


def test_codex_catalog_ignores_legacy_schema2_cache_without_d3_projection():
    provider = {
        "id": "codex-cached",
        "kind": "codex",
        "mode": "subscription",
        "default_model": "gpt-5.6",
    }
    models._update_cache(
        provider["id"],
        models=["gpt-5.5", "local-only-model"],
        retired=[],
        last_fetch_state="ok",
    )

    active, _retired, has_cache, cache = models._read_catalog_models(provider)

    assert has_cache is False
    assert active == []
    assert cache["last_fetch_state"] == "warming"
    assert "gpt-5.6" not in models._models_for(provider)


def test_codex_catalog_reads_d3_projection_only():
    # Fugu is NOT here: its CLI catalog is unverifiable, so it serves the
    # curated FUGU_MODELS constant instead of a D3 projection (locked by
    # test_fugu_curated_catalog.py).
    for kind in ("codex",):
        provider = {
            "id": f"{kind}-projected",
            "generation": "generation",
            "kind": kind,
            "mode": "subscription",
            "default_model": "gpt-5.6",
        }
        model_catalog_read_projection.apply_fact(changed_fact(CatalogProjection(
            provider_id=provider["id"],
            provider_generation=provider["generation"],
            status="current",
            models=("dynamic-only",),
            models_current=True,
            retired=(),
            last_refreshed_at=10.0,
            reason="",
            authority_fingerprint="a" * 64,
        )))

        active, _retired, has_cache, cache = models._read_catalog_models(
            provider,
        )

        assert has_cache is True
        assert active == ["dynamic-only"]
        assert cache["models_current"] is True


if __name__ == "__main__":
    test_fresh_codex_default_uses_cli_visible_model()
    test_codex_catalog_ignores_legacy_schema2_cache_without_d3_projection()
    test_codex_catalog_reads_d3_projection_only()
    print("ok")
