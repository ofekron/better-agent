from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_execution import (  # noqa: E402
    CodexExecutionContract,
    build_codex_execution_contract,
)
from codex_execution_common import ExecutionContractError  # noqa: E402
from codex_execution_contract import codex_authority_paths  # noqa: E402
from scripts.codex_execution_test_support import provider, write_executable  # noqa: E402


def _build_encoded(root: Path, **overrides) -> dict:
    """A fully valid execution-contract payload round-tripped through the builder."""
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        'model_provider = "openai"\n',
        encoding="utf-8",
    )
    executable = root / "codex"
    write_executable(executable, b"native")
    provider_record = provider(config_dir)
    provider_record.update(overrides.pop("provider_overrides", {}))

    contract = build_codex_execution_contract(
        provider_record,
        launcher_path=str(executable),
        **overrides,
    )
    # Canonical serialized form: tuples (e.g. symlink_chain) become lists, as
    # `from_dict` requires. Matches the on-disk/persisted contract shape.
    return json.loads(json.dumps(contract.to_dict()))


def _expect_invalid(mutation: dict) -> None:
    with pytest.raises(ExecutionContractError):
        CodexExecutionContract.from_dict(mutation)


# --- catalog_fingerprint projection (covers the property + its two helpers) ---

def test_catalog_fingerprint_is_a_stable_reduced_projection() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        encoded = _build_encoded(root)
        contract = CodexExecutionContract.from_dict(
            json.loads(json.dumps(encoded)),
        )

        first = contract.catalog_fingerprint
        assert len(first) == 64
        assert all(c in "0123456789abcdef" for c in first)
        # Deterministic across calls.
        assert contract.catalog_fingerprint == first
        # The catalog projection ignores runtime_args, so a runtime-only
        # change moves the full fingerprint but never the catalog one.
        assert contract.fingerprint != contract.catalog_fingerprint


def test_catalog_fingerprint_is_independent_of_runtime_args() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        base = _build_encoded(root)

        config_dir = root / "config"
        executable = root / "codex"
        other = build_codex_execution_contract(
            provider(config_dir),
            launcher_path=str(executable),
            runtime_args=("-c", "features.shell_snapshot=false"),
        ).to_dict()

        base_contract = CodexExecutionContract.from_dict(
            json.loads(json.dumps(base)),
        )
        other_contract = CodexExecutionContract.from_dict(
            json.loads(json.dumps(other)),
        )
        # runtime_args differs but the catalog projection excludes it.
        assert base_contract.catalog_fingerprint == other_contract.catalog_fingerprint
        assert base_contract.fingerprint != other_contract.fingerprint


def test_catalog_fingerprint_ignores_metadata_revision() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        base = CodexExecutionContract.from_dict(_build_encoded(root))
        metadata_only = CodexExecutionContract.from_dict(
            _build_encoded(
                root,
                provider_overrides={"revision": 99},
            )
        )
        assert base.fingerprint != metadata_only.fingerprint
        assert base.catalog_fingerprint == metadata_only.catalog_fingerprint


def test_attest_metadata_is_true_for_an_unmodified_contract() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        encoded = _build_encoded(root)
        contract = CodexExecutionContract.from_dict(
            json.loads(json.dumps(encoded)),
        )
        assert contract.attest_metadata() is True


# --- from_dict defensive validation branches ---

def test_from_dict_rejects_non_dict_launch_chain() -> None:
    with tempfile.TemporaryDirectory() as raw:
        encoded = _build_encoded(Path(raw))
    encoded["launch_chain"] = "not-a-dict"
    _expect_invalid(encoded)


def test_from_dict_rejects_unknown_launch_chain_keys() -> None:
    with tempfile.TemporaryDirectory() as raw:
        encoded = _build_encoded(Path(raw))
    encoded["launch_chain"]["unexpected"] = 1
    _expect_invalid(encoded)


def test_from_dict_rejects_non_list_components() -> None:
    with tempfile.TemporaryDirectory() as raw:
        encoded = _build_encoded(Path(raw))
    encoded["launch_chain"]["components"] = "not-a-list"
    _expect_invalid(encoded)


def test_from_dict_rejects_non_list_sibling_components() -> None:
    with tempfile.TemporaryDirectory() as raw:
        encoded = _build_encoded(Path(raw))
    encoded["launch_chain"]["sibling_components"] = "not-a-list"
    _expect_invalid(encoded)


def test_from_dict_rejects_non_list_component_indexes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        encoded = _build_encoded(Path(raw))
    encoded["launch_chain"]["component_argv_indexes"] = "not-a-list"
    _expect_invalid(encoded)


def test_from_dict_rejects_non_integer_component_indexes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        encoded = _build_encoded(Path(raw))
    encoded["launch_chain"]["component_argv_indexes"] = [0, "x"]
    _expect_invalid(encoded)


def test_from_dict_rejects_non_list_config() -> None:
    with tempfile.TemporaryDirectory() as raw:
        encoded = _build_encoded(Path(raw))
    encoded["config"] = "not-a-list"
    _expect_invalid(encoded)


def test_from_dict_rejects_malformed_environment_selectors() -> None:
    with tempfile.TemporaryDirectory() as raw:
        encoded = _build_encoded(Path(raw))
    encoded["environment_selectors"] = "not-a-list"
    _expect_invalid(encoded)

    bad_shape = json.loads(json.dumps(encoded))
    bad_shape["environment_selectors"] = [["only-one"]]
    _expect_invalid(bad_shape)

    bad_value = json.loads(json.dumps(encoded))
    bad_value["environment_selectors"] = [[1, 2]]
    _expect_invalid(bad_value)


def test_from_dict_rejects_genuine_fingerprint_mismatch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        encoded = _build_encoded(Path(raw))
    # A clean structural change that passes every semantic gate but makes
    # the recomputed fingerprint disagree with the supplied one.
    encoded["base_url"] = "https://other.example.test/v1"
    _expect_invalid(encoded)


# --- _clean_environment_selectors branches (via builder) ---

def test_clean_environment_selectors_accepts_allowed_and_rejects_others() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")

        provider_record = provider(config_dir)
        contract = build_codex_execution_contract(
            provider_record,
            launcher_path=str(executable),
            environment_selectors={"CODEX_HOME": str(root)},
        )
        assert contract.environment_selectors == (("CODEX_HOME", str(root)),)

        with pytest.raises(ExecutionContractError):
            build_codex_execution_contract(
                provider_record,
                launcher_path=str(executable),
                environment_selectors={"PATH": "/usr/bin"},
            )


# --- _clean_config_args branches (via builder) ---

def test_clean_config_args_rejects_non_sequence() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")

        with pytest.raises(ExecutionContractError):
            build_codex_execution_contract(
                provider(config_dir),
                launcher_path=str(executable),
                catalog_args="not-a-sequence",
            )


def test_clean_config_args_rejects_odd_and_non_string_arguments() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")

        provider_record = provider(config_dir)
        with pytest.raises(ExecutionContractError):
            build_codex_execution_contract(
                provider_record,
                launcher_path=str(executable),
                catalog_args=["-c"],
            )
        with pytest.raises(ExecutionContractError):
            build_codex_execution_contract(
                provider_record,
                launcher_path=str(executable),
                catalog_args=["-c", 1],
            )


def test_clean_config_args_rejects_disallowed_key() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")

        with pytest.raises(ExecutionContractError):
            build_codex_execution_contract(
                provider(config_dir),
                launcher_path=str(executable),
                catalog_args=("-c", "unknown_key=value"),
            )


def test_clean_config_args_rejects_secret_in_non_exclude_override() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")

        with pytest.raises(ExecutionContractError):
            build_codex_execution_contract(
                provider(config_dir),
                launcher_path=str(executable),
                runtime_args=("-c", "model=api_key_leak"),
            )


def test_clean_config_args_allows_secret_named_exclude_override() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")

        # shell_environment_policy.exclude legitimately names a secret to drop.
        contract = build_codex_execution_contract(
            provider(config_dir),
            launcher_path=str(executable),
            runtime_args=(
                "-c",
                "shell_environment_policy.exclude=API_KEY",
            ),
        )
        assert contract.runtime_args == (
            "-c",
            "shell_environment_policy.exclude=API_KEY",
        )


# --- _clean_base_url success branch (clean non-empty URL) ---

def test_clean_base_url_returns_a_clean_non_empty_url() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")

        clean = "https://example.test/v1"
        contract = build_codex_execution_contract(
            {**provider(config_dir), "base_url": clean},
            launcher_path=str(executable),
        )
        assert contract.base_url == clean


# --- build_codex_execution_contract authority/validation branches ---

def test_build_rejects_incomplete_provider_authority() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")

        for bad in (
            {**provider(config_dir), "id": ""},
            {**provider(config_dir), "kind": "openai"},
            {**provider(config_dir), "generation": ""},
        ):
            with pytest.raises(ExecutionContractError):
                build_codex_execution_contract(
                    bad,
                    launcher_path=str(executable),
                )


def test_build_rejects_invalid_provider_revision() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")

        for revision in ("7", -1):
            for key in ("revision", "execution_revision"):
                with pytest.raises(ExecutionContractError):
                    build_codex_execution_contract(
                        {**provider(config_dir), key: revision},
                        launcher_path=str(executable),
                    )


def test_build_rejects_invalid_profile() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")

        with pytest.raises(ExecutionContractError):
            build_codex_execution_contract(
                provider(config_dir),
                launcher_path=str(executable),
                profile="has space!",
            )


def test_build_rejects_relative_config_root() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        executable = root / "codex"
        write_executable(executable, b"native")

        with pytest.raises(ExecutionContractError):
            build_codex_execution_contract(
                {**provider(root), "config_dir": "relative/config"},
                launcher_path=str(executable),
            )


def test_build_rejects_unavailable_config_root() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        executable = root / "codex"
        write_executable(executable, b"native")

        with pytest.raises(ExecutionContractError):
            build_codex_execution_contract(
                {**provider(root), "config_dir": str(root / "does-not-exist")},
                launcher_path=str(executable),
            )


# --- _codex_agent_authority_paths symlink branch ---

def test_authority_paths_treats_agents_symlink_as_single_authority() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        real_agents = root / "real-agents"
        real_agents.mkdir()
        (real_agents / "reviewer.toml").write_text("enabled=true", encoding="utf-8")
        config_dir = root / "config"
        config_dir.mkdir()
        agents_link = config_dir / "agents"
        agents_link.symlink_to(real_agents)

        paths = codex_authority_paths(config_dir)
        # A symlinked agents root collapses to the single link target.
        assert str(agents_link) in paths


CONTRACT_BRANCH_TESTS = (
    test_catalog_fingerprint_is_a_stable_reduced_projection,
    test_catalog_fingerprint_is_independent_of_runtime_args,
    test_attest_metadata_is_true_for_an_unmodified_contract,
    test_from_dict_rejects_non_dict_launch_chain,
    test_from_dict_rejects_unknown_launch_chain_keys,
    test_from_dict_rejects_non_list_components,
    test_from_dict_rejects_non_list_sibling_components,
    test_from_dict_rejects_non_list_component_indexes,
    test_from_dict_rejects_non_integer_component_indexes,
    test_from_dict_rejects_non_list_config,
    test_from_dict_rejects_malformed_environment_selectors,
    test_from_dict_rejects_genuine_fingerprint_mismatch,
    test_clean_environment_selectors_accepts_allowed_and_rejects_others,
    test_clean_config_args_rejects_non_sequence,
    test_clean_config_args_rejects_odd_and_non_string_arguments,
    test_clean_config_args_rejects_disallowed_key,
    test_clean_config_args_rejects_secret_in_non_exclude_override,
    test_clean_config_args_allows_secret_named_exclude_override,
    test_clean_base_url_returns_a_clean_non_empty_url,
    test_build_rejects_incomplete_provider_authority,
    test_build_rejects_invalid_provider_revision,
    test_build_rejects_invalid_profile,
    test_build_rejects_relative_config_root,
    test_build_rejects_unavailable_config_root,
    test_authority_paths_treats_agents_symlink_as_single_authority,
)


if __name__ == "__main__":
    for test in CONTRACT_BRANCH_TESTS:
        test()
    print("PASS codex execution contract branches")
