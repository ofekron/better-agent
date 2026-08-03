from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from codex_execution import build_codex_execution_contract  # noqa: E402
from codex_execution_common import canonical_json  # noqa: E402
from model_catalog_authority import (  # noqa: E402
    CatalogAuthority,
    CatalogAuthorityError,
    build_catalog_authority,
)
from scripts.codex_execution_test_support import write_executable  # noqa: E402


PROVIDER_GENERATION = "9b5a6f36-d44c-4c3c-b54f-39554003065d"
STATE_GENERATION = "778b9bea-b654-4e4b-8799-496d34445062"


def _state_authority(revision: int = 11) -> dict:
    return {
        "generation": STATE_GENERATION,
        "revision": revision,
        "digest": "a" * 64,
    }


def _build_authority(root: Path) -> CatalogAuthority:
    config_dir = root / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('profile = "work"\n', encoding="utf-8")
    executable = root / "runtime" / "codex"
    write_executable(executable, b"native")
    provider = {
        "id": "codex-provider",
        "kind": "codex",
        "generation": PROVIDER_GENERATION,
        "revision": 7,
        "config_dir": str(config_dir),
        "base_url": "",
        "mode": "subscription",
        "api_key": "must-never-persist",
    }
    contract = build_codex_execution_contract(
        provider,
        launcher_path=str(executable),
        profile="work",
        catalog_args=("-c", 'model_provider="openai"'),
    )
    return build_catalog_authority(
        provider_id=provider["id"],
        provider_generation=provider["generation"],
        provider_revision=provider["revision"],
        provider_state_authority=_state_authority(),
        execution_contract=contract,
        discovery_method="codex.debug_models",
        discovery_version=1,
    )


def _recompute_authority_fingerprint(raw: dict) -> None:
    payload = {k: v for k, v in raw.items() if k != "fingerprint"}
    raw["fingerprint"] = hashlib.sha256(canonical_json(payload)).hexdigest()


@pytest.fixture(scope="module")
def authority() -> CatalogAuthority:
    with tempfile.TemporaryDirectory() as raw:
        return _build_authority(Path(raw))


def test_round_trip_reconstructs_equal_authority(authority: CatalogAuthority) -> None:
    assert CatalogAuthority.from_dict(authority.to_dict()) == authority
    assert "fingerprint" not in authority.to_dict(include_fingerprint=False)


def test_build_catalog_authority_rejects_non_dict_state(authority: CatalogAuthority) -> None:
    with pytest.raises(CatalogAuthorityError, match="invalid catalog authority"):
        build_catalog_authority(
            provider_id=authority.provider_id,
            provider_generation=authority.provider_generation,
            provider_revision=authority.provider_revision,
            provider_state_authority="not-a-dict",  # type: ignore[arg-type]
            execution_contract=authority.execution_contract,
            discovery_method=authority.discovery_method,
            discovery_version=authority.discovery_version,
        )


def test_build_catalog_authority_rejects_wrong_contract_type(authority: CatalogAuthority) -> None:
    with pytest.raises(CatalogAuthorityError, match="invalid catalog authority"):
        build_catalog_authority(
            provider_id=authority.provider_id,
            provider_generation=authority.provider_generation,
            provider_revision=authority.provider_revision,
            provider_state_authority=_state_authority(),
            execution_contract="not-a-contract",  # type: ignore[arg-type]
            discovery_method=authority.discovery_method,
            discovery_version=authority.discovery_version,
        )


def _base_raw(authority: CatalogAuthority) -> dict:
    return copy.deepcopy(authority.to_dict())


# Each entry mutates a deep copy of the valid authority dict so that the named
# validation branch fires. The match string pins the exact error so a different
# (wrong) branch failing the assertion cannot pass the case.
REJECT_MUTATIONS: list[tuple[str, str, object]] = [
    ("provider_extra_key", "invalid provider authority",
     lambda d: d["provider"].__setitem__("extra", 1)),
    ("source_extra_key", "invalid catalog source",
     lambda d: d["source"].__setitem__("extra", 1)),
    ("discovery_extra_key", "invalid discovery identity",
     lambda d: d["discovery"].__setitem__("extra", 1)),
    ("provider_id_empty", "invalid provider id",
     lambda d: d["provider"].__setitem__("id", "")),
    ("provider_id_non_str", "invalid provider id",
     lambda d: d["provider"].__setitem__("id", 5)),
    ("provider_id_too_long", "invalid provider id",
     lambda d: d["provider"].__setitem__("id", "a" * 129)),
    ("provider_id_untrimmed", "invalid provider id",
     lambda d: d["provider"].__setitem__("id", "id ")),
    ("provider_id_control_char", "invalid provider id",
     lambda d: d["provider"].__setitem__("id", "id\x01")),
    ("execution_contract_not_dict", "invalid execution contract",
     lambda d: d["source"].__setitem__("execution_contract", "not-a-dict")),
    ("catalog_fingerprint_mismatch", "catalog source fingerprint mismatch",
     lambda d: d["source"].__setitem__("catalog_fingerprint", "b" * 64)),
    ("discovery_method_non_str", "invalid discovery identity",
     lambda d: d["discovery"].__setitem__("method", 5)),
    ("discovery_method_bad_format", "invalid discovery identity",
     lambda d: d["discovery"].__setitem__("method", "Bad-Method")),
    ("discovery_version_non_int", "invalid discovery identity",
     lambda d: d["discovery"].__setitem__("version", "1")),
    ("discovery_version_below_one", "invalid discovery identity",
     lambda d: d["discovery"].__setitem__("version", 0)),
    ("supplied_fingerprint_non_str", "invalid discovery identity",
     lambda d: d.__setitem__("fingerprint", 5)),
    ("supplied_fingerprint_bad_format", "invalid discovery identity",
     lambda d: d.__setitem__("fingerprint", "xyz")),
    ("supplied_fingerprint_mismatch", "catalog authority fingerprint mismatch",
     lambda d: d.__setitem__("fingerprint", "c" * 64)),
]


@pytest.mark.parametrize(
    "name,expected,mutate", REJECT_MUTATIONS, ids=[m[0] for m in REJECT_MUTATIONS],
)
def test_from_dict_rejects_invalid_shapes(
    authority: CatalogAuthority, name: str, expected: str, mutate: object,
) -> None:
    del name  # only identifies the case; the match string pins the branch
    raw = _base_raw(authority)
    mutate(raw)
    with pytest.raises(CatalogAuthorityError, match=expected):
        CatalogAuthority.from_dict(raw)


def test_from_dict_rejects_runtime_only_arguments(authority: CatalogAuthority) -> None:
    # A genuinely valid contract carrying runtime-only args: catalog_fingerprint
    # is unchanged (it excludes runtime_args), so only the runtime-args guard fires.
    runtime_contract = replace(
        authority.execution_contract,
        runtime_args=("-c", "features.shell_snapshot=false"),
    )
    raw = _base_raw(authority)
    raw["source"]["execution_contract"] = json.loads(
        canonical_json(runtime_contract.to_dict()),
    )
    with pytest.raises(
        CatalogAuthorityError, match="catalog source contains runtime-only arguments",
    ):
        CatalogAuthority.from_dict(raw)


def test_from_dict_rejects_execution_contract_provider_mismatch(
    authority: CatalogAuthority,
) -> None:
    # The execution contract binds a different provider generation than the
    # authority's provider block, so the cross-check after parsing fires.
    mismatched_contract = replace(
        authority.execution_contract,
        provider_generation="00000000-0000-0000-0000-000000000000",
    )
    raw = _base_raw(authority)
    raw["source"]["execution_contract"] = json.loads(
        canonical_json(mismatched_contract.to_dict()),
    )
    raw["source"]["catalog_fingerprint"] = mismatched_contract.catalog_fingerprint
    _recompute_authority_fingerprint(raw)
    with pytest.raises(
        CatalogAuthorityError, match="execution contract provider authority mismatch",
    ):
        CatalogAuthority.from_dict(raw)
