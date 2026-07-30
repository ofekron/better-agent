from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import stat
import tempfile
from pathlib import Path

from codex_execution_common import (
    SHA256_RE,
    ExecutionContractError,
    binary_open_flags,
    canonical_json,
)
from codex_execution_identity import FileIdentity
from paths import ba_home, make_private_directory, windows_path_has_private_acl
from portable_lock import lock_ex, unlock
from provider_family_launch_attestation import (
    CriticalPackageIdentity,
    capture_critical_package,
)
from provider_runner_launch import RunnerLaunch


_SDK_PACKAGE = "claude_agent_sdk"
_EMBEDDED_SDK_PACKAGE = "claude_agent_sdk.embedded_runner"
_SDK_FILE_NAMES = frozenset({"py.typed"})


def _sdk_source_root() -> Path:
    spec = importlib.util.find_spec(_SDK_PACKAGE)
    locations = tuple(spec.submodule_search_locations or ()) if spec else ()
    if len(locations) != 1:
        raise ExecutionContractError("Claude Agent SDK package is unavailable")
    try:
        root = Path(locations[0]).resolve(strict=True)
    except OSError as exc:
        raise ExecutionContractError(
            "Claude Agent SDK package is unavailable",
        ) from exc
    if not root.is_dir():
        raise ExecutionContractError("Claude Agent SDK package is unavailable")
    return root


def _sdk_relative_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    try:
        for candidate in sorted(root.rglob("*")):
            relative = candidate.relative_to(root)
            if (
                candidate.is_symlink()
                or ".." in relative.parts
                or "__pycache__" in relative.parts
                or "_bundled" in relative.parts
            ):
                if candidate.is_symlink():
                    raise ExecutionContractError(
                        "Claude Agent SDK package contains a symlink",
                    )
                continue
            if candidate.is_file() and (
                candidate.suffix == ".py"
                or candidate.name in _SDK_FILE_NAMES
            ):
                files.append(relative)
    except OSError as exc:
        raise ExecutionContractError(
            "Claude Agent SDK package is unreadable",
        ) from exc
    if Path("__init__.py") not in files:
        raise ExecutionContractError("Claude Agent SDK package is incomplete")
    return tuple(files)


def capture_claude_sdk_package(
    package_root: str | Path | None = None,
) -> CriticalPackageIdentity:
    root = (
        Path(package_root).resolve(strict=True)
        if package_root is not None
        else _sdk_source_root()
    )
    if not root.is_dir() or root.name != _SDK_PACKAGE:
        raise ExecutionContractError("invalid Claude Agent SDK package root")
    return capture_critical_package(
        package_name=_SDK_PACKAGE,
        package_root=root,
        relative_paths=_sdk_relative_files(root),
    )


def capture_embedded_claude_sdk(
    runner: RunnerLaunch,
) -> CriticalPackageIdentity:
    try:
        runner.validate()
    except (AttributeError, ExecutionContractError) as exc:
        raise ExecutionContractError(
            "embedded Claude SDK requires frozen runner",
        ) from exc
    if (
        not isinstance(runner, RunnerLaunch)
        or not runner.frozen
        or runner.frozen_bundle is None
    ):
        raise ExecutionContractError("embedded Claude SDK requires frozen runner")
    executable = runner.launch.components[0]
    path = Path(executable.resolved_path)
    package = CriticalPackageIdentity(
        package_name=_EMBEDDED_SDK_PACKAGE,
        root=runner.frozen_bundle.root,
        files=(executable,),
    )
    if (
        not path.is_relative_to(Path(package.root.resolved_path))
        or not package.attest()
    ):
        raise ExecutionContractError(
            "embedded Claude SDK is not bound to frozen runner",
        )
    return package


def attest_embedded_claude_sdk(
    package: CriticalPackageIdentity,
    runner: RunnerLaunch,
) -> bool:
    return embedded_claude_sdk_attestation_failure(package, runner) is None


def embedded_claude_sdk_attestation_failure(
    package: CriticalPackageIdentity,
    runner: RunnerLaunch,
) -> str | None:
    try:
        runner.validate()
    except (AttributeError, ExecutionContractError):
        return "runner identity is invalid"
    if not isinstance(package, CriticalPackageIdentity):
        return "package identity type is invalid"
    if not isinstance(runner, RunnerLaunch):
        return "runner identity type is invalid"
    if not runner.frozen or runner.frozen_bundle is None:
        return "runner is not frozen"
    if package.package_name != _EMBEDDED_SDK_PACKAGE:
        return "package name is invalid"
    if package.root != runner.frozen_bundle.root:
        return "package root differs from runner bundle"
    if package.files != (runner.launch.components[0],):
        return "package executable differs from runner launch"
    return None


def _read_attested(identity: FileIdentity) -> bytes:
    # Single read pass: the pre-read check is stat-only (no hash) because
    # the content hash is verified once below, against the same bytes
    # this function reads for the copy - hashing the descriptor twice
    # (once to pre-verify, once more via `contents`) would read the same
    # file content twice for no added safety.
    flags = binary_open_flags(
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(identity.resolved_path, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_dev != identity.device
                or before.st_ino != identity.inode
                or before.st_size != identity.size
                or before.st_mtime_ns != identity.mtime_ns
                or before.st_ctime_ns != identity.ctime_ns
            ):
                raise ExecutionContractError(
                    "Claude Agent SDK source identity mismatch",
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ExecutionContractError(
            "Claude Agent SDK source is unavailable",
        ) from exc
    contents = b"".join(chunks)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or hashlib.sha256(contents).hexdigest() != identity.sha256
    ):
        raise ExecutionContractError(
            "Claude Agent SDK source changed during materialization",
        )
    return contents


def _write_private(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(
        path,
        binary_open_flags(
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        ),
        0o400,
    )
    try:
        os.fchmod(descriptor, 0o400)
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short Claude Agent SDK materialization write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def materialize_claude_sdk_package(
    package: CriticalPackageIdentity,
    destination: str | Path,
) -> Path:
    if (
        not isinstance(package, CriticalPackageIdentity)
        or package.package_name != _SDK_PACKAGE
        or not package.attest()
    ):
        raise ExecutionContractError("Claude Agent SDK authority mismatch")
    parent = Path(destination)
    if (
        not parent.is_absolute()
        or parent.exists()
        or parent.is_symlink()
    ):
        raise ExecutionContractError(
            "Claude Agent SDK destination is invalid",
        )
    try:
        container = parent.parent.resolve(strict=True)
        observed_container = parent.parent.lstat()
    except OSError as exc:
        raise ExecutionContractError(
            "Claude Agent SDK destination is unavailable",
        ) from exc
    if os.name == "nt":
        try:
            make_private_directory(container)
        except (OSError, PermissionError) as exc:
            raise ExecutionContractError(
                "Claude Agent SDK destination is unsafe",
            ) from exc
    unsafe_permissions = (
        not windows_path_has_private_acl(container)
        if os.name == "nt"
        else (
            bool(stat.S_IMODE(observed_container.st_mode) & 0o022)
            or observed_container.st_uid != os.getuid()
        )
    )
    if (
        not stat.S_ISDIR(observed_container.st_mode)
        or stat.S_ISLNK(observed_container.st_mode)
        or unsafe_permissions
    ):
        raise ExecutionContractError(
            "Claude Agent SDK destination is unsafe",
        )
    try:
        os.mkdir(parent, 0o700)
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise ExecutionContractError(
            "Claude Agent SDK destination is unavailable",
        ) from exc
    if resolved_parent.parent != container:
        shutil.rmtree(parent, ignore_errors=True)
        raise ExecutionContractError(
            "Claude Agent SDK destination escaped its container",
        )
    target = resolved_parent / _SDK_PACKAGE
    if target.exists() or target.is_symlink():
        raise ExecutionContractError(
            "Claude Agent SDK destination already exists",
        )
    source_root = Path(package.root.resolved_path)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{_SDK_PACKAGE}.",
        dir=resolved_parent,
    ))
    try:
        os.chmod(temporary, 0o700)
        for identity in package.files:
            source = Path(identity.resolved_path)
            if not source.is_relative_to(source_root):
                raise ExecutionContractError(
                    "Claude Agent SDK source escapes package root",
                )
            relative = source.relative_to(source_root)
            if relative.is_absolute() or ".." in relative.parts:
                raise ExecutionContractError(
                    "Claude Agent SDK source path is invalid",
                )
            _write_private(temporary / relative, _read_attested(identity))
        os.rename(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    for identity in package.files:
        relative = Path(identity.resolved_path).relative_to(source_root)
        materialized = FileIdentity.capture(target / relative)
        if (
            materialized.sha256 != identity.sha256
            or materialized.size != identity.size
        ):
            shutil.rmtree(target, ignore_errors=True)
            raise ExecutionContractError(
                "materialized Claude Agent SDK package mismatch",
            )
    return target


def attest_materialized_claude_sdk_package(
    package: CriticalPackageIdentity,
    package_root: str | Path,
) -> bool:
    root = Path(package_root)
    if (
        not isinstance(package, CriticalPackageIdentity)
        or package.package_name != _SDK_PACKAGE
        or not root.is_absolute()
        or root.is_symlink()
    ):
        return False
    try:
        resolved = root.resolve(strict=True)
    except OSError:
        return False
    source_root = Path(package.root.resolved_path)
    expected_relative = {
        Path(identity.resolved_path).relative_to(source_root)
        for identity in package.files
    }
    try:
        if set(_sdk_relative_files(resolved)) != expected_relative:
            return False
    except ExecutionContractError:
        return False
    for identity in package.files:
        source = Path(identity.resolved_path)
        if not source.is_relative_to(source_root):
            return False
        target = resolved / source.relative_to(source_root)
        try:
            materialized = FileIdentity.capture(target)
        except ExecutionContractError:
            return False
        if (
            materialized.sha256 != identity.sha256
            or materialized.size != identity.size
        ):
            return False
    return True


_SDK_PACKAGE_CACHE_SEGMENTS = ("cache", "claude-sdk-packages")


def _sdk_package_fingerprint(package: CriticalPackageIdentity) -> str:
    return hashlib.sha256(canonical_json(package.to_dict())).hexdigest()


def claude_sdk_package_cache_destination(
    package: CriticalPackageIdentity,
) -> Path:
    """Stable, fingerprint-keyed materialization container under the state
    home, mirroring `frozen_bundle_destination` / `sdk_launch_cache_destination`:
    the same attested SDK package always maps to the same destination, so
    one attested copy is materialized once and reused by every subsequent
    turn instead of copying the whole `claude_agent_sdk` package fresh into
    a new per-run directory every time.
    """
    if (
        not isinstance(package, CriticalPackageIdentity)
        or package.package_name != _SDK_PACKAGE
    ):
        raise ExecutionContractError(
            "invalid Claude Agent SDK package authority",
        )
    fingerprint = _sdk_package_fingerprint(package)
    if not SHA256_RE.fullmatch(fingerprint):
        raise ExecutionContractError(
            "invalid Claude Agent SDK cache fingerprint",
        )
    try:
        ancestor = ba_home()
        for name in (*_SDK_PACKAGE_CACHE_SEGMENTS, fingerprint):
            ancestor = ancestor / name
            ancestor.mkdir(mode=0o700, exist_ok=True)
            make_private_directory(ancestor)
    except (OSError, PermissionError) as exc:
        raise ExecutionContractError(
            "Claude Agent SDK cache is unavailable",
        ) from exc
    return ancestor / "root"


def materialize_claude_sdk_package_cached(
    package: CriticalPackageIdentity,
) -> Path:
    """Cache-aware wrapper around `materialize_claude_sdk_package`.

    The same attested package always maps to the same fingerprint-keyed
    destination under the state home, so a turn reuses a prior turn's
    materialized copy of the `claude_agent_sdk` package instead of
    copying it fresh every time. Concurrent first-time materializers of
    the same fingerprint are serialized with a blocking advisory lock
    (mirrors `materialize_sdk_launch_cached`'s concurrency handling).
    """
    destination = claude_sdk_package_cache_destination(package)
    target = destination / _SDK_PACKAGE
    if destination.is_dir() and attest_materialized_claude_sdk_package(
        package, target,
    ):
        return target
    lock_path = destination.parent / ".materializing.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        lock_ex(lock_fd)
        # Re-check now that we hold the lock: another process may have
        # materialized this fingerprint while we were waiting for it.
        if destination.is_dir() and attest_materialized_claude_sdk_package(
            package, target,
        ):
            return target
        if destination.exists() or destination.is_symlink():
            # A drifted/corrupted prior attempt: `materialize_claude_sdk_
            # package` requires its destination container to not already
            # exist, so a full retry needs it removed first.
            shutil.rmtree(destination, ignore_errors=True)
        materialize_claude_sdk_package(package, destination)
    finally:
        unlock(lock_fd)
        os.close(lock_fd)
    if not attest_materialized_claude_sdk_package(package, target):
        raise ExecutionContractError(
            "materialized Claude Agent SDK package mismatch",
        )
    return target


__all__ = [
    "attest_embedded_claude_sdk",
    "embedded_claude_sdk_attestation_failure",
    "attest_materialized_claude_sdk_package",
    "capture_claude_sdk_package",
    "capture_embedded_claude_sdk",
    "claude_sdk_package_cache_destination",
    "materialize_claude_sdk_package",
    "materialize_claude_sdk_package_cached",
]
