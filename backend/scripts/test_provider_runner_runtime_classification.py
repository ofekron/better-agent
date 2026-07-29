from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import _test_home  # noqa: E402

_test_home.isolate(prefix="runner-runtime-classification-")

from codex_execution_common import ExecutionContractError  # noqa: E402
from provider_frozen_bundle import frozen_bundle_destination  # noqa: E402
from provider_pinned_launch import open_pinned_runner_launch  # noqa: E402
import provider_runner_launch  # noqa: E402
from provider_runner_launch import capture_runner_launch  # noqa: E402


def test_base_python_installation_is_not_a_runtime_bundle() -> None:
    with tempfile.TemporaryDirectory() as raw:
        alias = Path(raw) / "base-alias"
        alias.symlink_to(Path(sys.base_prefix), target_is_directory=True)
        with (
            mock.patch.object(provider_runner_launch.sys, "prefix", str(alias)),
            mock.patch.object(
                provider_runner_launch.FrozenBundleIdentity,
                "capture",
                side_effect=AssertionError("base installation was scanned"),
            ),
        ):
            runner = capture_runner_launch(
                run_dir=Path(raw),
                executable_path=sys.executable,
                runner_entry=Path(__file__),
                runner_kind="openai",
                runner_module="test_provider_runner_runtime_classification",
                frozen=False,
            )

    assert runner.development_runtime is None


def test_self_contained_python_runtime_is_materialized_and_attested() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        runtime_root = root / "runtime"
        executable = runtime_root / "bin" / "python"
        executable.parent.mkdir(parents=True)
        shutil.copy2(Path(sys.executable).resolve(strict=True), executable)
        stdlib_root = runtime_root / "lib" / "python"
        stdlib_root.mkdir(parents=True)
        runner_entry = root / "runner_probe.py"
        runner_entry.write_text("print('runtime-captured')\n", encoding="utf-8")
        run_dir = root / "run"
        run_dir.mkdir()
        base_runtime_root = root / "base"
        base_runtime_root.mkdir()

        with (
            mock.patch.object(
                provider_runner_launch.sys,
                "executable",
                str(executable),
            ),
            mock.patch.object(
                provider_runner_launch.sys,
                "prefix",
                str(runtime_root),
            ),
            mock.patch.object(
                provider_runner_launch.sys,
                "base_prefix",
                str(base_runtime_root),
            ),
            mock.patch.object(
                provider_runner_launch.sysconfig,
                "get_path",
                return_value=str(stdlib_root),
            ),
        ):
            runner = capture_runner_launch(
                run_dir=run_dir,
                executable_path=executable,
                runner_entry=runner_entry,
                runner_kind="openai",
                runner_module="runner_probe",
                frozen=False,
            )

        runtime = runner.development_runtime
        assert runtime is not None
        assert Path(runtime.root.resolved_path) == runtime_root.resolve()
        assert (
            Path(runtime.root.resolved_path) / runtime.sidecar_relative
        ) == stdlib_root.resolve()
        assert runtime.excluded_relative_paths == ()
        with open_pinned_runner_launch(runner) as pinned:
            process = subprocess.run(
                pinned.argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        assert process.returncode == 0, process.stderr
        assert process.stdout.strip() == "runtime-captured"

        materialized = (
            frozen_bundle_destination(runtime)
            / runtime.executable_relative
        )
        materialized.write_bytes(b"tampered")
        try:
            with open_pinned_runner_launch(runner):
                pass
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("tampered runtime was accepted")


if __name__ == "__main__":
    test_base_python_installation_is_not_a_runtime_bundle()
    test_self_contained_python_runtime_is_materialized_and_attested()
    print("PASS provider runner runtime classification")
