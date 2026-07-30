#!/usr/bin/env python3
"""Locks _development_runtime's handling of stale host-installation state.

A self-locating host runtime (conda/homebrew/pyenv style: the bare pinned
interpreter copy boots on its own) must never have its base installation
scanned as a frozen bundle — a conda base_prefix is a multi-GB mutable tree
whose pkgs/ cache legitimately contains dangling symlinks, so scanning it
either crashes the turn ("frozen bundle is unreadable") or hashes the whole
installation. The bare-copy boot probe decides this before any capture.

A relocation-dependent runtime (uv python-build-standalone style: the bare
copy dies before main()) genuinely needs its runtime root bundled, so a
capture failure there is a real launch failure and must propagate loudly
instead of silently degrading to a bare copy that aborts at spawn.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _test_home  # noqa: E402

_test_home.isolate(prefix="development-runtime-stale-dep-")

import provider_runner_launch  # noqa: E402
from codex_execution_common import ExecutionContractError  # noqa: E402
from codex_execution_identity import FileIdentity  # noqa: E402
from provider_runner_launch import _development_runtime  # noqa: E402


def _host_base_layout(tmp_root: Path, *, probe_exit: int) -> tuple[Path, Path, Path]:
    """Venv-over-host-base topology: interpreter and stdlib resolve under
    base_prefix, with a conda-pkgs-style dangling symlink in the base."""
    base_root = tmp_root / "base"
    stdlib_root = base_root / "lib" / "python3.99"
    stdlib_root.mkdir(parents=True)
    bin_dir = base_root / "bin"
    bin_dir.mkdir()
    python_path = bin_dir / "python"
    python_path.write_bytes(f"#!/bin/sh\nexit {probe_exit}\n".encode())
    python_path.chmod(0o755)
    pkgs = base_root / "pkgs"
    pkgs.mkdir()
    (pkgs / "libstale.dylib").symlink_to("does/not/exist")
    prefix_root = tmp_root / "venv"
    prefix_root.mkdir()
    return python_path, prefix_root, stdlib_root


def _classify(python_path: Path, prefix_root: Path, stdlib_root: Path):
    executable = FileIdentity.capture(python_path)
    with (
        mock.patch.object(sys, "executable", str(python_path)),
        mock.patch.object(sys, "prefix", str(prefix_root)),
        mock.patch.object(sys, "base_prefix", str(stdlib_root.parents[1])),
        mock.patch.object(
            provider_runner_launch.sysconfig,
            "get_path",
            lambda name: str(stdlib_root),
        ),
    ):
        return _development_runtime(executable)


def test_self_locating_base_runtime_with_stale_cache_is_not_scanned() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory(prefix="dev-runtime-") as raw:
        python_path, prefix_root, stdlib_root = _host_base_layout(
            Path(raw).resolve(),
            probe_exit=0,
        )
        with mock.patch.object(
            provider_runner_launch.FrozenBundleIdentity,
            "capture",
            side_effect=AssertionError("host installation was scanned"),
        ):
            result = _classify(python_path, prefix_root, stdlib_root)
    assert result is None


def test_relocation_dependent_runtime_capture_failure_propagates() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory(prefix="dev-runtime-") as raw:
        python_path, prefix_root, stdlib_root = _host_base_layout(
            Path(raw).resolve(),
            probe_exit=1,
        )
        try:
            _classify(python_path, prefix_root, stdlib_root)
        except ExecutionContractError:
            pass
        else:
            raise AssertionError(
                "unreadable bundle for a relocation-dependent runtime "
                "did not propagate",
            )


if __name__ == "__main__":
    test_self_locating_base_runtime_with_stale_cache_is_not_scanned()
    test_relocation_dependent_runtime_capture_failure_propagates()
    print("PASS development runtime stale dependency")
