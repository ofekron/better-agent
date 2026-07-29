from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from codex_execution_common import (
    ExecutionContractError,
    binary_open_flags,
    record_step_since,
)
from codex_execution_identity import FileIdentity
from paths import make_private_directory
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


def _copy_descriptor(
    descriptor: int,
    target: Path,
    identity: FileIdentity,
) -> None:
    mode = 0o500
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
        _verify_file_handle(descriptor, identity)
        copied = FileIdentity.capture(target)
        if copied.sha256 != identity.sha256 or copied.size != identity.size:
            raise ExecutionContractError(
                "materialized execution component identity mismatch",
            )
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
        stat.S_ISREG(observed.st_mode)
        and observed.st_uid == 0
        and not observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        and any(resolved.is_relative_to(root) for root in _SYSTEM_INTERPRETER_ROOTS)
        and _verify_file_handle(descriptor, identity) is None
    )


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
            files = (FileIdentity.capture(executable),)
            return MaterializedSdkLaunch(str(executable), files)
        interpreter = root / f"provider-runtime{Path(launch.argv[0]).suffix}"
        _copy_descriptor(handles[0], interpreter, launch.components[0])
        script = root / f"provider-cli{Path(launch.argv[-1]).suffix}"
        arguments = launch.argv[1:-1]
        if any(any(character.isspace() for character in value) for value in (
            str(interpreter),
            *arguments,
        )):
            _unlink_if_present(interpreter)
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
            _unlink_if_present(interpreter)
            raise
        if hashlib.sha256(source).hexdigest() != launch.components[-1].sha256:
            _unlink_if_present(interpreter)
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
            _unlink_if_present(interpreter)
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
        _unlink_if_present(interpreter)
        raise
    return MaterializedSdkLaunch(str(script), files)
