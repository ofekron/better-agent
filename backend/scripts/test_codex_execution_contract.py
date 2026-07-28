from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_execution import (  # noqa: E402
    CodexExecutionContract,
    ExecutionContractError,
    build_codex_execution_contract,
    resolve_codex_launch_chain,
)


def _write_executable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _provider(config_dir: Path) -> dict:
    return {
        "id": "codex-provider",
        "kind": "codex",
        "generation": "generation-a",
        "record_version": 7,
        "config_dir": str(config_dir),
        "base_url": "",
        "mode": "subscription",
        "api_key": "must-never-persist",
    }


def test_native_symlink_chain_is_attested_and_retarget_fails() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        first = root / "release-a" / "codex"
        second = root / "release-b" / "codex"
        _write_executable(first, b"native-a")
        _write_executable(second, b"native-b")
        launcher = root / "bin" / "codex"
        launcher.parent.mkdir()
        launcher.symlink_to(first)

        chain = resolve_codex_launch_chain(str(launcher))

        assert chain.argv_prefix == (str(first.resolve()),)
        assert chain.attest()
        launcher.unlink()
        launcher.symlink_to(second)
        assert not chain.attest()


def test_same_size_same_mtime_replacement_fails_strong_identity() -> None:
    with tempfile.TemporaryDirectory() as raw:
        executable = Path(raw) / "codex"
        _write_executable(executable, b"aaaa")
        original = executable.stat()
        chain = resolve_codex_launch_chain(str(executable))

        executable.write_bytes(b"bbbb")
        os.utime(
            executable,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )

        assert executable.stat().st_size == original.st_size
        assert executable.stat().st_mtime_ns == original.st_mtime_ns
        assert not chain.attest()


def test_env_shebang_binds_interpreter_and_script() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        node = root / "runtime" / "node"
        script = root / "bin" / "codex"
        _write_executable(node, b"node-runtime")
        _write_executable(script, b"#!/usr/bin/env node\nconsole.log('codex')\n")

        chain = resolve_codex_launch_chain(
            str(script),
            search_path=str(node.parent),
            platform="linux",
        )

        assert chain.argv_prefix == (str(node.resolve()), str(script.resolve()))
        assert chain.attest()
        node.write_bytes(b"node-mutated")
        assert not chain.attest()


def test_windows_cmd_prefers_single_vendor_binary() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        launcher = root / "codex.cmd"
        _write_executable(launcher, b"@echo off\r\n")
        vendor = (
            root
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-arm64"
            / "vendor"
            / "aarch64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )
        _write_executable(vendor, b"windows-native")

        chain = resolve_codex_launch_chain(
            str(launcher),
            platform="win32",
        )

        assert chain.argv_prefix == (str(vendor.resolve()),)
        assert chain.attest()


def test_x64_vendor_package_suffix_is_resolved() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        launcher = root / "codex.cmd"
        _write_executable(launcher, b"@echo off\r\n")
        vendor = (
            root
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
            / "x86_64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )
        _write_executable(vendor, b"windows-x64")

        chain = resolve_codex_launch_chain(
            str(launcher),
            platform="win32",
            architecture="AMD64",
        )

        assert chain.argv_prefix == (str(vendor.resolve()),)


def test_ambiguous_vendor_targets_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        launcher = root / "codex.cmd"
        _write_executable(launcher, b"@echo off\r\n")
        for variant in ("first", "second"):
            vendor = (
                root
                / "node_modules"
                / "@openai"
                / "codex"
                / "node_modules"
                / "@openai"
                / "codex-win32-arm64"
                / "vendor"
                / variant
                / "bin"
                / "codex.exe"
            )
            _write_executable(vendor, variant.encode())

        try:
            resolve_codex_launch_chain(str(launcher), platform="win32")
        except ExecutionContractError as exc:
            assert "ambiguous" in str(exc)
        else:
            raise AssertionError("ambiguous vendor targets must fail closed")


def test_contract_is_deterministic_secret_free_and_config_bound() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text('model_provider = "openai"\n', encoding="utf-8")
        executable = root / "codex"
        _write_executable(executable, b"native")
        provider = _provider(config_dir)

        contract = build_codex_execution_contract(
            provider,
            launcher_path=str(executable),
            profile="work",
            catalog_args=("-c", 'model_provider="openai"'),
            runtime_args=("-c", "features.shell_snapshot=false"),
            credential_generation=3,
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

        config_file.write_text('model_provider = "other"\n', encoding="utf-8")
        assert not contract.attest()


def test_open_attested_components_pins_exact_bytes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        executable = root / "codex"
        _write_executable(executable, b"native-a")
        chain = resolve_codex_launch_chain(str(executable))

        with chain.open_attested_components() as handles:
            replacement = root / "replacement"
            _write_executable(replacement, b"native-b")
            replacement.replace(executable)
            os.lseek(handles[0], 0, os.SEEK_SET)
            pinned = os.read(handles[0], 64)

        assert pinned == b"native-a"
        assert not chain.attest_metadata()


def test_config_path_escape_and_secret_selectors_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        _write_executable(executable, b"native")
        provider = _provider(config_dir)

        escaped = config_dir / "escaped.toml"
        escaped.symlink_to(root / "outside.toml")
        (root / "outside.toml").write_text("x=1", encoding="utf-8")
        try:
            build_codex_execution_contract(
                provider,
                launcher_path=str(executable),
                config_paths=(str(escaped),),
            )
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("config symlink escape must fail closed")

        try:
            build_codex_execution_contract(
                provider,
                launcher_path=str(executable),
                environment_selectors={"SAKANA_API_KEY": "secret"},
            )
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("secret selectors must never enter the contract")


def test_secret_arguments_and_url_query_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        executable = root / "codex"
        _write_executable(executable, b"native")
        provider = _provider(config_dir)
        provider["base_url"] = "https://example.test/v1?api_key=TOPSECRET"

        try:
            build_codex_execution_contract(
                provider,
                launcher_path=str(executable),
            )
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("credential-bearing URL must be rejected")

        provider["base_url"] = ""
        try:
            build_codex_execution_contract(
                provider,
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
        _write_executable(executable, b"native")
        encoded = build_codex_execution_contract(
            _provider(config_dir),
            launcher_path=str(executable),
        ).to_dict()
        mutations = []
        boolean_revision = json.loads(json.dumps(encoded))
        boolean_revision["provider_record_version"] = True
        mutations.append(boolean_revision)
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
        _write_executable(unrelated, b"unrelated")
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
        _write_executable(node, b"node-runtime")
        _write_executable(launcher, b"#!/usr/bin/env node\n")
        encoded = build_codex_execution_contract(
            _provider(config_dir),
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
        _write_executable(executable, b"native")
        contract = build_codex_execution_contract(
            _provider(config_dir),
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


if __name__ == "__main__":
    test_native_symlink_chain_is_attested_and_retarget_fails()
    test_same_size_same_mtime_replacement_fails_strong_identity()
    test_env_shebang_binds_interpreter_and_script()
    test_windows_cmd_prefers_single_vendor_binary()
    test_x64_vendor_package_suffix_is_resolved()
    test_ambiguous_vendor_targets_fail_closed()
    test_contract_is_deterministic_secret_free_and_config_bound()
    test_open_attested_components_pins_exact_bytes()
    test_config_path_escape_and_secret_selectors_are_rejected()
    test_secret_arguments_and_url_query_are_rejected()
    test_deserialization_rejects_coercion_unknowns_and_missing_fingerprint()
    test_deserialization_reconstructs_exact_shebang_arguments()
    test_fingerprint_matches_canonical_payload()
    print("PASS codex execution contract")
