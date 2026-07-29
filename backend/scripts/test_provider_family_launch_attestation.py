from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_execution_common import ExecutionContractError  # noqa: E402
from provider_family_launch_attestation import (  # noqa: E402
    ConfigScopeIdentity,
    FamilyLaunchAttestation,
    build_provider_family_launch_contract,
    capture_cli_launch,
    capture_config_scope,
    capture_critical_package,
    capture_runner_launch,
    materialize_sdk_launch,
    open_pinned_launch,
)
from provider_pinned_launch import open_pinned_runner_launch  # noqa: E402
from provider_runner_launch import RunnerLaunch  # noqa: E402


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


def _assert_contract_rejected(callable_) -> None:
    try:
        callable_()
    except ExecutionContractError:
        return
    raise AssertionError("invalid execution contract was accepted")


def test_external_config_symlink_is_exactly_attested() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_root = root / "config"
        external_root = root / "shared"
        config_root.mkdir()
        external_root.mkdir()
        first = external_root / "first.json"
        second = external_root / "second.json"
        first.write_text('{"theme":"dark"}', encoding="utf-8")
        second.write_text('{"theme":"dark"}', encoding="utf-8")
        settings = config_root / "settings.json"
        settings.symlink_to(first)

        captured = capture_config_scope(
            root_path=config_root,
            config_paths=(settings,),
        )
        decoded = ConfigScopeIdentity.from_dict(
            json.loads(json.dumps(captured.to_dict())),
        )

        assert decoded == captured
        assert decoded.attest()
        assert decoded.files[0].attest_metadata()

        forged = captured.to_dict()
        forged["files"][0]["config_file"]["symlink_chain"] = []
        _assert_contract_rejected(
            lambda: ConfigScopeIdentity.from_dict(forged),
        )

        settings.unlink()
        settings.symlink_to(second)
        assert not captured.attest()

        settings.unlink()
        settings.symlink_to(first)
        captured = capture_config_scope(
            root_path=config_root,
            config_paths=(settings,),
        )
        first.write_text('{"theme":"light"}', encoding="utf-8")
        assert not captured.attest()

        first.write_text('{"theme":"dark"}', encoding="utf-8")
        captured = capture_config_scope(
            root_path=config_root,
            config_paths=(settings,),
        )
        first.unlink()
        assert not captured.attest()

        first.write_text('{"theme":"dark"}', encoding="utf-8")
        captured = capture_config_scope(
            root_path=config_root,
            config_paths=(settings,),
        )
        settings.unlink()
        settings.write_text('{"theme":"dark"}', encoding="utf-8")
        assert not captured.attest()

        nested_parent = config_root / "nested"
        nested_parent.mkdir()
        nested_settings = nested_parent / "settings.json"
        nested_settings.write_text('{"theme":"dark"}', encoding="utf-8")
        nested_captured = capture_config_scope(
            root_path=config_root,
            config_paths=(nested_settings,),
        )
        replacement_parent = external_root / "nested"
        replacement_parent.mkdir()
        (replacement_parent / "settings.json").write_text(
            '{"theme":"dark"}',
            encoding="utf-8",
        )
        shutil.rmtree(nested_parent)
        nested_parent.symlink_to(replacement_parent, target_is_directory=True)
        assert not nested_captured.files[0].attest()

        parent_link = config_root / "linked"
        parent_link.symlink_to(external_root, target_is_directory=True)
        _assert_contract_rejected(
            lambda: capture_config_scope(
                root_path=config_root,
                config_paths=(parent_link / "second.json",),
            ),
        )
        _assert_contract_rejected(
            lambda: capture_config_scope(
                root_path=config_root,
                config_paths=(second,),
            ),
        )

        directory_link = config_root / "directory.json"
        directory_link.symlink_to(external_root, target_is_directory=True)
        _assert_contract_rejected(
            lambda: capture_config_scope(
                root_path=config_root,
                config_paths=(directory_link,),
            ),
        )


def test_runner_argv_shapes_are_exact_for_dev_frozen_and_windows() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        run_dir = root / "run"
        run_dir.mkdir()
        bundle_root = root / "bundle"
        sidecar = bundle_root / "_internal"
        sidecar.mkdir(parents=True)
        (sidecar / "runtime.pyd").write_bytes(b"sidecar")
        python = bundle_root / "python.exe"
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
        claude_dev = capture_runner_launch(
            run_dir=run_dir,
            executable_path=python,
            runner_entry=claude_runner,
            runner_kind="claude",
            runner_module="runner",
            frozen=False,
            platform="darwin",
        )
        frozen_agy = capture_runner_launch(
            run_dir=run_dir,
            executable_path=python,
            runner_entry=runner,
            runner_kind="agy",
            runner_module="runner_agy",
            frozen=True,
            platform="win32",
            frozen_bundle_root=bundle_root,
            frozen_sidecar_root=sidecar,
        )
        frozen_claude = capture_runner_launch(
            run_dir=run_dir,
            executable_path=python,
            runner_entry=claude_runner,
            runner_kind="claude",
            runner_module="runner",
            frozen=True,
            platform="darwin",
            frozen_bundle_root=bundle_root,
            frozen_sidecar_root=sidecar,
        )

        assert dev.launch.argv == (
            str(python.resolve()),
            str(runner.resolve()),
            "--run-dir",
            str(run_dir),
        )
        assert dev.launch.component_argv_indexes == (0, 1)
        assert claude_dev.launch.argv == (
            str(python.resolve()),
            "-I",
            str(claude_runner.resolve()),
            "--run-dir",
            str(run_dir),
            "--backend-root",
            str(root.resolve()),
            "--site-packages-root",
            claude_dev.launch.argv[8],
            "--application-sdk-root",
            claude_dev.launch.argv[10],
        )
        assert claude_dev.launch.component_argv_indexes == (0, 2)
        assert Path(claude_dev.launch.argv[8]).is_absolute()
        assert Path(claude_dev.launch.argv[10]).is_absolute()
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
        assert frozen_agy.runner_entry is None
        assert frozen_agy.frozen_bundle is not None
        assert RunnerLaunch.from_dict(
            json.loads(json.dumps(frozen_agy.to_dict())),
        ) == frozen_agy
        python.write_bytes(b"changed")
        assert not dev.attest()


def test_cli_path_swap_and_windows_wrappers_fail_closed() -> None:
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

        command_processor = root / "windows" / "cmd.exe"
        _write_executable(command_processor, b"cmd")
        for suffix in (".cmd", ".bat"):
            wrapper = root / "windows" / f"agy{suffix}"
            _write_executable(wrapper, b"@echo off\r\n")
            try:
                capture_cli_launch(
                    logical_command="agy",
                    launcher_path=wrapper,
                    command_processor=command_processor,
                    platform="win32",
                )
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("Windows command wrapper was accepted")

        native = root / "windows" / "agy.exe"
        _write_executable(native, b"MZ-native")
        windows = capture_cli_launch(
            logical_command="agy",
            launcher_path=native,
            command_processor=command_processor,
            platform="win32",
        )
        assert windows.mode == "native"
        assert windows.argv == (str(native.resolve()),)
        assert windows.attest()
        native.write_bytes(b"MZ-changed")
        assert not windows.attest()


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


def test_shebang_launch_pins_script_and_restricts_interpreter() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        launcher = root / "agy"
        _write_executable(
            launcher,
            b"#!/bin/sh\nprintf original\n",
        )
        launch = capture_cli_launch(
            logical_command="agy",
            launcher_path=launcher,
            platform=sys.platform,
        )
        with open_pinned_launch(launch) as pinned:
            replacement = root / "replacement"
            _write_executable(
                replacement,
                b"#!/bin/sh\nprintf replacement\n",
            )
            replacement.replace(launcher)
            completed = subprocess.run(
                pinned.argv,
                capture_output=True,
                check=False,
                text=True,
            )
            assert pinned.argv[0] == str(Path("/bin/sh").resolve())
            assert pinned.argv[-1] != str(launcher)
            assert completed.returncode == 0
            assert completed.stdout == "original"

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        interpreter = root / "runtime"
        launcher = root / "agy"
        _write_executable(interpreter, b"runtime")
        _write_executable(
            launcher,
            f"#!{interpreter}\n".encode("utf-8"),
        )
        launch = capture_cli_launch(
            logical_command="agy",
            launcher_path=launcher,
            platform=sys.platform,
        )
        try:
            with open_pinned_launch(launch):
                pass
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("mutable interpreter executed by path")


def test_runner_materialization_outlives_spawn_context() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        run_dir = root / "run"
        run_dir.mkdir()
        marker = root / "ran"
        if os.name == "nt":
            runner_path = root / "runner_better_agent.py"
            runner_path.write_text(
                "import pathlib, sys, time\n"
                "time.sleep(0.1)\n"
                "pathlib.Path(sys.argv[-1]).write_text('ran')\n",
                encoding="utf-8",
            )
            executable = Path(sys.executable)
            runner_kind = "openai"
            runner_module = "runner_better_agent"
        else:
            runner_path = root / "runner_agy"
            runner_path.write_text(
                "sleep 0.1\n"
                "printf ran > \"$3\"\n",
                encoding="utf-8",
            )
            executable = Path("/bin/sh")
            runner_kind = "agy"
            runner_module = "runner_agy"
        runner = capture_runner_launch(
            run_dir=run_dir,
            executable_path=executable,
            runner_entry=runner_path,
            runner_kind=runner_kind,
            runner_module=runner_module,
            frozen=False,
        )
        with open_pinned_runner_launch(runner) as pinned:
            materialized_runner = Path(pinned.argv[1])
            process = subprocess.Popen(
                [*pinned.argv, str(marker)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )

        assert materialized_runner.is_file()
        assert materialized_runner.is_relative_to(run_dir)
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert marker.read_text(encoding="utf-8") == "ran"


def test_runner_materialization_preserves_python_runtime_closure() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        run_dir = root / "run"
        run_dir.mkdir()
        runner_path = root / "runner_runtime_probe.py"
        runner_path.write_text("print('runtime-closure-ok')\n", encoding="utf-8")
        runner = capture_runner_launch(
            run_dir=run_dir,
            executable_path=sys.executable,
            runner_entry=runner_path,
            runner_kind="openai",
            runner_module="runner_runtime_probe",
            frozen=False,
        )

        with open_pinned_runner_launch(runner) as pinned:
            process = subprocess.run(
                pinned.argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

        assert process.returncode == 0, process.stderr
        assert process.stdout.strip() == "runtime-closure-ok"
        if runner.development_runtime is not None:
            runtime_root = (
                run_dir
                / Path(runner.development_runtime.root.resolved_path).name
            )
            materialized = (
                runtime_root
                / runner.development_runtime.executable_relative
            )
            assert materialized.is_file()
            materialized.chmod(0o700)
            materialized.write_bytes(b"tampered")
            try:
                with open_pinned_runner_launch(runner):
                    pass
            except ExecutionContractError:
                pass
            else:
                raise AssertionError(
                    "tampered interpreter runtime was accepted",
                )


TESTS = (
    test_external_config_symlink_is_exactly_attested,
    test_runner_argv_shapes_are_exact_for_dev_frozen_and_windows,
    test_cli_path_swap_and_windows_wrappers_fail_closed,
    test_source_package_config_resume_and_symlink_drift_fail_attestation,
    test_payload_round_trip_and_tamper_are_strict,
    test_pinned_launch_and_sdk_materialization_do_not_reread_resolution,
    test_shebang_launch_pins_script_and_restricts_interpreter,
    test_runner_materialization_outlives_spawn_context,
    test_runner_materialization_preserves_python_runtime_closure,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
    print("PASS provider family launch attestation")
