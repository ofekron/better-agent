from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_execution_common import ExecutionContractError  # noqa: E402
from provider_family_launch_attestation import (  # noqa: E402
    FamilyLaunchAttestation,
    build_provider_family_launch_contract,
    capture_cli_launch,
    capture_config_scope,
    capture_critical_package,
    capture_runner_launch,
    materialize_sdk_launch,
    open_pinned_launch,
)


def _write_executable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(path.stat().st_mode | 0o100)


def _capture(root: Path) -> FamilyLaunchAttestation:
    run_dir = root / "run"
    run_dir.mkdir()
    runner = root / "backend" / "runner.py"
    _write_executable(runner, b"print('runner')\n")
    cli = root / "bin" / "claude"
    _write_executable(cli, b"native-cli")
    config_root = root / "config"
    config_root.mkdir()
    settings = config_root / "settings.json"
    settings.write_text('{"theme":"dark"}', encoding="utf-8")
    resume = config_root / "projects" / "session.jsonl"
    resume.parent.mkdir()
    resume.write_text('{"type":"user"}\n', encoding="utf-8")
    package_root = root / "site-packages" / "claude_agent_sdk"
    source = package_root / "_internal" / "transport" / "subprocess_cli.py"
    _write_executable(package_root / "__init__.py", b"__version__='1'\n")
    _write_executable(source, b"class SubprocessCLITransport: pass\n")
    return FamilyLaunchAttestation.capture(
        family="claude",
        runner=capture_runner_launch(
            run_dir=run_dir,
            executable_path=Path(sys.executable),
            runner_entry=runner,
            runner_kind="claude",
            runner_module="runner",
            frozen=False,
        ),
        downstream=capture_cli_launch(
            logical_command="claude",
            launcher_path=cli,
            platform="linux",
        ),
        config=capture_config_scope(
            root_path=config_root,
            config_paths=(settings,),
            resume_path=resume,
        ),
        critical_packages=(
            capture_critical_package(
                package_name="claude_agent_sdk",
                package_root=package_root,
                relative_paths=(
                    "__init__.py",
                    "_internal/transport/subprocess_cli.py",
                ),
            ),
        ),
    )


def test_runner_argv_shapes_are_exact_for_dev_frozen_and_windows() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        run_dir = root / "run"
        run_dir.mkdir()
        python = root / "python.exe"
        runner = root / "runner_agy.py"
        claude_runner = root / "runner.py"
        _write_executable(python, b"python")
        _write_executable(runner, b"runner")
        _write_executable(claude_runner, b"runner")

        dev = capture_runner_launch(
            run_dir=run_dir,
            executable_path=python,
            runner_entry=runner,
            runner_kind="agy",
            runner_module="runner_agy",
            frozen=False,
            platform="win32",
        )
        frozen_agy = capture_runner_launch(
            run_dir=run_dir,
            executable_path=python,
            runner_entry=runner,
            runner_kind="agy",
            runner_module="runner_agy",
            frozen=True,
            platform="win32",
        )
        frozen_claude = capture_runner_launch(
            run_dir=run_dir,
            executable_path=python,
            runner_entry=claude_runner,
            runner_kind="claude",
            runner_module="runner",
            frozen=True,
            platform="darwin",
        )

        assert dev.launch.argv == (
            str(python.resolve()),
            str(runner.resolve()),
            "--run-dir",
            str(run_dir),
        )
        assert dev.launch.component_argv_indexes == (0, 1)
        assert frozen_agy.launch.argv == (
            str(python.resolve()),
            "--run-dir",
            str(run_dir),
            "--runner-kind",
            "agy",
        )
        assert frozen_claude.launch.argv == (
            str(python.resolve()),
            "--run-dir",
            str(run_dir),
        )
        assert frozen_agy.launch.component_argv_indexes == (0,)
        python.write_bytes(b"changed")
        assert not dev.attest()


def test_cli_path_swap_and_windows_command_shape_are_bound() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        runtime_a = root / "runtime-a" / "node"
        runtime_b = root / "runtime-b" / "node"
        launcher = root / "bin" / "claude"
        _write_executable(runtime_a, b"runtime-a")
        _write_executable(runtime_b, b"runtime-b")
        _write_executable(launcher, b"#!/usr/bin/env node\n")

        launch = capture_cli_launch(
            logical_command="claude",
            launcher_path=launcher,
            search_path=str(runtime_a.parent),
            platform="linux",
        )
        previous_path = os.environ.get("PATH")
        os.environ["PATH"] = str(runtime_b.parent)
        try:
            assert launch.argv == (
                str(runtime_a.resolve()),
                str(launcher.resolve()),
            )
            assert launch.attest()
        finally:
            if previous_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = previous_path
        runtime_a.write_bytes(b"runtime-changed")
        assert not launch.attest()

        cmd = root / "windows" / "agy.cmd"
        command_processor = root / "windows" / "cmd.exe"
        _write_executable(cmd, b"@echo off\r\n")
        _write_executable(command_processor, b"cmd")
        windows = capture_cli_launch(
            logical_command="agy",
            launcher_path=cmd,
            command_processor=command_processor,
            platform="win32",
        )
        assert windows.argv == (
            str(command_processor.resolve()),
            "/d",
            "/s",
            "/c",
            str(cmd.resolve()),
        )
        assert windows.component_argv_indexes == (0, 4)


def test_source_package_config_resume_and_symlink_drift_fail_attestation() -> None:
    mutators = (
        lambda root: (root / "backend" / "runner.py").write_bytes(b"changed"),
        lambda root: (root / "bin" / "claude").write_bytes(b"changed"),
        lambda root: (
            root
            / "site-packages"
            / "claude_agent_sdk"
            / "_internal"
            / "transport"
            / "subprocess_cli.py"
        ).write_bytes(b"changed"),
        lambda root: (root / "config" / "settings.json").write_text(
            "{}",
            encoding="utf-8",
        ),
        lambda root: (
            root / "config" / "projects" / "session.jsonl"
        ).write_text("changed", encoding="utf-8"),
    )
    for mutate in mutators:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            attestation = _capture(root)
            mutate(root)
            assert not attestation.attest()
            try:
                with attestation.open_downstream():
                    pass
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("drifted launch graph reached spawn")

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        first = root / "first"
        second = root / "second"
        _write_executable(first, b"same")
        _write_executable(second, b"same")
        launcher = root / "claude"
        launcher.symlink_to(first)
        launch = capture_cli_launch(
            logical_command="claude",
            launcher_path=launcher,
            platform="linux",
        )
        launcher.unlink()
        launcher.symlink_to(second)
        assert not launch.attest()
        try:
            materialize_sdk_launch(launch, root)
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("retargeted launcher was materialized")


def test_payload_round_trip_and_tamper_are_strict() -> None:
    with tempfile.TemporaryDirectory() as raw:
        attestation = _capture(Path(raw))
        payload = attestation.to_payload()
        decoded = FamilyLaunchAttestation.from_payload(
            json.loads(json.dumps(payload)),
        )
        envelope = build_provider_family_launch_contract(
            {
                "id": "claude-test",
                "kind": "claude",
                "generation": "generation-1",
                "revision": 3,
            },
            attestation,
        )
        assert decoded == attestation
        assert decoded.attest()
        assert envelope["contract"]["payload"] == payload
        assert "api_key" not in json.dumps(payload, sort_keys=True).lower()

        mutations = []
        changed = json.loads(json.dumps(payload))
        changed["launch_attestation"]["runner"]["launch"]["argv"][0] = "/tmp/x"
        mutations.append(changed)
        unknown = json.loads(json.dumps(payload))
        unknown["launch_attestation"]["unknown"] = True
        mutations.append(unknown)
        missing = json.loads(json.dumps(payload))
        missing["launch_attestation"].pop("fingerprint")
        mutations.append(missing)
        malformed = json.loads(json.dumps(payload))
        contract = malformed["launch_attestation"]
        contract["runner"]["launch"]["argv"].append("--unexpected")
        unsigned = dict(contract)
        unsigned.pop("fingerprint")
        contract["fingerprint"] = hashlib.sha256(
            json.dumps(
                unsigned,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        mutations.append(malformed)
        for mutation in mutations:
            try:
                FamilyLaunchAttestation.from_payload(mutation)
            except ExecutionContractError:
                continue
            raise AssertionError("tampered launch attestation was accepted")


def test_pinned_launch_and_sdk_materialization_do_not_reread_resolution() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        executable = root / "agy"
        _write_executable(executable, b"original")
        launch = capture_cli_launch(
            logical_command="agy",
            launcher_path=executable,
            platform="linux",
        )
        with open_pinned_launch(launch) as pinned:
            replacement = root / "replacement"
            _write_executable(replacement, b"replacement")
            replacement.replace(executable)
            with open(pinned.argv[0], "rb", closefd=True) as handle:
                assert handle.read() == b"original"

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        launcher = root / "bin" / "claude"
        destination = root / "materialized"
        destination.mkdir()
        _write_executable(
            launcher,
            (
                f"#!{Path(sys.executable).resolve()}\n"
                "print('materialized', end='')\n"
            ).encode("utf-8"),
        )
        launch = capture_cli_launch(
            logical_command="claude",
            launcher_path=launcher,
            platform="linux",
        )
        materialized = materialize_sdk_launch(launch, destination)
        launcher.write_bytes(b"changed")
        completed = subprocess.run(
            [materialized.executable_path],
            capture_output=True,
            check=False,
            text=True,
        )

        assert materialized.attest()
        assert completed.returncode == 0
        assert completed.stdout == "materialized"
        assert Path(materialized.executable_path).is_file()
        assert str(destination.resolve()) in materialized.executable_path

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        executable = root / "claude"
        destination = root / "materialized"
        destination.mkdir()
        _write_executable(executable, b"original")
        launch = capture_cli_launch(
            logical_command="claude",
            launcher_path=executable,
            platform="linux",
        )
        executable.write_bytes(b"changed")
        try:
            materialize_sdk_launch(launch, destination)
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("drifted SDK executable was materialized")


TESTS = (
    test_runner_argv_shapes_are_exact_for_dev_frozen_and_windows,
    test_cli_path_swap_and_windows_command_shape_are_bound,
    test_source_package_config_resume_and_symlink_drift_fail_attestation,
    test_payload_round_trip_and_tamper_are_strict,
    test_pinned_launch_and_sdk_materialization_do_not_reread_resolution,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
    print("PASS provider family launch attestation")
