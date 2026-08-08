from __future__ import annotations

import errno
import hashlib
import os
import shutil
import statistics
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import _test_home

_test_home.isolate("ba-test-provider-pinned-cow-")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_execution_common import ExecutionContractError
from provider_family_launch_attestation import (
    capture_cli_launch,
    materialize_sdk_launch,
)
import perf
import provider_pinned_launch


def _write_executable(path: Path, size: int) -> tuple[str, bytes]:
    digest = hashlib.sha256()
    prefix = b""
    remaining = size
    with path.open("wb") as handle:
        while remaining:
            chunk = os.urandom(min(1024 * 1024, remaining))
            if not prefix:
                prefix = chunk[:7]
            handle.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    path.chmod(0o700)
    return digest.hexdigest(), prefix


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _materialize(
    source: Path,
    destination: Path,
) -> tuple[Path, list[str]]:
    destination.mkdir(mode=0o700)
    launch = capture_cli_launch(
        logical_command="provider",
        launcher_path=source,
        platform=sys.platform,
    )
    strategies: list[str] = []

    def record(name: str, count: int) -> None:
        if count == 1 and name.startswith(
            "provider.launch.materialization.",
        ):
            strategies.append(name.rsplit(".", 1)[-1])

    with patch.object(perf, "record_count", side_effect=record):
        materialized = materialize_sdk_launch(launch, destination)
    return Path(materialized.executable_path), strategies


def _clone_helper_name() -> str | None:
    if sys.platform == "darwin":
        return "_clone_descriptor_macos"
    if sys.platform.startswith("linux"):
        return "_clone_descriptor_linux"
    return None


def test_real_clone_preserves_private_attested_identity() -> None:
    helper_name = _clone_helper_name()
    if helper_name is None:
        return
    with tempfile.TemporaryDirectory(
        prefix="ba-provider-cow-",
    ) as raw:
        root = Path(raw)
        source = root / "provider"
        size = 32 * 1024 * 1024
        original_sha256, original_prefix = _write_executable(source, size)
        deltas = []
        targets = []
        strategies = []
        for index in range(3):
            before = shutil.disk_usage(root).free
            target, observed = _materialize(
                source,
                root / f"materialized-{index}",
            )
            after = shutil.disk_usage(root).free
            targets.append(target)
            strategies.extend(observed)
            deltas.append(max(0, before - after))

        if strategies == ["copy", "copy", "copy"]:
            print("SKIP real COW proof: filesystem reports unsupported clone")
            return
        assert strategies == ["cow_clone", "cow_clone", "cow_clone"]
        target = targets[0]
        assert source.stat().st_ino != target.stat().st_ino
        assert _sha256(target) == original_sha256
        assert target.stat().st_mode & 0o777 == 0o500
        assert statistics.median(deltas) < size // 4

        target.chmod(0o700)
        with target.open("r+b") as handle:
            handle.write(b"target!")
        with source.open("rb") as handle:
            assert handle.read(7) == original_prefix
        with target.open("rb") as handle:
            assert handle.read(7) == b"target!"


def test_unsupported_clone_uses_verified_copy() -> None:
    helper_name = _clone_helper_name()
    if helper_name is None:
        return
    with tempfile.TemporaryDirectory(
        prefix="ba-provider-copy-",
    ) as raw:
        root = Path(raw)
        source = root / "provider"
        original_sha256, _ = _write_executable(source, 1024 * 1024)
        with patch.object(
            provider_pinned_launch,
            helper_name,
            side_effect=OSError(errno.ENOTSUP, "unsupported"),
        ):
            target, strategies = _materialize(
                source,
                root / "materialized",
            )
        assert strategies == ["copy"]
        assert _sha256(target) == original_sha256
        assert source.stat().st_ino != target.stat().st_ino


def test_hardlink_clone_is_rejected() -> None:
    helper_name = _clone_helper_name()
    if helper_name is None:
        return
    with tempfile.TemporaryDirectory(
        prefix="ba-provider-hardlink-rejection-",
    ) as raw:
        root = Path(raw)
        source = root / "provider"
        _write_executable(source, 1024)

        def hardlink_clone(
            _descriptor: int,
            target: Path,
            mode: int,
        ) -> None:
            os.link(source, target)
            target.chmod(mode)

        destination = root / "materialized"
        with (
            patch.object(
                provider_pinned_launch,
                helper_name,
                side_effect=hardlink_clone,
            ),
            patch.object(
                provider_pinned_launch,
                "_verify_file_handle",
                return_value=None,
            ),
        ):
            try:
                _materialize(source, destination)
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("hardlinked execution reached launch")
        assert list(destination.iterdir()) == []


def test_clone_io_failure_fails_closed() -> None:
    helper_name = _clone_helper_name()
    if helper_name is None:
        return
    with tempfile.TemporaryDirectory(
        prefix="ba-provider-clone-failure-",
    ) as raw:
        root = Path(raw)
        source = root / "provider"
        _write_executable(source, 1024)
        destination = root / "materialized"
        with patch.object(
            provider_pinned_launch,
            helper_name,
            side_effect=OSError(errno.EIO, "I/O failure"),
        ):
            try:
                _materialize(source, destination)
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("clone I/O failure reached execution")
        assert list(destination.iterdir()) == []


def test_source_change_during_clone_is_rejected() -> None:
    helper_name = _clone_helper_name()
    if helper_name is None:
        return
    with tempfile.TemporaryDirectory(
        prefix="ba-provider-clone-race-",
    ) as raw:
        root = Path(raw)
        source = root / "provider"
        _write_executable(source, 1024 * 1024)

        def clone_then_mutate(
            descriptor: int,
            target: Path,
            mode: int,
        ) -> None:
            # Fabricate a successful clone via a plain byte-copy instead of
            # calling the real clone syscall: on filesystems without
            # reflink/clonefile support (e.g. this test image's overlayfs,
            # where FICLONE fails ENOTSUP) the real clone raises before the
            # mutation below ever runs, silently defeating this race
            # scenario. The tamper check under test
            # (`_verify_source_unchanged`) only cares that materialization
            # completed and the source then changed underneath it — not
            # which clone strategy produced the target — so this keeps the
            # test deterministic across filesystems.
            os.lseek(descriptor, 0, os.SEEK_SET)
            with target.open("wb") as handle:
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            target.chmod(mode)
            source.chmod(0o700)
            with source.open("r+b") as handle:
                handle.write(b"changed")

        destination = root / "materialized"
        with patch.object(
            provider_pinned_launch,
            helper_name,
            side_effect=clone_then_mutate,
        ):
            try:
                _materialize(source, destination)
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("source mutation reached execution")
        assert list(destination.iterdir()) == []


def main() -> None:
    test_real_clone_preserves_private_attested_identity()
    test_unsupported_clone_uses_verified_copy()
    test_hardlink_clone_is_rejected()
    test_clone_io_failure_fails_closed()
    test_source_change_during_clone_is_rejected()
    print("provider pinned launch COW integration: ok")


if __name__ == "__main__":
    main()
