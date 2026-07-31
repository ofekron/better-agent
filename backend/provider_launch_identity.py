from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from codex_execution_common import (
    ExecutionContractError,
    SECRET_NAMES,
    binary_open_flags,
    cached_sha256_fd,
    parallel_map,
    required_integer,
    required_string,
    stable_stat_identity,
    symlink_chain,
)
from codex_execution_identity import (
    FileIdentity,
    file_identity_from_dict,
    file_identity_to_dict,
)


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_LAUNCH_MODES = frozenset({
    "native",
    "posix-shebang",
    "runner-dev",
    "runner-frozen",
})


def _require_object(
    raw: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if type(raw) is not dict or set(raw) != expected:
        raise ExecutionContractError(f"invalid {label}")


def _capture_directory_stat(
    raw_path: str | Path,
) -> tuple[Path, Path, tuple[tuple[str, str], ...], os.stat_result]:
    """TOCTOU-safe directory (requested, resolved, symlink_chain, stat) capture.

    Shared by DirectoryIdentity (full content-metadata identity) and
    DirectoryScopeIdentity (path-confinement-only identity): both need the
    same before/after re-stat guard against a directory swap happening
    mid-capture, they just disagree on which of the captured stat fields are
    later treated as attestation-relevant.
    """
    requested = Path(raw_path)
    if not requested.is_absolute():
        raise ExecutionContractError("authority directory must be absolute")
    try:
        chain_before = symlink_chain(requested)
        resolved_before = requested.resolve(strict=True)
        before = resolved_before.stat()
        chain_after = symlink_chain(requested)
        resolved_after = requested.resolve(strict=True)
        after = resolved_after.stat()
    except (OSError, RuntimeError) as exc:
        raise ExecutionContractError(
            "authority directory is unavailable",
        ) from exc
    if (
        not stat.S_ISDIR(after.st_mode)
        or chain_before != chain_after
        or resolved_before != resolved_after
        or stable_stat_identity(before) != stable_stat_identity(after)
    ):
        raise ExecutionContractError(
            "authority directory changed during identity capture",
        )
    return requested, resolved_after, chain_after, after


@dataclass(frozen=True)
class DirectoryIdentity:
    requested_path: str
    resolved_path: str
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    symlink_chain: tuple[tuple[str, str], ...] = ()

    @classmethod
    def capture(cls, raw_path: str | Path) -> DirectoryIdentity:
        requested, resolved_after, chain_after, after = _capture_directory_stat(
            raw_path,
        )
        return cls(
            requested_path=str(requested.absolute()),
            resolved_path=str(resolved_after),
            mode=after.st_mode,
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
            ctime_ns=after.st_ctime_ns,
            device=after.st_dev,
            inode=after.st_ino,
            symlink_chain=chain_after,
        )

    def attest(self) -> bool:
        return self.attest_with_reason()[0]

    def attest_with_reason(self) -> tuple[bool, str]:
        try:
            current = DirectoryIdentity.capture(self.requested_path)
            if current == self:
                return True, "ok"
            return False, f"directory_changed:{self.requested_path}"
        except ExecutionContractError as exc:
            return False, f"directory_unavailable:{self.requested_path}:{exc}"


    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_path": self.requested_path,
            "resolved_path": self.resolved_path,
            "mode": self.mode,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "device": self.device,
            "inode": self.inode,
            "symlink_chain": [list(item) for item in self.symlink_chain],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DirectoryIdentity:
        _require_object(raw, {
            "requested_path",
            "resolved_path",
            "mode",
            "size",
            "mtime_ns",
            "ctime_ns",
            "device",
            "inode",
            "symlink_chain",
        }, "directory identity")
        requested_path = required_string(raw, "requested_path")
        resolved_path = required_string(raw, "resolved_path")
        integers = {
            key: required_integer(raw, key)
            for key in (
                "mode",
                "size",
                "mtime_ns",
                "ctime_ns",
                "device",
                "inode",
            )
        }
        chain = raw["symlink_chain"]
        if (
            not Path(requested_path).is_absolute()
            or not Path(resolved_path).is_absolute()
            or not stat.S_ISDIR(integers["mode"])
            or any(value < 0 for value in integers.values())
            or type(chain) is not list
            or any(
                type(item) is not list
                or len(item) != 2
                or any(type(value) is not str for value in item)
                for item in chain
            )
        ):
            raise ExecutionContractError("invalid directory identity")
        return cls(
            requested_path=requested_path,
            resolved_path=resolved_path,
            symlink_chain=tuple((item[0], item[1]) for item in chain),
            **integers,
        )


@dataclass(frozen=True)
class DirectoryScopeIdentity:
    """Path-confinement-only directory identity.

    Unlike DirectoryIdentity, this intentionally does NOT track mutable
    content metadata (size/mtime_ns/ctime_ns): it is for a root whose
    CONTENTS are expected to change while the identity is held live (e.g. a
    provider CLI's own config directory, which the CLI itself writes to —
    new session/lock files, rewritten settings — during a normal launch).
    A directory's mtime/ctime change whenever an entry is added or removed
    underneath it, so pinning those fields for a live-written root produces
    false-positive attestation failures unrelated to any real tamper.

    Security for what actually matters inside such a root is enforced per
    file, by ConfigIdentity/FileIdentity attestation on the exact paths the
    caller cares about (credentials, settings, resume log). This type only
    pins WHICH directory the root resolves to (device + inode + resolved
    path + symlink chain + mode), so path-escape / root-swap checks stay
    meaningful even while legitimate concurrent writes happen inside it.
    """

    requested_path: str
    resolved_path: str
    mode: int
    device: int
    inode: int
    symlink_chain: tuple[tuple[str, str], ...] = ()

    @classmethod
    def capture(cls, raw_path: str | Path) -> DirectoryScopeIdentity:
        requested, resolved_after, chain_after, after = _capture_directory_stat(
            raw_path,
        )
        return cls(
            requested_path=str(requested.absolute()),
            resolved_path=str(resolved_after),
            mode=after.st_mode,
            device=after.st_dev,
            inode=after.st_ino,
            symlink_chain=chain_after,
        )

    def attest(self) -> bool:
        return self.attest_with_reason()[0]

    def attest_with_reason(self) -> tuple[bool, str]:
        try:
            current = DirectoryScopeIdentity.capture(self.requested_path)
            if current == self:
                return True, "ok"
            return False, f"directory_scope_changed:{self.requested_path}"
        except ExecutionContractError as exc:
            return False, f"directory_scope_unavailable:{self.requested_path}:{exc}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_path": self.requested_path,
            "resolved_path": self.resolved_path,
            "mode": self.mode,
            "device": self.device,
            "inode": self.inode,
            "symlink_chain": [list(item) for item in self.symlink_chain],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DirectoryScopeIdentity:
        _require_object(raw, {
            "requested_path",
            "resolved_path",
            "mode",
            "device",
            "inode",
            "symlink_chain",
        }, "directory scope identity")
        requested_path = required_string(raw, "requested_path")
        resolved_path = required_string(raw, "resolved_path")
        integers = {
            key: required_integer(raw, key)
            for key in ("mode", "device", "inode")
        }
        chain = raw["symlink_chain"]
        if (
            not Path(requested_path).is_absolute()
            or not Path(resolved_path).is_absolute()
            or not stat.S_ISDIR(integers["mode"])
            or any(value < 0 for value in integers.values())
            or type(chain) is not list
            or any(
                type(item) is not list
                or len(item) != 2
                or any(type(value) is not str for value in item)
                for item in chain
            )
        ):
            raise ExecutionContractError("invalid directory scope identity")
        return cls(
            requested_path=requested_path,
            resolved_path=resolved_path,
            symlink_chain=tuple((item[0], item[1]) for item in chain),
            **integers,
        )


def _verify_file_handle(fd: int, identity: FileIdentity) -> None:
    observed = os.fstat(fd)
    if (
        observed.st_dev != identity.device
        or observed.st_ino != identity.inode
        or observed.st_size != identity.size
        or observed.st_mtime_ns != identity.mtime_ns
        or observed.st_ctime_ns != identity.ctime_ns
        or cached_sha256_fd(
            fd,
            (identity.resolved_path, stable_stat_identity(observed)),
        ) != identity.sha256
    ):
        raise ExecutionContractError("execution component identity mismatch")


@contextmanager
def _open_file_identities(
    identities: tuple[FileIdentity, ...],
) -> Iterator[tuple[int, ...]]:
    handles: list[int] = []
    try:
        for identity in identities:
            flags = binary_open_flags(
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(identity.resolved_path, flags)
            except OSError as exc:
                raise ExecutionContractError(
                    "execution component is unavailable",
                ) from exc
            handles.append(descriptor)
            _verify_file_handle(descriptor, identity)
        yield tuple(handles)
    finally:
        for descriptor in handles:
            os.close(descriptor)


@dataclass(frozen=True)
class AttestedLaunch:
    logical_command: str
    platform: str
    mode: str
    argv: tuple[str, ...]
    launcher: FileIdentity
    components: tuple[FileIdentity, ...]
    component_argv_indexes: tuple[int, ...]

    def attest(self) -> bool:
        # Identical FileIdentity values assert identical expectations, so
        # each unique identity is verified once, in parallel.
        return all(
            parallel_map(
                FileIdentity.attest,
                {self.launcher, *self.components},
            ),
        )

    def attest_metadata(self) -> bool:
        """Cheap stat-tuple-only re-check for a launch already fully
        attested earlier in the same launch/spawn cycle."""
        return all(
            parallel_map(
                FileIdentity.attest_metadata,
                {self.launcher, *self.components},
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_command": self.logical_command,
            "platform": self.platform,
            "mode": self.mode,
            "argv": list(self.argv),
            "launcher": file_identity_to_dict(self.launcher),
            "components": [
                file_identity_to_dict(component)
                for component in self.components
            ],
            "component_argv_indexes": list(self.component_argv_indexes),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AttestedLaunch:
        _require_object(raw, {
            "logical_command",
            "platform",
            "mode",
            "argv",
            "launcher",
            "components",
            "component_argv_indexes",
        }, "attested launch")
        argv = raw["argv"]
        components = raw["components"]
        indexes = raw["component_argv_indexes"]
        if (
            type(argv) is not list
            or not argv
            or any(type(value) is not str for value in argv)
            or type(components) is not list
            or not components
            or any(type(value) is not dict for value in components)
            or type(indexes) is not list
            or any(type(value) is not int for value in indexes)
            or type(raw["launcher"]) is not dict
        ):
            raise ExecutionContractError("invalid attested launch")
        launch = cls(
            logical_command=required_string(raw, "logical_command"),
            platform=required_string(raw, "platform"),
            mode=required_string(raw, "mode"),
            argv=tuple(argv),
            launcher=file_identity_from_dict(raw["launcher"]),
            components=tuple(
                file_identity_from_dict(component) for component in components
            ),
            component_argv_indexes=tuple(indexes),
        )
        _validate_launch(launch)
        return launch


def _validate_launch(launch: AttestedLaunch) -> None:
    indexes = launch.component_argv_indexes
    if (
        not _SAFE_NAME_RE.fullmatch(launch.logical_command)
        or not launch.platform
        or "\x00" in launch.platform
        or "\n" in launch.platform
        or launch.mode not in _LAUNCH_MODES
        or not launch.argv
        or not launch.components
        or len(indexes) != len(launch.components)
        or indexes != tuple(sorted(set(indexes)))
        or indexes[0] != 0
        or any(index < 0 or index >= len(launch.argv) for index in indexes)
        or any("\x00" in value or "\n" in value for value in launch.argv)
    ):
        raise ExecutionContractError("incoherent attested launch")
    for index, component in zip(indexes, launch.components):
        if launch.argv[index] != component.resolved_path:
            raise ExecutionContractError("incoherent attested launch component")
    if launch.mode == "native" and (
        len(launch.argv) != 1
        or indexes != (0,)
        or launch.components[0] != launch.launcher
    ):
        raise ExecutionContractError("incoherent native launch")
    if launch.mode == "posix-shebang" and (
        len(launch.components) != 2
        or indexes != (0, len(launch.argv) - 1)
        or launch.components[-1] != launch.launcher
    ):
        raise ExecutionContractError("incoherent shebang launch")
    if launch.mode == "posix-shebang" and any(
        marker in value.lower()
        for value in launch.argv[1:-1]
        for marker in SECRET_NAMES
    ):
        raise ExecutionContractError("secret launch argument is not allowed")
    if launch.mode == "runner-dev" and (
        indexes not in {(0, 1), (0, 2)}
        or (indexes == (0, 2) and launch.argv[1] != "-I")
    ):
        raise ExecutionContractError("incoherent development runner launch")
    if launch.mode == "runner-frozen" and indexes != (0,):
        raise ExecutionContractError("incoherent frozen runner launch")
    if launch.mode.startswith("runner-") and (
        launch.components[0] != launch.launcher
    ):
        raise ExecutionContractError("incoherent runner launcher")


def _shebang_line_from_first_line(first_line: bytes) -> str:
    if not first_line.startswith(b"#!"):
        return ""
    try:
        line = first_line[2:].decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ExecutionContractError("provider launcher shebang is invalid") from exc
    if not line:
        raise ExecutionContractError("provider launcher shebang is empty")
    return line


def _parse_shebang_tokens(line: str) -> tuple[str, ...]:
    # Absolute interpreter paths may legitimately contain spaces (e.g. a venv
    # installed under "~/Library/Application Support/..."). An unquoted
    # shebang line cannot distinguish embedded spaces from a following
    # argument, so prefer the whole line as one interpreter token whenever it
    # resolves to a real file before falling back to word-splitting (which
    # covers "/usr/bin/env node"-style shebangs with no spaces at all).
    whole = Path(line)
    if whole.is_absolute() and whole.exists():
        return (line,)
    try:
        values = tuple(shlex.split(line))
    except ValueError as exc:
        raise ExecutionContractError("provider launcher shebang is invalid") from exc
    if not values:
        raise ExecutionContractError("provider launcher shebang is empty")
    return values


def capture_cli_launch(
    *,
    logical_command: str,
    launcher_path: str | Path,
    search_path: str | None = None,
    platform: str | None = None,
    command_processor: str | Path | None = None,
) -> AttestedLaunch:
    effective_platform = platform or sys.platform
    if effective_platform.lower().startswith("win") and (
        Path(launcher_path).suffix.lower() in {".cmd", ".bat"}
    ):
        raise ExecutionContractError(
            "Windows command wrappers require complete chain authority",
        )
    launcher, first_line = FileIdentity.capture_with_first_line(launcher_path)
    target = Path(launcher.resolved_path)
    if effective_platform.lower().startswith("win") and (
        target.suffix.lower() in {".cmd", ".bat"}
    ):
        raise ExecutionContractError(
            "Windows command wrappers require complete chain authority",
        )
    shebang_line = _shebang_line_from_first_line(first_line)
    if not shebang_line:
        launch = AttestedLaunch(
            logical_command=logical_command,
            platform=effective_platform,
            mode="native",
            argv=(launcher.resolved_path,),
            launcher=launcher,
            components=(launcher,),
            component_argv_indexes=(0,),
        )
        _validate_launch(launch)
        return launch
    tokens = _parse_shebang_tokens(shebang_line)
    interpreter_token = tokens[0]
    interpreter_args = tokens[1:]
    if interpreter_token == "/usr/bin/env":
        if (
            len(interpreter_args) != 1
            or interpreter_args[0].startswith("-")
            or search_path is None
        ):
            raise ExecutionContractError("provider env shebang is ambiguous")
        resolved_interpreter = shutil.which(
            interpreter_args[0],
            path=search_path,
        )
        if resolved_interpreter is None:
            raise ExecutionContractError("provider interpreter is unavailable")
        shebang_args: tuple[str, ...] = ()
    else:
        interpreter_path = Path(interpreter_token)
        if not interpreter_path.is_absolute():
            raise ExecutionContractError("provider interpreter is unavailable")
        try:
            resolved_interpreter = str(interpreter_path.resolve(strict=True))
        except OSError as exc:
            raise ExecutionContractError(
                "provider interpreter is unavailable",
            ) from exc
        shebang_args = interpreter_args
    interpreter = FileIdentity.capture(resolved_interpreter)
    launch = AttestedLaunch(
        logical_command=logical_command,
        platform=effective_platform,
        mode="posix-shebang",
        argv=(
            interpreter.resolved_path,
            *shebang_args,
            launcher.resolved_path,
        ),
        launcher=launcher,
        components=(interpreter, launcher),
        component_argv_indexes=(0, len(shebang_args) + 1),
    )
    _validate_launch(launch)
    return launch
