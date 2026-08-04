from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import codex_execution  # noqa: E402
from codex_execution import (  # noqa: E402
    CodexExecutionContract,
    ExecutionContractError,
    build_codex_execution_contract,
)
from codex_execution_contract import codex_authority_paths  # noqa: E402
from scripts.codex_execution_test_support import (  # noqa: E402
    provider,
    write_executable,
)
from scripts.test_codex_execution_launch import LAUNCH_TESTS  # noqa: E402


def test_facade_exports_stable_execution_api() -> None:
    assert codex_execution.__all__ == [
        "CONTRACT_SCHEMA",
        "CodexExecutionContract",
        "ConfigIdentity",
        "ExecutionContractError",
        "FileIdentity",
        "LaunchChain",
        "build_codex_execution_contract",
        "resolve_codex_launch_chain",
    ]


def test_contract_is_deterministic_secret_free_and_config_bound() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text('model_provider = "openai"\n', encoding="utf-8")
        executable = root / "codex"
        write_executable(executable, b"native")

        contract = build_codex_execution_contract(
            provider(config_dir),
            launcher_path=str(executable),
            profile="work",
            catalog_args=("-c", 'model_provider="openai"'),
            runtime_args=("-c", "features.shell_snapshot=false"),
        )
        encoded = contract.to_dict()
        serialized = json.dumps(encoded, sort_keys=True)
        round_tripped = CodexExecutionContract.from_dict(
            json.loads(json.dumps(encoded)),
        )

        assert round_tripped == contract
        assert round_tripped.fingerprint == contract.fingerprint
        assert "must-never-persist" not in serialized
        assert "api_key" not in serialized
        assert contract.attest()

        (config_dir / "unrelated-cache.json").write_text(
            "{}",
            encoding="utf-8",
        )
        assert contract.attest()

        agents_dir = config_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "unexpected.toml").write_text(
            "enabled=true",
            encoding="utf-8",
        )
        assert not contract.attest()

        (agents_dir / "unexpected.toml").unlink()
        agents_dir.rmdir()
        assert contract.attest()

        original_mode = config_dir.stat().st_mode & 0o777
        config_dir.chmod(original_mode ^ 0o010)
        assert not contract.attest()
        config_dir.chmod(original_mode)
        assert contract.attest()

        config_file.write_text('model_provider = "other"\n', encoding="utf-8")
        assert not contract.attest()


def test_config_path_escape_and_secret_selectors_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")
        provider_record = provider(config_dir)

        escaped = config_dir / "escaped.toml"
        escaped.symlink_to(root / "outside.toml")
        (root / "outside.toml").write_text("x=1", encoding="utf-8")
        try:
            build_codex_execution_contract(
                provider_record,
                launcher_path=str(executable),
                config_paths=(str(escaped),),
            )
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("config symlink escape must fail closed")

        try:
            build_codex_execution_contract(
                provider_record,
                launcher_path=str(executable),
                environment_selectors={"SAKANA_API_KEY": "secret"},
            )
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("secret selectors must never enter the contract")


def test_agent_authority_membership_is_frozen() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        agents_dir = config_dir / "agents"
        agents_dir.mkdir(parents=True)
        agent_file = agents_dir / "reviewer.toml"
        agent_file.write_text("enabled=true", encoding="utf-8")
        executable = root / "codex"
        write_executable(executable, b"native")
        contract = build_codex_execution_contract(
            provider(config_dir),
            launcher_path=str(executable),
            config_paths=codex_authority_paths(config_dir.resolve()),
        )

        assert contract.attest()
        agent_file.unlink()
        assert not contract.attest()


def test_secret_arguments_and_url_query_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")
        provider_record = provider(config_dir)
        provider_record["base_url"] = (
            "https://example.test/v1?api_key=TOPSECRET"
        )

        try:
            build_codex_execution_contract(
                provider_record,
                launcher_path=str(executable),
            )
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("credential-bearing URL must be rejected")

        provider_record["base_url"] = ""
        try:
            build_codex_execution_contract(
                provider_record,
                launcher_path=str(executable),
                runtime_args=("--api-key", "TOPSECRET"),
            )
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("unmanaged runtime arguments must be rejected")


def test_deserialization_rejects_coercion_unknowns_and_missing_fingerprint() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")
        encoded = build_codex_execution_contract(
            provider(config_dir),
            launcher_path=str(executable),
        ).to_dict()
        mismatched_generation = json.loads(json.dumps(encoded))
        mismatched_generation["credential_generation"] += 1
        mismatched_payload = dict(mismatched_generation)
        mismatched_payload.pop("fingerprint")
        mismatched_generation["fingerprint"] = hashlib.sha256(
            json.dumps(
                mismatched_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        mutations = []
        mutations.append(mismatched_generation)
        boolean_revision = json.loads(json.dumps(encoded))
        boolean_revision["provider_revision"] = True
        mutations.append(boolean_revision)
        boolean_execution_revision = json.loads(json.dumps(encoded))
        boolean_execution_revision["provider_execution_revision"] = True
        mutations.append(boolean_execution_revision)
        string_args = json.loads(json.dumps(encoded))
        string_args["catalog_args"] = "abc"
        mutations.append(string_args)
        unknown = json.loads(json.dumps(encoded))
        unknown["launch_chain"]["launcher"]["unexpected"] = 1
        mutations.append(unknown)
        missing_fingerprint = json.loads(json.dumps(encoded))
        missing_fingerprint.pop("fingerprint")
        mutations.append(missing_fingerprint)
        unrelated = root / "unrelated"
        write_executable(unrelated, b"unrelated")
        arbitrary_spawn = json.loads(json.dumps(encoded))
        arbitrary_spawn["launch_chain"]["argv_prefix"][0] = str(unrelated)
        payload = dict(arbitrary_spawn)
        payload.pop("fingerprint")
        arbitrary_spawn["fingerprint"] = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        mutations.append(arbitrary_spawn)

        for mutation in mutations:
            try:
                CodexExecutionContract.from_dict(mutation)
            except ExecutionContractError:
                continue
            raise AssertionError(f"invalid contract was accepted: {mutation}")


def test_deserialization_reconstructs_exact_shebang_arguments() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        node = root / "runtime" / "node"
        launcher = root / "bin" / "codex"
        write_executable(node, b"node-runtime")
        write_executable(launcher, b"#!/usr/bin/env node\n")
        encoded = build_codex_execution_contract(
            provider(config_dir),
            launcher_path=str(launcher),
            search_path=str(node.parent),
        ).to_dict()
        encoded["launch_chain"]["argv_prefix"] = [
            str(node.resolve()),
            "--eval",
            "process.exit(0)",
            str(launcher.resolve()),
        ]
        encoded["launch_chain"]["component_argv_indexes"] = [0, 3]
        payload = dict(encoded)
        payload.pop("fingerprint")
        encoded["fingerprint"] = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()

        try:
            CodexExecutionContract.from_dict(encoded)
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("tampered shebang arguments were accepted")


def test_fingerprint_matches_canonical_payload() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        write_executable(executable, b"native")
        contract = build_codex_execution_contract(
            provider(config_dir),
            launcher_path=str(executable),
        )
        payload = contract.to_dict(include_fingerprint=False)
        expected = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()

        assert contract.fingerprint == expected


def test_builder_accepts_config_store_provider_authority() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state_home = root / "state"
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("", encoding="utf-8")
        executable = root / "codex"
        write_executable(executable, b"native")
        previous_home = os.environ.get("BETTER_AGENT_HOME")
        os.environ["BETTER_AGENT_HOME"] = str(state_home)
        try:
            import config_store

            created = config_store.add_provider({
                "name": "Contract test",
                "kind": "codex",
                "mode": "subscription",
                "config_dir": str(config_dir),
            })
            record = config_store.get_provider(created["id"])
            assert record is not None
            contract = build_codex_execution_contract(
                record,
                launcher_path=str(executable),
            )
        finally:
            if previous_home is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = previous_home

        assert contract.provider_generation == record["generation"]
        assert contract.provider_revision == record["revision"]
        assert (
            contract.provider_execution_revision
            == record["execution_revision"]
        )


CONTRACT_TESTS = (
    test_facade_exports_stable_execution_api,
    test_contract_is_deterministic_secret_free_and_config_bound,
    test_config_path_escape_and_secret_selectors_are_rejected,
    test_agent_authority_membership_is_frozen,
    test_secret_arguments_and_url_query_are_rejected,
    test_deserialization_rejects_coercion_unknowns_and_missing_fingerprint,
    test_deserialization_reconstructs_exact_shebang_arguments,
    test_fingerprint_matches_canonical_payload,
    test_builder_accepts_config_store_provider_authority,
)


if __name__ == "__main__":
    for test in (*LAUNCH_TESTS, *CONTRACT_TESTS):
        test()
    print("PASS codex execution contract")
