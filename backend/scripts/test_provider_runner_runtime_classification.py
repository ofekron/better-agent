from __future__ import annotations

import os
import shlex
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
from codex_execution_identity import FileIdentity  # noqa: E402
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


def test_external_venv_base_interpreter_is_not_a_runtime_bundle() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        prefix_root = root / "venv"
        prefix_root.mkdir()
        base_root = root / "shared-base"
        executable = base_root / "bin" / "python"
        executable.parent.mkdir(parents=True)
        # Self-locating interpreter stand-in: the bare-copy probe must pass
        # so the shared base installation is skipped without any scan.
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        stdlib_root = base_root / "lib" / "python"
        stdlib_root.mkdir(parents=True)

        with (
            mock.patch.object(
                provider_runner_launch.sys,
                "executable",
                str(executable),
            ),
            mock.patch.object(
                provider_runner_launch.sys,
                "prefix",
                str(prefix_root),
            ),
            mock.patch.object(
                provider_runner_launch.sys,
                "base_prefix",
                str(base_root),
            ),
            mock.patch.object(
                provider_runner_launch.sysconfig,
                "get_path",
                return_value=str(stdlib_root),
            ),
            mock.patch.object(
                provider_runner_launch.FrozenBundleIdentity,
                "capture",
                side_effect=AssertionError("shared base installation was scanned"),
            ),
        ):
            runner = capture_runner_launch(
                run_dir=root,
                executable_path=executable,
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
        executable.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(str(Path(sys.executable).resolve(strict=True)))} \"$@\"\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
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


def test_system_interpreter_is_not_bundled_from_external_venv() -> None:
    with tempfile.TemporaryDirectory() as raw:
        prefix_root = Path(raw) / "venv"
        prefix_root.mkdir()
        current = Path("/usr/bin/python3")
        if not current.is_file():
            return
        with (
            mock.patch.object(
                provider_runner_launch.sys,
                "executable",
                str(current),
            ),
            mock.patch.object(
                provider_runner_launch.sys,
                "prefix",
                str(prefix_root),
            ),
            mock.patch.object(
                provider_runner_launch.sys,
                "base_prefix",
                "/usr",
            ),
            mock.patch.object(
                provider_runner_launch.sysconfig,
                "get_path",
                return_value="/usr/lib",
            ),
            mock.patch.object(
                provider_runner_launch.FrozenBundleIdentity,
                "capture",
                side_effect=AssertionError("system installation was scanned"),
            ),
        ):
            runner = capture_runner_launch(
                run_dir=Path(raw),
                executable_path=current,
                runner_entry=Path(__file__),
                runner_kind="openai",
                runner_module="test_provider_runner_runtime_classification",
                frozen=False,
            )

    assert runner.development_runtime is None


def _relocation_dependent_base_layout(
    tmp_root: Path,
    *,
    probe_exit: int,
) -> tuple[Path, Path, Path]:
    """Venv whose interpreter and stdlib resolve into a store-style base
    root (uv python-build-standalone layout)."""
    base_root = tmp_root / "store"
    stdlib_root = base_root / "lib" / "python3.99"
    stdlib_root.mkdir(parents=True)
    (stdlib_root / "os.py").write_text("", encoding="utf-8")
    bin_dir = base_root / "bin"
    bin_dir.mkdir()
    python_path = bin_dir / "python"
    python_path.write_bytes(f"#!/bin/sh\nexit {probe_exit}\n".encode())
    python_path.chmod(0o755)
    prefix_root = tmp_root / "venv"
    prefix_root.mkdir()
    return python_path, prefix_root, stdlib_root


def _classify_base(python_path: Path, prefix_root: Path, stdlib_root: Path):
    executable = FileIdentity.capture(python_path)
    with (
        mock.patch.object(
            provider_runner_launch.sys,
            "executable",
            str(python_path),
        ),
        mock.patch.object(
            provider_runner_launch.sys,
            "prefix",
            str(prefix_root),
        ),
        mock.patch.object(
            provider_runner_launch.sys,
            "base_prefix",
            str(stdlib_root.parents[1]),
        ),
        mock.patch.object(
            provider_runner_launch.sysconfig,
            "get_path",
            lambda name: str(stdlib_root),
        ),
    ):
        return provider_runner_launch._development_runtime(executable)


def test_relocation_dependent_base_store_is_bundled() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as raw:
        python_path, prefix_root, stdlib_root = (
            _relocation_dependent_base_layout(
                Path(raw).resolve(),
                probe_exit=1,
            )
        )
        runtime = _classify_base(python_path, prefix_root, stdlib_root)
        assert runtime is not None
        assert Path(runtime.root.resolved_path) == stdlib_root.parents[1]
        assert (
            Path(runtime.root.resolved_path) / runtime.sidecar_relative
        ) == stdlib_root


def test_bare_copy_probe_runs_once_per_interpreter() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as raw:
        python_path, prefix_root, stdlib_root = (
            _relocation_dependent_base_layout(
                Path(raw).resolve(),
                probe_exit=0,
            )
        )
        probed: list[Path] = []
        original = provider_runner_launch._run_bare_copy_probe
        with mock.patch.object(
            provider_runner_launch,
            "_run_bare_copy_probe",
            side_effect=lambda path: (probed.append(path), original(path))[1],
        ):
            first = _classify_base(python_path, prefix_root, stdlib_root)
            second = _classify_base(python_path, prefix_root, stdlib_root)
    assert first is None and second is None
    assert len(probed) == 1


def test_bare_copy_probe_spawn_failure_fails_closed() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as raw:
        python_path, prefix_root, stdlib_root = (
            _relocation_dependent_base_layout(
                Path(raw).resolve(),
                probe_exit=0,
            )
        )
        with mock.patch.object(
            provider_runner_launch.subprocess,
            "Popen",
            side_effect=OSError("exec blocked"),
        ):
            try:
                _classify_base(python_path, prefix_root, stdlib_root)
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("probe spawn failure did not fail closed")


if __name__ == "__main__":
    test_base_python_installation_is_not_a_runtime_bundle()
    test_external_venv_base_interpreter_is_not_a_runtime_bundle()
    test_self_contained_python_runtime_is_materialized_and_attested()
    test_system_interpreter_is_not_bundled_from_external_venv()
    test_relocation_dependent_base_store_is_bundled()
    test_bare_copy_probe_runs_once_per_interpreter()
    test_bare_copy_probe_spawn_failure_fails_closed()
    print("PASS provider runner runtime classification")
