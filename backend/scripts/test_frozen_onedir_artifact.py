#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codex_execution_common import ExecutionContractError
from provider_frozen_bundle import (
    FrozenBundleIdentity,
    attest_materialized_frozen_bundle,
    materialize_frozen_bundle,
)


def _fixture(parent: Path) -> tuple[Path, Path, Path]:
    root = parent / "Better Agent"
    sidecar = root / "_internal"
    sidecar.mkdir(parents=True)
    executable = root / (
        "Better Agent.exe" if sys.platform == "win32" else "Better Agent"
    )
    executable.write_bytes(b"frozen-executable")
    executable.chmod(0o500)
    package = sidecar / "claude_agent_sdk"
    package.mkdir()
    (package / "__init__.py").write_bytes(b"SDK_AUTHORITY = True\n")
    metadata = sidecar / "claude_agent_sdk-0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_bytes(b"Name: claude-agent-sdk\n")
    (sidecar / "runner_agy.pyc").write_bytes(b"agy-runner")
    if os.name != "nt":
        os.symlink(
            "claude_agent_sdk/__init__.py",
            sidecar / "sdk-link",
        )
    return root, executable, sidecar


def _capture(parent: Path) -> tuple[
    FrozenBundleIdentity,
    Path,
    Path,
]:
    root, executable, sidecar = _fixture(parent)
    bundle = FrozenBundleIdentity.capture(
        executable_path=executable,
        bundle_root=root,
        sidecar_root=sidecar,
    )
    return bundle, root, sidecar


def test_complete_bundle_round_trip_and_materialization() -> None:
    with tempfile.TemporaryDirectory(prefix="frozen-bundle-") as raw:
        parent = Path(raw)
        bundle, root, _ = _capture(parent)
        restored = FrozenBundleIdentity.from_dict(
            json.loads(json.dumps(bundle.to_dict())),
        )
        assert restored == bundle
        assert restored.attest()
        run_dir = parent / "run"
        run_dir.mkdir(mode=0o700)
        destination = run_dir / "frozen-runner"
        previous_umask = os.umask(0o077)
        try:
            materialized = materialize_frozen_bundle(
                restored,
                destination,
            )
        finally:
            os.umask(previous_umask)
        assert materialized == destination.resolve()
        assert attest_materialized_frozen_bundle(restored, destination)
        assert (
            destination / restored.executable_relative
        ).read_bytes() == b"frozen-executable"
        assert materialize_frozen_bundle(restored, destination) == materialized
        source_paths = {
            entry.relative_path for entry in restored.entries
        }
        target_paths = {
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
        }
        assert target_paths == source_paths
        assert root.exists()


def test_source_add_change_remove_and_mode_tamper_are_rejected() -> None:
    mutations = (
        lambda root, sidecar: (sidecar / "injected.py").write_bytes(b"x"),
        lambda root, sidecar: (
            sidecar / "claude_agent_sdk" / "__init__.py"
        ).write_bytes(b"changed"),
        lambda root, sidecar: (
            sidecar / "runner_agy.pyc"
        ).unlink(),
        lambda root, sidecar: (
            root / (
                "Better Agent.exe"
                if sys.platform == "win32"
                else "Better Agent"
            )
        ).chmod(0o700),
    )
    for mutate in mutations:
        with tempfile.TemporaryDirectory(prefix="frozen-tamper-") as raw:
            bundle, root, sidecar = _capture(Path(raw))
            mutate(root, sidecar)
            assert not bundle.attest()
            run_dir = Path(raw) / "run"
            run_dir.mkdir(mode=0o700)
            try:
                materialize_frozen_bundle(
                    bundle,
                    run_dir / "frozen-runner",
                )
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("source tamper was accepted")


def test_materialized_add_change_remove_and_mode_tamper_are_rejected() -> None:
    def mutate_add(target: Path, bundle: FrozenBundleIdentity) -> None:
        (target / "injected.py").write_bytes(b"x")

    def mutate_change(target: Path, bundle: FrozenBundleIdentity) -> None:
        executable = target / bundle.executable_relative
        executable.chmod(0o700)
        executable.write_bytes(b"changed")

    def mutate_remove(target: Path, bundle: FrozenBundleIdentity) -> None:
        regular = next(
            entry for entry in bundle.entries
            if entry.kind == "file"
            and entry.relative_path != bundle.executable_relative
        )
        (target / regular.relative_path).unlink()

    def mutate_mode(target: Path, bundle: FrozenBundleIdentity) -> None:
        executable = target / bundle.executable_relative
        executable.chmod(
            stat.S_IMODE(executable.stat().st_mode) | stat.S_IWUSR,
        )

    for mutate in (
        mutate_add,
        mutate_change,
        mutate_remove,
        mutate_mode,
    ):
        with tempfile.TemporaryDirectory(prefix="materialized-tamper-") as raw:
            parent = Path(raw)
            bundle, _, _ = _capture(parent)
            run_dir = parent / "run"
            run_dir.mkdir(mode=0o700)
            destination = run_dir / "frozen-runner"
            materialize_frozen_bundle(bundle, destination)
            mutate(destination, bundle)
            assert not attest_materialized_frozen_bundle(
                bundle,
                destination,
            )
            try:
                materialize_frozen_bundle(bundle, destination)
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("materialized tamper was accepted")


def main() -> None:
    test_complete_bundle_round_trip_and_materialization()
    test_source_add_change_remove_and_mode_tamper_are_rejected()
    test_materialized_add_change_remove_and_mode_tamper_are_rejected()
    print("frozen onedir artifact tests passed")


if __name__ == "__main__":
    main()
