#!/usr/bin/env python3
"""Locks _development_runtime's tolerance of a stale/unreadable dependency.

Before the fix, `FrozenBundleIdentity.capture` raising
`ExecutionContractError` (e.g. a pruned conda pkgs-cache symlink target
under the interpreter's runtime root, as hit live: turn_manager crashed a
whole turn with `FileNotFoundError` -> `ExecutionContractError("frozen
bundle is unreadable")` deep inside `provider_frozen_bundle.scan`)
propagated out of `_development_runtime` uncaught. Every caller already
treats a `None` `development_runtime` as "skip check" (see
`RunnerLaunch._attest`), so a stale fingerprint dependency must degrade to
`None` instead of crashing the turn.
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
from codex_execution_identity import FileIdentity  # noqa: E402
from provider_runner_launch import _development_runtime  # noqa: E402


def test_development_runtime_tolerates_unresolvable_symlink() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory(prefix="dev-runtime-") as raw:
        tmp_root = Path(raw).resolve()
        prefix_root = tmp_root / "venv"
        stdlib_root = prefix_root / "lib" / "python3.99"
        stdlib_root.mkdir(parents=True)
        bin_dir = prefix_root / "bin"
        bin_dir.mkdir()
        python_path = bin_dir / "python"
        python_path.write_bytes(b"#!/bin/sh\n")
        python_path.chmod(0o700)

        # A broken symlink anywhere under the scanned runtime root (not
        # under site-packages, which is excluded from the scan) reproduces
        # the exact failure mode observed live: a conda pkgs-cache entry
        # pruned out from under a still-referenced dylib symlink.
        (stdlib_root / "libstale.dylib").symlink_to("does/not/exist")

        base_root = tmp_root / "base"
        base_root.mkdir()

        executable = FileIdentity.capture(python_path)

        with (
            mock.patch.object(sys, "executable", str(python_path)),
            mock.patch.object(sys, "prefix", str(prefix_root)),
            mock.patch.object(sys, "base_prefix", str(base_root)),
            mock.patch.object(
                provider_runner_launch.sysconfig,
                "get_path",
                lambda name: str(stdlib_root),
            ),
        ):
            result = _development_runtime(executable)

    assert result is None
