from __future__ import annotations

import os
import platform as host_platform
import shlex
import shutil
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from codex_execution_common import (
    SECRET_NAMES,
    ExecutionContractError,
    binary_open_flags,
    sha256_and_first_line_fd,
    sha256_fd,
    stable_stat_identity,
)
from codex_execution_identity import FileIdentity


@dataclass(frozen=True)
class LaunchChain:
    logical_command: str
    mode: str
    launcher: FileIdentity
    argv_prefix: tuple[str, ...]
    components: tuple[FileIdentity, ...]
    component_argv_indexes: tuple[int, ...]

    def attest(self) -> bool:
        return self.launcher.attest() and all(
            component.attest() for component in self.components
        )

    def attest_metadata(self) -> bool:
        return self.launcher.attest_metadata() and all(
            component.attest_metadata() for component in self.components
        )

    @contextmanager
    def open_attested_components(self) -> Iterator[tuple[int, ...]]:
        handles: list[int] = []
        try:
            for component in self.components:
                flags = binary_open_flags(
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                )
                flags |= getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(component.resolved_path, flags)
                handles.append(fd)
                stat_result = os.fstat(fd)
                if (
                    stat_result.st_dev != component.device
                    or stat_result.st_ino != component.inode
                    or stat_result.st_size != component.size
                    or stat_result.st_mtime_ns != component.mtime_ns
                    or stat_result.st_ctime_ns != component.ctime_ns
                    or sha256_fd(fd) != component.sha256
                ):
                    raise ExecutionContractError(
                        "execution component identity mismatch",
                    )
            yield tuple(handles)
        finally:
            for fd in handles:
                os.close(fd)


@dataclass(frozen=True)
class PinnedLaunch:
    argv_prefix: tuple[str, ...]
    pass_fds: tuple[int, ...]


def _trusted_system_interpreter(fd: int, component: FileIdentity) -> bool:
    observed = os.fstat(fd)
    resolved = Path(component.resolved_path)
    return (
        stat.S_ISREG(observed.st_mode)
        and observed.st_uid == 0
        and not observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        and any(
            resolved.is_relative_to(root)
            for root in (Path("/bin"), Path("/usr/bin"))
        )
        and sha256_fd(fd) == component.sha256
    )


def _verify_component_handle(fd: int, component: FileIdentity) -> None:
    stat_result = os.fstat(fd)
    if (
        stat_result.st_dev != component.device
        or stat_result.st_ino != component.inode
        or stat_result.st_size != component.size
        or stat_result.st_mtime_ns != component.mtime_ns
        or stat_result.st_ctime_ns != component.ctime_ns
        or sha256_fd(fd) != component.sha256
    ):
        raise ExecutionContractError("execution component identity mismatch")


@contextmanager
def _open_windows_locked_components(
    components: tuple[FileIdentity, ...],
) -> Iterator[tuple[int, ...]]:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    invalid_handle = wintypes.HANDLE(-1).value
    handles: list[int] = []
    try:
        for component in components:
            handle = create_file(
                component.resolved_path,
                0x80000000,
                0x00000001,
                None,
                3,
                0x00200080,
                None,
            )
            if handle == invalid_handle:
                raise ExecutionContractError(
                    "execution component is unavailable",
                )
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
            handles.append(fd)
            _verify_component_handle(fd, component)
        yield tuple(handles)
    finally:
        for fd in handles:
            os.close(fd)


@contextmanager
def pinned_launch(chain: LaunchChain) -> Iterator[PinnedLaunch]:
    validate_launch_chain(chain)
    if os.name == "nt":
        with _open_windows_locked_components(chain.components):
            yield PinnedLaunch(chain.argv_prefix, ())
        return
    with chain.open_attested_components() as handles:
        if Path("/proc/self/fd").is_dir():
            argv = list(chain.argv_prefix)
            for index, fd in zip(chain.component_argv_indexes, handles):
                argv[index] = f"/proc/self/fd/{fd}"
            yield PinnedLaunch(tuple(argv), handles)
            return
        with tempfile.TemporaryDirectory(
            prefix="better-agent-codex-launch-",
        ) as raw:
            root = Path(raw)
            argv = list(chain.argv_prefix)
            for position, (index, fd) in enumerate(
                zip(chain.component_argv_indexes, handles),
            ):
                if chain.mode == "shebang" and position == 0:
                    if not _trusted_system_interpreter(
                        fd,
                        chain.components[position],
                    ):
                        raise ExecutionContractError(
                            "execution interpreter cannot be pinned",
                        )
                    continue
                # Copy from the attested descriptor instead of hardlinking:
                # os.link bumps the shared inode's ctime, which races every
                # concurrent attestation of the same binary (identity capture
                # and open-time checks both pin ctime).
                from provider_pinned_launch import _copy_descriptor

                target = root / f"{position}-{Path(argv[index]).name}"
                _copy_descriptor(
                    fd,
                    target,
                    chain.components[position],
                )
                argv[index] = str(target)
            yield PinnedLaunch(tuple(argv), ())


def validate_launch_chain(chain: LaunchChain) -> None:
    indexes = chain.component_argv_indexes
    if (
        chain.logical_command != "codex"
        or chain.mode not in {"native", "vendor", "shebang"}
        or Path(chain.launcher.requested_path).stem.lower() != "codex"
        or not chain.argv_prefix
        or not chain.components
        or len(indexes) != len(chain.components)
        or indexes != tuple(sorted(set(indexes)))
        or indexes[0] != 0
        or any(index < 0 or index >= len(chain.argv_prefix) for index in indexes)
    ):
        raise ExecutionContractError("incoherent launch chain")
    indexed = set(indexes)
    for index, component in zip(indexes, chain.components):
        if chain.argv_prefix[index] not in {
            component.requested_path,
            component.resolved_path,
        }:
            raise ExecutionContractError("incoherent launch chain")
    for index, value in enumerate(chain.argv_prefix):
        if "\x00" in value or "\n" in value:
            raise ExecutionContractError("incoherent launch chain")
        if Path(value).is_absolute() and index not in indexed:
            raise ExecutionContractError("unattested launch path")
        if index not in indexed and any(
            marker in value.lower() for marker in SECRET_NAMES
        ):
            raise ExecutionContractError("secret launch argument is not allowed")
    if chain.mode in {"native", "vendor"}:
        if (
            len(chain.components) != 1
            or chain.component_argv_indexes != (0,)
            or chain.argv_prefix != (chain.components[0].resolved_path,)
        ):
            raise ExecutionContractError("incoherent native launch chain")
        return
    if len(chain.components) != 2:
        raise ExecutionContractError("incoherent shebang launch chain")
    interpreter, script = chain.components
    if script.resolved_path != chain.launcher.resolved_path:
        raise ExecutionContractError("incoherent shebang launch chain")
    tokens = _read_shebang_tokens(
        Path(chain.launcher.resolved_path),
        expected=chain.launcher,
    )
    if tokens[0] == "/usr/bin/env":
        if len(tokens) != 2 or tokens[1].startswith("-"):
            raise ExecutionContractError("provider env shebang is ambiguous")
        if Path(interpreter.resolved_path).name != tokens[1]:
            raise ExecutionContractError("incoherent shebang interpreter")
        expected = (interpreter.resolved_path, script.resolved_path)
    else:
        try:
            resolved_interpreter = Path(tokens[0]).resolve(strict=True)
        except OSError as exc:
            raise ExecutionContractError(
                "provider interpreter is unavailable",
            ) from exc
        if resolved_interpreter != Path(interpreter.resolved_path):
            raise ExecutionContractError("incoherent shebang interpreter")
        expected = (
            interpreter.resolved_path,
            *tokens[1:],
            script.resolved_path,
        )
    if chain.argv_prefix != expected:
        raise ExecutionContractError("incoherent shebang arguments")


def _vendor_candidates(
    launcher: Path,
    resolved_target: Path,
    platform: str,
    architecture: str,
) -> list[Path]:
    lowered = platform.lower()
    normalized_arch = architecture.lower()
    if normalized_arch in {"amd64", "x86_64"}:
        normalized_arch = "x64"
    elif normalized_arch == "aarch64":
        normalized_arch = "arm64"
    if lowered.startswith("win"):
        package_pattern = f"codex-win32-{normalized_arch}"
        executable_name = "codex.exe"
    elif lowered.startswith("darwin"):
        package_pattern = f"codex-darwin-{normalized_arch}"
        executable_name = "codex"
    elif lowered.startswith("linux"):
        package_pattern = f"codex-linux-{normalized_arch}"
        executable_name = "codex"
    else:
        return []
    package_roots: list[Path] = []
    for candidate in (
        launcher.parent / "node_modules" / "@openai" / "codex",
        resolved_target.parent,
        *resolved_target.parents,
    ):
        if candidate.name == "codex" and candidate.parent.name == "@openai":
            package_roots.append(candidate)
    candidates: set[Path] = set()
    for package_root in dict.fromkeys(package_roots):
        dependency_root = package_root / "node_modules" / "@openai"
        if not dependency_root.is_dir():
            continue
        for package in dependency_root.glob(package_pattern):
            vendor = package / "vendor"
            if not vendor.is_dir():
                continue
            for candidate in vendor.glob(f"**/{executable_name}"):
                if candidate.is_file():
                    candidates.add(candidate.resolve(strict=True))
    return sorted(candidates)


def _read_shebang_tokens(
    target: Path,
    *,
    expected: FileIdentity | None = None,
) -> tuple[str, ...]:
    try:
        flags = binary_open_flags(
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags)
        try:
            stat_before = os.fstat(fd)
            digest, first_line = sha256_and_first_line_fd(fd)
            stat_after = os.fstat(fd)
            if stable_stat_identity(stat_before) != stable_stat_identity(
                stat_after,
            ):
                raise ExecutionContractError(
                    "provider launcher changed during shebang read",
                )
            if expected is not None and (
                stat_after.st_dev != expected.device
                or stat_after.st_ino != expected.inode
                or stat_after.st_size != expected.size
                or stat_after.st_mtime_ns != expected.mtime_ns
                or stat_after.st_ctime_ns != expected.ctime_ns
                or digest != expected.sha256
            ):
                raise ExecutionContractError(
                    "provider launcher identity mismatch",
                )
        finally:
            os.close(fd)
    except OSError as exc:
        raise ExecutionContractError("provider launcher is unreadable") from exc
    if not first_line.startswith(b"#!"):
        return ()
    try:
        tokens = shlex.split(first_line[2:].decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExecutionContractError("provider launcher shebang is invalid") from exc
    if not tokens:
        raise ExecutionContractError("provider launcher shebang is empty")
    return tuple(tokens)


def _shebang_prefix(
    target: Path,
    *,
    search_path: str | None,
    expected: FileIdentity,
) -> tuple[str, ...] | None:
    tokens = _read_shebang_tokens(target, expected=expected)
    if not tokens:
        return None
    interpreter = tokens[0]
    interpreter_args = list(tokens[1:])
    if interpreter == "/usr/bin/env":
        if len(interpreter_args) != 1 or interpreter_args[0].startswith("-"):
            raise ExecutionContractError("provider env shebang is ambiguous")
        resolved = shutil.which(
            interpreter_args[0],
            path=search_path or os.environ.get("PATH", ""),
        )
        if not resolved:
            raise ExecutionContractError("provider interpreter is unavailable")
        return (str(Path(resolved).resolve(strict=True)), str(target))
    interpreter_path = Path(interpreter)
    if not interpreter_path.is_absolute() or not interpreter_path.is_file():
        raise ExecutionContractError("provider interpreter is unavailable")
    return (
        str(interpreter_path.resolve(strict=True)),
        *interpreter_args,
        str(target),
    )


def resolve_codex_launch_chain(
    launcher_path: str,
    *,
    search_path: str | None = None,
    platform: str | None = None,
    architecture: str | None = None,
) -> LaunchChain:
    launcher = Path(launcher_path)
    if not launcher.is_absolute():
        raise ExecutionContractError("provider launcher must be absolute")
    launcher_identity = FileIdentity.capture(launcher)
    target = Path(launcher_identity.resolved_path)
    effective_platform = platform or sys.platform
    effective_architecture = architecture or host_platform.machine()
    vendors = _vendor_candidates(
        launcher,
        target,
        effective_platform,
        effective_architecture,
    )
    if len(vendors) > 1:
        raise ExecutionContractError("provider vendor target is ambiguous")
    if vendors:
        launch_mode = "vendor"
        argv_prefix = (str(vendors[0]),)
    elif effective_platform.lower().startswith("win") and launcher.suffix.lower() in {
        ".cmd",
        ".bat",
    }:
        raise ExecutionContractError("provider vendor target is unavailable")
    else:
        shebang_prefix = _shebang_prefix(
            target,
            search_path=search_path,
            expected=launcher_identity,
        )
        launch_mode = "shebang" if shebang_prefix else "native"
        argv_prefix = shebang_prefix or (str(target),)
    component_entries = [
        (index, Path(value))
        for index, value in enumerate(argv_prefix)
        if Path(value).is_absolute() and Path(value).is_file()
    ]
    chain = LaunchChain(
        logical_command="codex",
        mode=launch_mode,
        launcher=launcher_identity,
        argv_prefix=argv_prefix,
        components=tuple(
            FileIdentity.capture(path) for _index, path in component_entries
        ),
        component_argv_indexes=tuple(
            index for index, _path in component_entries
        ),
    )
    validate_launch_chain(chain)
    return chain
