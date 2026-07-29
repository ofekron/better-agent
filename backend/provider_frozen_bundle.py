from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Mapping

from codex_execution_common import (
    SHA256_RE,
    ExecutionContractError,
    canonical_json,
    required_integer,
    required_string,
    sha256_fd,
)
from codex_execution_identity import (
    FileIdentity,
    file_identity_from_dict,
    file_identity_to_dict,
)
from provider_launch_identity import DirectoryIdentity, _require_object


_ENTRY_KINDS = frozenset({"directory", "file", "symlink"})


def _relative_path(raw: str, label: str) -> Path:
    value = Path(raw)
    if (
        not raw
        or value.is_absolute()
        or ".." in value.parts
        or value == Path(".")
        or value.as_posix() != raw
    ):
        raise ExecutionContractError(f"invalid frozen bundle {label}")
    return value


def _safe_symlink_target(relative: Path, target: str) -> None:
    value = Path(target)
    if not target or value.is_absolute() or "\x00" in target:
        raise ExecutionContractError("frozen bundle symlink escapes bundle")
    parts: list[str] = []
    for part in (relative.parent / value).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ExecutionContractError(
                    "frozen bundle symlink escapes bundle",
                )
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise ExecutionContractError("frozen bundle symlink escapes bundle")


@dataclass(frozen=True)
class FrozenBundleEntry:
    relative_path: str
    kind: str
    mode: int
    file: FileIdentity | None = None
    target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "kind": self.kind,
            "mode": self.mode,
            "file": (
                file_identity_to_dict(self.file)
                if self.file is not None
                else None
            ),
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FrozenBundleEntry:
        _require_object(
            raw,
            {"relative_path", "kind", "mode", "file", "target"},
            "frozen bundle entry",
        )
        file_raw = raw["file"]
        target = raw["target"]
        if (
            (file_raw is not None and type(file_raw) is not dict)
            or (target is not None and type(target) is not str)
        ):
            raise ExecutionContractError("invalid frozen bundle entry")
        value = cls(
            relative_path=required_string(raw, "relative_path"),
            kind=required_string(raw, "kind"),
            mode=required_integer(raw, "mode"),
            file=(
                file_identity_from_dict(file_raw)
                if file_raw is not None
                else None
            ),
            target=target,
        )
        _validate_entry(value)
        return value


def _validate_entry(entry: FrozenBundleEntry) -> None:
    relative = _relative_path(entry.relative_path, "entry path")
    if (
        entry.kind not in _ENTRY_KINDS
        or entry.mode < 0
        or entry.mode > 0o7777
        or (
            entry.kind == "file"
            and (entry.file is None or entry.target is not None)
        )
        or (
            entry.kind == "directory"
            and (entry.file is not None or entry.target is not None)
        )
        or (
            entry.kind == "symlink"
            and (entry.file is not None or entry.target is None)
        )
    ):
        raise ExecutionContractError("invalid frozen bundle entry")
    if entry.kind == "symlink":
        _safe_symlink_target(relative, entry.target or "")


def _scan_entries(root: Path) -> tuple[FrozenBundleEntry, ...]:
    entries: list[FrozenBundleEntry] = []

    def scan(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ExecutionContractError("frozen bundle is unreadable") from exc
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root)
            relative_path = relative.as_posix()
            try:
                observed = path.lstat()
            except OSError as exc:
                raise ExecutionContractError(
                    "frozen bundle is unreadable",
                ) from exc
            mode = stat.S_IMODE(observed.st_mode)
            if stat.S_ISLNK(observed.st_mode):
                try:
                    target = os.readlink(path)
                    resolved_target = path.resolve(strict=True)
                except OSError as exc:
                    raise ExecutionContractError(
                        "frozen bundle is unreadable",
                    ) from exc
                _safe_symlink_target(relative, target)
                if not resolved_target.is_relative_to(root):
                    raise ExecutionContractError(
                        "frozen bundle symlink escapes bundle",
                    )
                entries.append(
                    FrozenBundleEntry(
                        relative_path=relative_path,
                        kind="symlink",
                        mode=mode,
                        target=target,
                    ),
                )
                continue
            if stat.S_ISDIR(observed.st_mode):
                entries.append(
                    FrozenBundleEntry(
                        relative_path=relative_path,
                        kind="directory",
                        mode=mode,
                    ),
                )
                scan(path)
                continue
            if not stat.S_ISREG(observed.st_mode):
                raise ExecutionContractError(
                    "frozen bundle contains unsupported file type",
                )
            identity = FileIdentity.capture(path)
            if Path(identity.resolved_path) != path.resolve(strict=True):
                raise ExecutionContractError(
                    "frozen bundle file escapes bundle",
                )
            entries.append(
                FrozenBundleEntry(
                    relative_path=relative_path,
                    kind="file",
                    mode=mode,
                    file=identity,
                ),
            )

    scan(root)
    return tuple(
        sorted(entries, key=lambda entry: entry.relative_path),
    )


@dataclass(frozen=True)
class FrozenBundleIdentity:
    root: DirectoryIdentity
    executable_relative: str
    sidecar_relative: str
    entries: tuple[FrozenBundleEntry, ...]
    fingerprint: str

    @classmethod
    def capture(
        cls,
        *,
        executable_path: str | Path,
        bundle_root: str | Path | None = None,
        sidecar_root: str | Path | None = None,
    ) -> FrozenBundleIdentity:
        executable = Path(executable_path)
        if not executable.is_absolute():
            raise ExecutionContractError(
                "frozen bundle executable must be absolute",
            )
        root_path, sidecar_path = _bundle_paths(
            executable=executable,
            bundle_root=bundle_root,
            sidecar_root=sidecar_root,
        )
        root_before = DirectoryIdentity.capture(root_path)
        resolved_root = Path(root_before.resolved_path)
        executable_relative = executable.resolve(strict=True).relative_to(
            resolved_root,
        ).as_posix()
        sidecar_relative = sidecar_path.resolve(strict=True).relative_to(
            resolved_root,
        ).as_posix()
        entries = _scan_entries(resolved_root)
        root_after = DirectoryIdentity.capture(root_path)
        if root_before != root_after:
            raise ExecutionContractError(
                "frozen bundle changed during identity capture",
            )
        unsigned = cls(
            root=root_after,
            executable_relative=executable_relative,
            sidecar_relative=sidecar_relative,
            entries=entries,
            fingerprint="",
        )
        value = cls(
            root=unsigned.root,
            executable_relative=unsigned.executable_relative,
            sidecar_relative=unsigned.sidecar_relative,
            entries=unsigned.entries,
            fingerprint=unsigned._computed_fingerprint(),
        )
        _validate_bundle(value)
        return value

    def attest(self) -> bool:
        try:
            current = FrozenBundleIdentity.capture(
                executable_path=(
                    Path(self.root.requested_path)
                    / self.executable_relative
                ),
                bundle_root=self.root.requested_path,
                sidecar_root=(
                    Path(self.root.requested_path)
                    / self.sidecar_relative
                ),
            )
        except (ExecutionContractError, OSError, ValueError):
            return False
        return current == self

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root.to_dict(),
            "executable_relative": self.executable_relative,
            "sidecar_relative": self.sidecar_relative,
            "entries": [entry.to_dict() for entry in self.entries],
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FrozenBundleIdentity:
        _require_object(
            raw,
            {
                "root",
                "executable_relative",
                "sidecar_relative",
                "entries",
                "fingerprint",
            },
            "frozen bundle identity",
        )
        entries = raw["entries"]
        if (
            type(raw["root"]) is not dict
            or type(entries) is not list
            or not entries
            or any(type(entry) is not dict for entry in entries)
        ):
            raise ExecutionContractError("invalid frozen bundle identity")
        value = cls(
            root=DirectoryIdentity.from_dict(raw["root"]),
            executable_relative=required_string(
                raw,
                "executable_relative",
            ),
            sidecar_relative=required_string(raw, "sidecar_relative"),
            entries=tuple(
                FrozenBundleEntry.from_dict(entry) for entry in entries
            ),
            fingerprint=required_string(raw, "fingerprint"),
        )
        _validate_bundle(value)
        return value

    def _unsigned_dict(self) -> dict[str, Any]:
        return {
            "root": self.root.to_dict(),
            "executable_relative": self.executable_relative,
            "sidecar_relative": self.sidecar_relative,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def _computed_fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self._unsigned_dict())).hexdigest()


def _bundle_paths(
    *,
    executable: Path,
    bundle_root: str | Path | None,
    sidecar_root: str | Path | None,
) -> tuple[Path, Path]:
    if bundle_root is None:
        if not getattr(sys, "frozen", False):
            raise ExecutionContractError(
                "frozen bundle root requires frozen runtime",
            )
        executable_parent = executable.resolve(strict=True).parent
        if (
            sys.platform == "darwin"
            and executable_parent.name == "MacOS"
            and executable_parent.parent.name == "Contents"
        ):
            root = executable_parent.parent
        else:
            root = executable_parent
    else:
        root = Path(bundle_root)
    raw_sidecar = (
        sidecar_root
        if sidecar_root is not None
        else getattr(sys, "_MEIPASS", None)
    )
    if raw_sidecar is None:
        raise ExecutionContractError("frozen bundle sidecar root is unavailable")
    sidecar = Path(raw_sidecar)
    if not root.is_absolute() or not sidecar.is_absolute():
        raise ExecutionContractError("frozen bundle paths must be absolute")
    try:
        resolved_root = root.resolve(strict=True)
        resolved_executable = executable.resolve(strict=True)
        resolved_sidecar = sidecar.resolve(strict=True)
    except OSError as exc:
        raise ExecutionContractError("frozen bundle is unavailable") from exc
    if (
        not resolved_root.is_dir()
        or not resolved_executable.is_file()
        or not resolved_sidecar.is_dir()
        or not resolved_executable.is_relative_to(resolved_root)
        or not resolved_sidecar.is_relative_to(resolved_root)
        or resolved_sidecar == resolved_root
    ):
        raise ExecutionContractError("frozen bundle layout is invalid")
    return root, sidecar


def _validate_bundle(bundle: FrozenBundleIdentity) -> None:
    executable = _relative_path(
        bundle.executable_relative,
        "executable path",
    )
    sidecar = _relative_path(bundle.sidecar_relative, "sidecar path")
    entry_paths = tuple(
        _relative_path(entry.relative_path, "entry path")
        for entry in bundle.entries
    )
    entries_by_path = {
        entry.relative_path: entry for entry in bundle.entries
    }
    for entry in bundle.entries:
        _validate_entry(entry)
    if (
        not SHA256_RE.fullmatch(bundle.fingerprint)
        or bundle.fingerprint != bundle._computed_fingerprint()
        or entry_paths != tuple(sorted(entry_paths, key=PurePath.as_posix))
        or len(set(entry_paths)) != len(entry_paths)
        or executable not in entry_paths
        or sidecar not in entry_paths
        or entries_by_path.get(executable.as_posix()) is None
        or entries_by_path[executable.as_posix()].kind != "file"
        or entries_by_path.get(sidecar.as_posix()) is None
        or entries_by_path[sidecar.as_posix()].kind != "directory"
    ):
        raise ExecutionContractError("invalid frozen bundle identity")
    root = Path(bundle.root.resolved_path)
    for entry in bundle.entries:
        if entry.file is None:
            continue
        expected = root / entry.relative_path
        if (
            Path(entry.file.requested_path) != expected
            or not Path(entry.file.resolved_path).is_relative_to(root)
        ):
            raise ExecutionContractError(
                "frozen bundle file escapes bundle",
            )


def _expected_content(
    bundle: FrozenBundleIdentity,
) -> tuple[tuple[Any, ...], ...]:
    values: list[tuple[Any, ...]] = []
    for entry in bundle.entries:
        if entry.kind == "file":
            assert entry.file is not None
            values.append((
                entry.relative_path,
                entry.kind,
                entry.mode,
                entry.file.size,
                entry.file.sha256,
            ))
        else:
            values.append((
                entry.relative_path,
                entry.kind,
                entry.mode,
                entry.target,
            ))
    return tuple(values)


def _capture_materialized_content(
    root: Path,
) -> tuple[tuple[Any, ...], ...]:
    entries = _scan_entries(root)
    values: list[tuple[Any, ...]] = []
    for entry in entries:
        if entry.kind == "file":
            assert entry.file is not None
            values.append((
                entry.relative_path,
                entry.kind,
                entry.mode,
                entry.file.size,
                entry.file.sha256,
            ))
        else:
            values.append((
                entry.relative_path,
                entry.kind,
                entry.mode,
                entry.target,
            ))
    return tuple(values)


def attest_materialized_frozen_bundle(
    bundle: FrozenBundleIdentity,
    destination: str | Path,
) -> bool:
    target = Path(destination)
    if not target.is_absolute() or target.is_symlink():
        return False
    try:
        resolved = target.resolve(strict=True)
        if not resolved.is_dir():
            return False
        return _capture_materialized_content(resolved) == _expected_content(
            bundle,
        )
    except (ExecutionContractError, OSError):
        return False


def _copy_attested_file(
    entry: FrozenBundleEntry,
    destination: Path,
) -> None:
    identity = entry.file
    if identity is None:
        raise ExecutionContractError("invalid frozen bundle file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        source = os.open(identity.resolved_path, flags)
        try:
            observed = os.fstat(source)
            if (
                observed.st_dev != identity.device
                or observed.st_ino != identity.inode
                or observed.st_size != identity.size
                or observed.st_mtime_ns != identity.mtime_ns
                or observed.st_ctime_ns != identity.ctime_ns
                or sha256_fd(source) != identity.sha256
            ):
                raise ExecutionContractError(
                    "frozen bundle file identity mismatch",
                )
            os.lseek(source, 0, os.SEEK_SET)
            output = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                entry.mode,
            )
            try:
                os.fchmod(output, entry.mode)
                while chunk := os.read(source, 1024 * 1024):
                    view = memoryview(chunk)
                    while view:
                        written = os.write(output, view)
                        if written <= 0:
                            raise OSError("short frozen bundle write")
                        view = view[written:]
                os.fsync(output)
            finally:
                os.close(output)
            if (
                sha256_fd(source) != identity.sha256
                or not identity.attest_metadata()
            ):
                raise ExecutionContractError(
                    "frozen bundle changed during materialization",
                )
        finally:
            os.close(source)
    except ExecutionContractError:
        raise
    except OSError as exc:
        raise ExecutionContractError(
            "frozen bundle cannot be materialized",
        ) from exc


def _safe_materialization_parent(destination: Path) -> Path:
    if (
        not destination.is_absolute()
        or destination.exists()
        or destination.is_symlink()
    ):
        raise ExecutionContractError(
            "frozen bundle destination is invalid",
        )
    try:
        parent = destination.parent.resolve(strict=True)
        observed = destination.parent.lstat()
    except OSError as exc:
        raise ExecutionContractError(
            "frozen bundle destination is unavailable",
        ) from exc
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or stat.S_IMODE(observed.st_mode) & 0o022
        or (
            hasattr(os, "getuid")
            and observed.st_uid != os.getuid()
        )
    ):
        raise ExecutionContractError(
            "frozen bundle destination is unsafe",
        )
    return parent


def materialize_frozen_bundle(
    bundle: FrozenBundleIdentity,
    destination: str | Path,
) -> Path:
    if not isinstance(bundle, FrozenBundleIdentity) or not bundle.attest():
        raise ExecutionContractError("frozen bundle authority mismatch")
    target = Path(destination)
    if target.exists() and not target.is_symlink():
        if attest_materialized_frozen_bundle(bundle, target):
            return target.resolve(strict=True)
        raise ExecutionContractError(
            "materialized frozen bundle identity mismatch",
        )
    parent = _safe_materialization_parent(target)
    target = parent / target.name
    temporary = Path(tempfile.mkdtemp(prefix=".frozen-runner.", dir=parent))
    try:
        os.chmod(temporary, 0o700)
        for entry in bundle.entries:
            if entry.kind == "directory":
                (temporary / entry.relative_path).mkdir(mode=0o700)
        for entry in bundle.entries:
            path = temporary / entry.relative_path
            if entry.kind == "directory":
                continue
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if entry.kind == "symlink":
                assert entry.target is not None
                os.symlink(entry.target, path)
                if hasattr(os, "lchmod"):
                    os.lchmod(path, entry.mode)
                elif stat.S_IMODE(path.lstat().st_mode) != entry.mode:
                    raise ExecutionContractError(
                        "frozen bundle symlink mode cannot be preserved",
                    )
                continue
            _copy_attested_file(entry, path)
        directories = (
            entry for entry in reversed(bundle.entries)
            if entry.kind == "directory"
        )
        for entry in directories:
            os.chmod(temporary / entry.relative_path, entry.mode)
        if not attest_materialized_frozen_bundle(bundle, temporary):
            raise ExecutionContractError(
                "materialized frozen bundle identity mismatch",
            )
        os.rename(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target.resolve(strict=True)


__all__ = [
    "FrozenBundleEntry",
    "FrozenBundleIdentity",
    "attest_materialized_frozen_bundle",
    "materialize_frozen_bundle",
]
