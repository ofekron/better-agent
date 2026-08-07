from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from codex_execution_common import (
    SHA256_RE,
    ExecutionContractError,
    binary_open_flags,
    canonical_json,
    record_step_since,
)
from codex_execution_identity import FileIdentity
from paths import ba_home, make_private_directory, make_private_file
from portable_lock import lock_ex, unlock
from provider_binary_relocatability import assert_relocatable_executable
from provider_frozen_bundle import (
    frozen_bundle_destination,
    materialize_frozen_bundle,
)
from provider_launch_identity import (
    AttestedLaunch,
    _open_file_identities,
    _validate_launch,
    _verify_file_handle,
)
from provider_runner_launch import RunnerLaunch

_SYSTEM_INTERPRETER_ROOTS = (
    Path("/bin"),
    Path("/usr/bin"),
)


@dataclass(frozen=True)
class PinnedLaunch:
    argv: tuple[str, ...]
    pass_fds: tuple[int, ...]


def _unlink_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _record_materialization_strategy(strategy: str) -> None:
    try:
        import perf
        perf.record_count(
            f"provider.launch.materialization.{strategy}",
            1,
        )
    except Exception:
        pass


def _finish_cloned_file(target: Path, mode: int) -> None:
    flags = binary_open_flags(os.O_RDONLY)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _clone_descriptor_macos(descriptor: int, target: Path, mode: int) -> None:
    import ctypes

    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(target.parent, directory_flags)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        clone = getattr(libc, "fclonefileat", None)
        if clone is None:
            raise OSError(errno.ENOSYS, "fclonefileat is unavailable")
        clone.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        clone.restype = ctypes.c_int
        if clone(
            descriptor,
            directory,
            os.fsencode(target.name),
            0,
        ) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    finally:
        os.close(directory)
    _finish_cloned_file(target, mode)


def _clone_descriptor_linux(descriptor: int, target: Path, mode: int) -> None:
    import fcntl

    flags = binary_open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    output = os.open(target, flags, mode)
    try:
        fcntl.ioctl(output, 0x40049409, descriptor)
        os.fchmod(output, mode)
        os.fsync(output)
    finally:
        os.close(output)


def _try_clone_descriptor(
    descriptor: int,
    target: Path,
    mode: int,
) -> bool:
    fallback_errnos = {
        errno.EXDEV,
        errno.ENOSYS,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
    try:
        if sys.platform == "darwin":
            _clone_descriptor_macos(descriptor, target, mode)
        elif sys.platform.startswith("linux"):
            fallback_errnos.update({errno.EINVAL, errno.ENOTTY})
            _clone_descriptor_linux(descriptor, target, mode)
        else:
            _record_materialization_strategy("copy")
            return False
    except OSError as exc:
        _unlink_if_present(target)
        if exc.errno in fallback_errnos:
            _record_materialization_strategy("copy")
            return False
        raise ExecutionContractError(
            "execution component cannot be materialized",
        ) from exc
    _record_materialization_strategy("cow_clone")
    return True


def _verify_source_unchanged(descriptor: int, identity: FileIdentity) -> None:
    """Cheap stat-tuple recheck of an already fully-verified source fd.

    The caller (`_open_file_identities`) already ran a full hash-verify
    of this exact fd when it opened it, moments before the copy. A
    second full re-hash here would be pure repeat work: instead, confirm
    the fd's own file didn't mutate underneath us since that verify. Any
    stat drift fails closed rather than falling back to a re-hash that
    could paper over a swapped file.
    """
    observed = os.fstat(descriptor)
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_dev != identity.device
        or observed.st_ino != identity.inode
        or observed.st_size != identity.size
        or observed.st_mtime_ns != identity.mtime_ns
        or observed.st_ctime_ns != identity.ctime_ns
    ):
        raise ExecutionContractError("execution component identity mismatch")


def _verify_materialized_descriptor(
    descriptor: int,
    target: Path,
    identity: FileIdentity,
) -> None:
    _verify_source_unchanged(descriptor, identity)
    copied = FileIdentity.capture(target)
    if (
        (copied.device, copied.inode) == (identity.device, identity.inode)
        or copied.sha256 != identity.sha256
        or copied.size != identity.size
    ):
        raise ExecutionContractError(
            "materialized execution component identity mismatch",
        )


def _secure_materialized_file(target: Path) -> None:
    if os.name != "nt":
        return
    try:
        make_private_file(target)
        os.chmod(target, stat.S_IREAD)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExecutionContractError(
            "materialized execution component cannot be secured",
        ) from exc


def _copy_descriptor(
    descriptor: int,
    target: Path,
    identity: FileIdentity,
) -> None:
    mode = 0o500
    if _try_clone_descriptor(descriptor, target, mode):
        try:
            _secure_materialized_file(target)
            _verify_materialized_descriptor(
                descriptor,
                target,
                identity,
            )
        except ExecutionContractError:
            _unlink_if_present(target)
            raise
        return
    flags = binary_open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        output = os.open(target, flags, mode)
        created = True
        try:
            if os.name != "nt":
                os.fchmod(output, mode)
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                offset = 0
                while offset < len(chunk):
                    offset += os.write(output, chunk[offset:])
            os.fsync(output)
        finally:
            os.close(output)
        _secure_materialized_file(target)
        _verify_materialized_descriptor(descriptor, target, identity)
    except OSError as exc:
        if created:
            _unlink_if_present(target)
        raise ExecutionContractError(
            "execution component cannot be materialized",
        ) from exc
    except ExecutionContractError:
        if created:
            _unlink_if_present(target)
        raise


def _is_trusted_system_stat(
    resolved: Path,
    uid: int,
    mode: int,
) -> bool:
    return (
        stat.S_ISREG(mode)
        and uid == 0
        and not mode & (stat.S_IWGRP | stat.S_IWOTH)
        and any(resolved.is_relative_to(root) for root in _SYSTEM_INTERPRETER_ROOTS)
    )


def _trusted_system_interpreter(
    descriptor: int,
    identity: FileIdentity,
) -> bool:
    try:
        observed = os.fstat(descriptor)
        resolved = Path(identity.resolved_path)
    except OSError:
        return False
    return (
        _is_trusted_system_stat(resolved, observed.st_uid, observed.st_mode)
        and _verify_file_handle(descriptor, identity) is None
    )


def trusted_system_interpreter_path(path: Path) -> bool:
    """True if `path` is a root-owned, non-writable file under a trusted
    system interpreter root (e.g. /bin, /usr/bin) — the same trust basis
    `open_pinned_launch` already uses to execute a system interpreter in
    place instead of copying it. Used to admit an out-of-materialization-root
    file in a hydrated MaterializedSdkLaunch without weakening attestation:
    the caller still hash-verifies the file via FileIdentity.attest().
    """
    try:
        observed = path.stat()
    except OSError:
        return False
    return _is_trusted_system_stat(path, observed.st_uid, observed.st_mode)


@contextmanager
def _materialization_directory(
    root: Path | None,
) -> Iterator[str]:
    if root is None:
        with tempfile.TemporaryDirectory(prefix="better-agent-launch-") as raw:
            yield raw
        return
    raw = tempfile.mkdtemp(prefix=".provider-launch-", dir=root)
    os.chmod(raw, 0o700)
    yield raw


@contextmanager
def open_pinned_launch(
    launch: AttestedLaunch,
    *,
    materialization_root: Path | None = None,
) -> Iterator[PinnedLaunch]:
    _validate_launch(launch)
    if not launch.launcher.attest_metadata():
        raise ExecutionContractError("provider launcher identity mismatch")
    if os.name == "nt" and materialization_root is None:
        from codex_execution_launch import _open_windows_locked_components

        with _open_windows_locked_components(launch.components):
            yield PinnedLaunch(launch.argv, ())
        return
    if os.name == "nt":
        from codex_execution_launch import _open_windows_locked_components

        component_handles = _open_windows_locked_components(launch.components)
    else:
        component_handles = _open_file_identities(launch.components)
    with component_handles as handles:
        with _materialization_directory(materialization_root) as raw:
            argv = list(launch.argv)
            for position, (index, descriptor) in enumerate(
                zip(launch.component_argv_indexes, handles),
            ):
                source = launch.components[position]
                if os.name == "nt" and position == 0:
                    continue
                if launch.mode in {"posix-shebang", "runner-dev"} and position == 0:
                    if _trusted_system_interpreter(descriptor, source):
                        continue
                    if launch.mode == "posix-shebang":
                        raise ExecutionContractError(
                            "provider interpreter cannot be executed safely",
                        )
                target = Path(raw) / f"{position}-{Path(argv[index]).name}"
                _copy_descriptor(descriptor, target, source)
                argv[index] = str(target)
            yield PinnedLaunch(tuple(argv), ())


@contextmanager
def open_pinned_runner_launch(
    runner: RunnerLaunch,
) -> Iterator[PinnedLaunch]:
    setup_started = time.perf_counter()
    if not isinstance(runner, RunnerLaunch):
        raise ExecutionContractError("runner launch authority mismatch")
    try:
        runner.validate()
    except ExecutionContractError as exc:
        raise ExecutionContractError(
            "runner launch authority mismatch",
        ) from exc
    run_dir = Path(
        runner.launch.argv[runner.launch.argv.index("--run-dir") + 1],
    )
    if not run_dir.is_absolute():
        raise ExecutionContractError("runner launch directory is invalid")
    try:
        make_private_directory(run_dir)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExecutionContractError(
            "runner launch directory cannot be secured",
        ) from exc
    if not runner.frozen:
        if not runner.attest():
            raise ExecutionContractError("runner launch authority mismatch")
        if runner.development_runtime is not None:
            bundle_root = materialize_frozen_bundle(
                runner.development_runtime,
                frozen_bundle_destination(runner.development_runtime),
            )
            executable = (
                bundle_root
                / runner.development_runtime.executable_relative
            )
            assert runner.runner_entry is not None
            with _open_file_identities((runner.runner_entry,)) as handles:
                with _materialization_directory(run_dir) as raw:
                    argv = list(runner.launch.argv)
                    runner_index = runner.launch.component_argv_indexes[1]
                    target = Path(raw) / (
                        "1-" + Path(argv[runner_index]).name
                    )
                    _copy_descriptor(
                        handles[0],
                        target,
                        runner.runner_entry,
                    )
                    argv[0] = str(executable)
                    argv[runner_index] = str(target)
                    record_step_since(
                        "provider.pinned_launch.open_runner",
                        setup_started,
                    )
                    yield PinnedLaunch(tuple(argv), ())
            return
        with open_pinned_launch(
            runner.launch,
            materialization_root=run_dir,
        ) as pinned:
            record_step_since(
                "provider.pinned_launch.open_runner",
                setup_started,
            )
            yield pinned
        return
    if runner.frozen_bundle is None:
        raise ExecutionContractError("frozen runner bundle authority is missing")
    bundle_root = materialize_frozen_bundle(
        runner.frozen_bundle,
        frozen_bundle_destination(runner.frozen_bundle),
    )
    executable = bundle_root / runner.frozen_bundle.executable_relative
    argv = list(runner.launch.argv)
    argv[0] = str(executable)
    record_step_since("provider.pinned_launch.open_runner", setup_started)
    yield PinnedLaunch(tuple(argv), ())


@dataclass(frozen=True)
class MaterializedSdkLaunch:
    executable_path: str
    files: tuple[FileIdentity, ...]
    payload_files: tuple[FileIdentity, ...] = ()

    def attest(self) -> bool:
        return all(identity.attest() for identity in self.files)


def _secure_materialization_root(raw_path: str | Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise ExecutionContractError("materialization root must be absolute")
    try:
        resolved = path.resolve(strict=True)
        observed = path.lstat()
    except OSError as exc:
        raise ExecutionContractError(
            "materialization root is unavailable",
        ) from exc
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
    ):
        raise ExecutionContractError("materialization root must not be a symlink")
    return resolved


def materialize_sdk_launch(
    launch: AttestedLaunch,
    destination: str | Path,
) -> MaterializedSdkLaunch:
    _validate_launch(launch)
    if not launch.launcher.attest_metadata():
        raise ExecutionContractError("provider launcher identity mismatch")
    if launch.mode not in {"native", "posix-shebang"}:
        raise ExecutionContractError(
            "SDK launch cannot materialize this launcher type",
        )
    root = _secure_materialization_root(destination)
    with _open_file_identities(launch.components) as handles:
        if launch.mode == "native":
            suffix = Path(launch.argv[0]).suffix
            executable = root / f"provider-cli{suffix}"
            _copy_descriptor(handles[0], executable, launch.components[0])
            try:
                assert_relocatable_executable(executable)
            except ExecutionContractError:
                _unlink_if_present(executable)
                raise
            files = (FileIdentity.capture(executable),)
            return MaterializedSdkLaunch(str(executable), files, files)
        interpreter_is_trusted = _trusted_system_interpreter(
            handles[0], launch.components[0],
        )
        if interpreter_is_trusted:
            # Referenced at its original path, never copied: this is the
            # same trust basis open_pinned_launch already uses to execute a
            # system interpreter in place, so it can't inherit the dyld
            # relocation hazards a copy would (see provider_binary_
            # relocatability for what those are). Its FileIdentity below
            # keeps it hash-pinned like every other materialized component.
            interpreter = Path(launch.components[0].resolved_path)
        else:
            interpreter = root / f"provider-runtime{Path(launch.argv[0]).suffix}"
            _copy_descriptor(handles[0], interpreter, launch.components[0])
            try:
                assert_relocatable_executable(interpreter)
            except ExecutionContractError:
                _unlink_if_present(interpreter)
                raise

        def _unlink_interpreter_if_copied() -> None:
            if not interpreter_is_trusted:
                _unlink_if_present(interpreter)

        script = root / f"provider-cli{Path(launch.argv[-1]).suffix}"
        arguments = launch.argv[1:-1]
        if any(any(character.isspace() for character in value) for value in (
            str(interpreter),
            *arguments,
        )):
            _unlink_interpreter_if_copied()
            raise ExecutionContractError(
                "materialized shebang path cannot contain whitespace",
            )
        os.lseek(handles[-1], 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(handles[-1], 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        source = b"".join(chunks)
        try:
            _verify_file_handle(handles[-1], launch.components[-1])
        except ExecutionContractError:
            _unlink_interpreter_if_copied()
            raise
        if hashlib.sha256(source).hexdigest() != launch.components[-1].sha256:
            _unlink_interpreter_if_copied()
            raise ExecutionContractError("SDK launcher identity mismatch")
        newline = source.find(b"\n")
        body = b"" if newline < 0 else source[newline + 1:]
        shebang = f"#!{interpreter}"
        if arguments:
            shebang += " " + " ".join(arguments)
        payload = shebang.encode("utf-8") + b"\n" + body
        flags = binary_open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        created = False
        try:
            descriptor = os.open(script, flags, 0o500)
            created = True
            try:
                os.fchmod(descriptor, 0o500)
                offset = 0
                while offset < len(payload):
                    offset += os.write(descriptor, payload[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            if created:
                _unlink_if_present(script)
            _unlink_interpreter_if_copied()
            raise ExecutionContractError(
                "SDK launcher cannot be materialized",
            ) from exc
    try:
        files = (
            FileIdentity.capture(interpreter),
            FileIdentity.capture(script),
        )
    except ExecutionContractError:
        _unlink_if_present(script)
        _unlink_interpreter_if_copied()
        raise
    payload_files = files if not interpreter_is_trusted else (files[-1],)
    return MaterializedSdkLaunch(str(script), files, payload_files)


_SDK_LAUNCH_CACHE_SEGMENTS = ("cache", "sdk-launches")


def _sdk_launch_fingerprint(launch: AttestedLaunch) -> str:
    payload = {
        "mode": launch.mode,
        "logical_command": launch.logical_command,
        "argv_tail": list(launch.argv[1:-1]) if launch.mode != "native" else [],
        "components": [
            {"sha256": component.sha256, "size": component.size}
            for component in launch.components
        ],
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def sdk_launch_cache_root() -> Path:
    """The shared, private state-home directory every SDK launch cache
    entry lives under — the additional trusted prefix a hydrated
    `MaterializedSdkLaunch` may resolve into, alongside `run_dir` and
    `trusted_system_interpreter_path`.

    Returned fully resolved (`.resolve(strict=True)`, symlinks stripped) so
    every caller that compares an already-resolved path (e.g. runner.py's
    pinned-launch check, which resolves the launched executable before
    comparing) shares the identical symlink-free identity. `ba_home()`
    itself is allowed to return an unresolved path — e.g. the
    `~/.better-agent` alias `paths._ensure_default_alias` creates pointing
    at the real `~/.better-claude` — and without resolving here too, a
    genuinely-pinned CLI would compare unequal to its own trusted root.
    """
    try:
        ancestor = ba_home()
        for name in _SDK_LAUNCH_CACHE_SEGMENTS:
            ancestor = ancestor / name
            ancestor.mkdir(mode=0o700, exist_ok=True)
            make_private_directory(ancestor)
        ancestor = ancestor.resolve(strict=True)
    except (OSError, PermissionError) as exc:
        raise ExecutionContractError(
            "SDK launch cache is unavailable",
        ) from exc
    return ancestor


def sdk_launch_cache_destination(launch: AttestedLaunch) -> Path:
    """Stable, fingerprint-keyed materialization target under the state
    home, mirroring `frozen_bundle_destination`: the same attested SDK
    launch always maps to the same destination, so one attested copy is
    materialized once and reused by every subsequent turn instead of a
    fresh copy per run.
    """
    _validate_launch(launch)
    fingerprint = _sdk_launch_fingerprint(launch)
    if not SHA256_RE.fullmatch(fingerprint):
        raise ExecutionContractError("invalid SDK launch cache fingerprint")
    root = sdk_launch_cache_root()
    fingerprint_dir = root / fingerprint
    try:
        fingerprint_dir.mkdir(mode=0o700, exist_ok=True)
        make_private_directory(fingerprint_dir)
    except (OSError, PermissionError) as exc:
        raise ExecutionContractError(
            "SDK launch cache is unavailable",
        ) from exc
    return fingerprint_dir / "root"


def _expected_sdk_component_paths(
    launch: AttestedLaunch,
    destination: Path,
) -> tuple[Path, ...]:
    if launch.mode == "native":
        suffix = Path(launch.argv[0]).suffix
        return (destination / f"provider-cli{suffix}",)
    interpreter_source = Path(launch.components[0].resolved_path)
    interpreter_path = (
        interpreter_source
        if trusted_system_interpreter_path(interpreter_source)
        else destination / f"provider-runtime{Path(launch.argv[0]).suffix}"
    )
    return (
        interpreter_path,
        destination / f"provider-cli{Path(launch.argv[-1]).suffix}",
    )


def _expected_shebang_script_sha256(
    launch: AttestedLaunch,
    interpreter: Path,
) -> str:
    # The materialized script is not a byte-identical copy of its source:
    # `materialize_sdk_launch` rewrites the shebang line to point at the
    # (destination-relative) interpreter path. A cached copy's identity is
    # therefore checked against this same deterministic rewrite rather than
    # against the source component's own sha256.
    with _open_file_identities((launch.components[-1],)) as handles:
        descriptor = handles[0]
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        source = b"".join(chunks)
        _verify_file_handle(descriptor, launch.components[-1])
    if hashlib.sha256(source).hexdigest() != launch.components[-1].sha256:
        raise ExecutionContractError("SDK launcher identity mismatch")
    newline = source.find(b"\n")
    body = b"" if newline < 0 else source[newline + 1:]
    arguments = launch.argv[1:-1]
    shebang = f"#!{interpreter}"
    if arguments:
        shebang += " " + " ".join(arguments)
    return hashlib.sha256(shebang.encode("utf-8") + b"\n" + body).hexdigest()


def _adopt_materialized_sdk_launch(
    launch: AttestedLaunch,
    destination: Path,
) -> MaterializedSdkLaunch | None:
    """Adopt an existing cache entry if it still attests.

    Mirrors `provider_frozen_bundle._adopt_materialized_bundle`: nothing
    at `destination` yet is the normal cold-cache case (returns None, the
    caller materializes fresh); something at `destination` that does NOT
    hash-verify against the attested launch is a tampered or corrupted
    shared cache entry and fails closed (raises) instead of being silently
    overwritten.
    """
    paths = _expected_sdk_component_paths(launch, destination)
    trusted_interpreter = launch.mode != "native" and trusted_system_interpreter_path(
        paths[0],
    )
    check_paths = paths[1:] if trusted_interpreter else paths
    if not any(path.exists() for path in check_paths):
        return None
    if not all(path.is_file() for path in check_paths):
        raise ExecutionContractError(
            "materialized SDK launch identity mismatch: destination layout differs",
        )
    if launch.mode == "native":
        observed = FileIdentity.capture(paths[0])
        expected = launch.components[0]
        if observed.sha256 != expected.sha256 or observed.size != expected.size:
            raise ExecutionContractError(
                "materialized SDK launch identity mismatch: hash differs",
            )
        return MaterializedSdkLaunch(str(paths[0]), (observed,), (observed,))
    interpreter, script = paths
    observed_interpreter = FileIdentity.capture(interpreter)
    if not trusted_interpreter:
        expected_interpreter = launch.components[0]
        if (
            observed_interpreter.sha256 != expected_interpreter.sha256
            or observed_interpreter.size != expected_interpreter.size
        ):
            raise ExecutionContractError(
                "materialized SDK launch identity mismatch: "
                "interpreter hash differs",
            )
    observed_script = FileIdentity.capture(script)
    if observed_script.sha256 != _expected_shebang_script_sha256(
        launch, interpreter,
    ):
        raise ExecutionContractError(
            "materialized SDK launch identity mismatch: script hash differs",
        )
    adopted_files = (observed_interpreter, observed_script)
    adopted_payload_files = (
        adopted_files if not trusted_interpreter else (observed_script,)
    )
    return MaterializedSdkLaunch(str(script), adopted_files, adopted_payload_files)


def materialize_sdk_launch_cached(launch: AttestedLaunch) -> MaterializedSdkLaunch:
    """Cache-aware wrapper around `materialize_sdk_launch`.

    The same attested launch always maps to the same fingerprint-keyed
    destination under the state home (see `sdk_launch_cache_destination`),
    so a turn reuses a prior turn's materialized copy — up to hundreds of
    MB for a CLI bundle — instead of paying a fresh copy every time.

    Materializes directly at the fingerprint-keyed destination rather than
    materializing into a scratch directory and renaming into place: a
    posix-shebang launch's script bakes its own destination-relative
    interpreter path into the shebang line, so renaming the populated
    directory afterward would leave that shebang pointing at a path that
    no longer exists. Concurrent first-time materializers of the same
    fingerprint are serialized with a blocking advisory lock (the kernel
    wakes a waiter exactly when the lock releases — no polling) rather
    than a rename-based race, since the destination cannot be populated
    behind the writer's back and then atomically swapped in.
    """
    destination = sdk_launch_cache_destination(launch)
    adopted = _adopt_materialized_sdk_launch(launch, destination)
    if adopted is not None:
        return adopted
    lock_path = destination.parent / ".materializing.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        lock_ex(lock_fd)
        # Re-check now that we hold the lock: another process may have
        # materialized this fingerprint while we were waiting for it.
        adopted = _adopt_materialized_sdk_launch(launch, destination)
        if adopted is not None:
            return adopted
        destination.mkdir(mode=0o700)
        try:
            materialize_sdk_launch(launch, destination)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    finally:
        unlock(lock_fd)
        os.close(lock_fd)
    adopted = _adopt_materialized_sdk_launch(launch, destination)
    if adopted is None:
        raise ExecutionContractError(
            "materialized SDK launch identity mismatch",
        )
    return adopted
