#!/usr/bin/env python3
"""Locks Fugu's curated model catalog.

Fugu's CLI catalog is unverifiable (codex_model_discovery rejects it
with `fugu_catalog_unverifiable`), so its catalog authority is the
bundled `FUGU_MODELS` constant: model listing serves it via the generic
cache/static path, headless model validation checks against it, and
spawn admission does not demand a D3 catalog that can never exist.
Before this, every fugu session create failed with
"Fugu has no known models".
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _test_home

_test_home.isolate(prefix="fugu-catalog-")

import models as models_mod  # noqa: E402
from codex_headless_execution import _require_model  # noqa: E402
from model_execution_admission import _CATALOG_KINDS  # noqa: E402
from provider_fugu import FUGU_MODELS  # noqa: E402

RECORD = {
    "id": "fugu-test-provider",
    "kind": "fugu",
    "generation": "5c0e8c1e-2d34-4a1e-9c55-7f6b1a2d3e4f",
    "revision": 0,
    "mode": "api_key",
    "default_model": "fugu",
}


def test_models_projection_serves_curated_catalog() -> None:
    models = models_mod._models_for(dict(RECORD))
    for model in FUGU_MODELS:
        assert model in models, (model, models)


def test_headless_model_validation_uses_curated_catalog() -> None:
    _require_model(dict(RECORD), "fugu")
    try:
        _require_model(dict(RECORD), "not-a-fugu-model")
    except ValueError:
        return
    raise AssertionError("expected unknown fugu model rejection")


def test_admission_does_not_demand_unobtainable_catalog() -> None:
    assert "fugu" not in _CATALOG_KINDS
    assert "codex" in _CATALOG_KINDS


def test_admission_skips_catalog_only_for_delegated_family_contract() -> None:
    from types import SimpleNamespace

    from model_execution_admission import _delegated_family_contract

    def artifact(contract):
        return SimpleNamespace(provider_kind="codex", provider_contract=contract)

    assert _delegated_family_contract(
        artifact({"type": "openai", "contract": {}}),
    )
    # Native codex, missing, or unrelated contracts keep the gate closed.
    assert not _delegated_family_contract(artifact({"type": "codex"}))
    assert not _delegated_family_contract(artifact(None))
    assert not _delegated_family_contract(artifact({"type": "claude"}))


TESTS = (
    test_models_projection_serves_curated_catalog,
    test_headless_model_validation_uses_curated_catalog,
    test_admission_does_not_demand_unobtainable_catalog,
    test_admission_skips_catalog_only_for_delegated_family_contract,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print("all fugu curated catalog tests passed")
