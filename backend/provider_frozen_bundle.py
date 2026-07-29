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
    binary_open_flags,
    canonical_json,
    required_integer,
    required_string,
)
from codex_execution_identity import (
    FileIdentity,
    file_identity_from_dict,
    file_identity_to_dict,
)
from provider_launch_identity import DirectoryIdentity, _require_object
from paths import windows_path_has_private_acl


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


@dataclass(frozen=True)
class _ObservedBundleEntry:
    path: Path
    relative_path: str
    kind: str
    mode: int
    target: str | None = None


def _scan_observed_entries(
    root: Path,
    excluded_paths: tuple[Path, ...] = (),
) -> tuple[_ObservedBundleEntry, ...]:
    entries: list[_ObservedBundleEntry] = []

    def scan(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ExecutionContractError("frozen bundle is unreadable") from exc
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root)
            if any(
                relative == excluded
                or relative.is_relative_to(excluded)
                for excluded in excluded_paths
            ):
                continue
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
                    _ObservedBundleEntry(
                        path=path,
                        relative_path=relative_path,
                        kind="symlink",
                        mode=mode,
                        target=target,
                    ),
                )
                continue
            if stat.S_ISDIR(observed.st_mode):
                entries.append(
                    _ObservedBundleEntry(
                        path=path,
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
            entries.append(
                _ObservedBundleEntry(
                    path=path,
                    relative_path=relative_path,
                    kind="file",
                    mode=mode,
                ),
            )

    scan(root)
    return tuple(
        sorted(entries, key=lambda entry: entry.relative_path),
    )


def _scan_entries(
    root: Path,
    excluded_paths: tuple[Path, ...] = (),
) -> tuple[FrozenBundleEntry, ...]:
    entries: list[FrozenBundleEntry] = []
    for observed in _scan_observed_entries(root, excluded_paths):
        identity = None
        if observed.kind == "file":
            identity = FileIdentity.capture(observed.path)
            if Path(identity.resolved_path) != observed.path.resolve(
                strict=True,
            ):
                raise ExecutionContractError(
                    "frozen bundle file escapes bundle",
                )
        entries.append(
            FrozenBundleEntry(
                relative_path=observed.relative_path,
                kind=observed.kind,
                mode=observed.mode,
                file=identity,
                target=observed.target,
            ),
        )
    return tuple(entries)


@dataclass(frozen=True)
class FrozenBundleIdentity:
    root: DirectoryIdentity
    executable_relative: str
    sidecar_relative: str
    excluded_relative_paths: tuple[str, ...]
    entries: tuple[FrozenBundleEntry, ...]
    fingerprint: str

    def validate(self) -> None:
        _validate_bundle(self)

    @classmethod
    def capture(
        cls,
        *,
        executable_path: str | Path,
        bundle_root: str | Path | None = None,
        sidecar_root: str | Path | None = None,
        excluded_relative_paths: tuple[str, ...] = (),
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
        excluded = tuple(
            sorted(
                (
                    _relative_path(path, "excluded path")
                    for path in excluded_relative_paths
                ),
                key=PurePath.as_posix,
            ),
        )
        entries = _scan_entries(resolved_root, excluded)
        root_after = DirectoryIdentity.capture(root_path)
        if root_before != root_after:
            raise ExecutionContractError(
                "frozen bundle changed during identity capture",
            )
        unsigned = cls(
            root=root_after,
            executable_relative=executable_relative,
            sidecar_relative=sidecar_relative,
            excluded_relative_paths=tuple(
                path.as_posix() for path in excluded
            ),
            entries=entries,
            fingerprint="",
        )
        value = cls(
            root=unsigned.root,
            executable_relative=unsigned.executable_relative,
            sidecar_relative=unsigned.sidecar_relative,
            excluded_relative_paths=unsigned.excluded_relative_paths,
            entries=unsigned.entries,
            fingerprint=unsigned._computed_fingerprint(),
        )
        value.validate()
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
                excluded_relative_paths=self.excluded_relative_paths,
            )
        except (ExecutionContractError, OSError, ValueError):
            return False
        return current == self

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root.to_dict(),
            "executable_relative": self.executable_relative,
            "sidecar_relative": self.sidecar_relative,
            "excluded_relative_paths": list(self.excluded_relative_paths),
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
                "excluded_relative_paths",
                "entries",
                "fingerprint",
            },
            "frozen bundle identity",
        )
        entries = raw["entries"]
        excluded = raw["excluded_relative_paths"]
        if (
            type(raw["root"]) is not dict
            or type(entries) is not list
            or type(excluded) is not list
            or any(type(path) is not str for path in excluded)
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
            excluded_relative_paths=tuple(excluded),
            entries=tuple(
                FrozenBundleEntry.from_dict(entry) for entry in entries
            ),
            fingerprint=required_string(raw, "fingerprint"),
        )
        value.validate()
        return value

    def _unsigned_dict(self) -> dict[str, Any]:
        return {
            "root": self.root.to_dict(),
            "executable_relative": self.executable_relative,
            "sidecar_relative": self.sidecar_relative,
            "excluded_relative_paths": list(self.excluded_relative_paths),
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
            root = executable_parent.parent.parent
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
    excluded_paths = tuple(
        _relative_path(path, "excluded path")
        for path in bundle.excluded_relative_paths
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
        or excluded_paths
        != tuple(sorted(excluded_paths, key=PurePath.as_posix))
        or len(set(excluded_paths)) != len(excluded_paths)
        or any(
            entry == excluded or entry.is_relative_to(excluded)
            for entry in entry_paths
            for excluded in excluded_paths
        )
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


def _expected_layout(
    bundle: FrozenBundleIdentity,
) -> tuple[tuple[str, str, int, str | None], ...]:
    return tuple(
        (
            entry.relative_path,
            entry.kind,
            entry.mode,
            entry.target,
        )
        for entry in bundle.entries
    )


def _capture_layout(
    root: Path,
    excluded_paths: tuple[Path, ...] = (),
) -> tuple[tuple[str, str, int, str | None], ...]:
    return tuple(
        (
            entry.relative_path,
            entry.kind,
            entry.mode,
            entry.target,
        )
        for entry in _scan_observed_entries(root, excluded_paths)
    )


def attest_materialized_frozen_bundle(
    bundle: FrozenBundleIdentity,
    destination: str | Path,
) -> bool:
    return _materialized_bundle_attestation_failure(
        bundle,
        destination,
    ) is None


def _materialized_bundle_attestation_failure(
    bundle: FrozenBundleIdentity,
    destination: str | Path,
) -> str | None:
    target = Path(destination)
    if not target.is_absolute() or target.is_symlink():
        return "destination path is invalid"
    try:
        resolved = target.resolve(strict=True)
        if not resolved.is_dir():
            return "destination is not a directory"
        observed_layout = _capture_layout(resolved)
        expected_layout = _expected_layout(bundle)
        if observed_layout != expected_layout:
            if tuple(item[0] for item in observed_layout) != tuple(
                item[0] for item in expected_layout
            ):
                return "destination paths differ"
            mismatch = next(
                expected[0]
                for expected, observed in zip(
                    expected_layout,
                    observed_layout,
                )
                if expected != observed
            )
            return f"destination layout differs at {mismatch}"
        for entry in bundle.entries:
            if entry.file is None:
                continue
            observed = FileIdentity.capture(
                resolved / entry.relative_path,
            )
            if observed.size != entry.file.size:
                return f"destination size differs at {entry.relative_path}"
            if observed.sha256 != entry.file.sha256:
                return f"destination hash differs at {entry.relative_path}"
        return None
    except (ExecutionContractError, OSError):
        return "destination is unreadable"


def _copy_attested_file(
    entry: FrozenBundleEntry,
    destination: Path,
) -> None:
    identity = entry.file
    if identity is None:
        raise ExecutionContractError("invalid frozen bundle file")
    flags = binary_open_flags(
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        source = os.open(identity.resolved_path, flags)
        try:
            observed = os.fstat(source)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_dev != identity.device
                or observed.st_ino != identity.inode
                or observed.st_size != identity.size
                or observed.st_mtime_ns != identity.mtime_ns
                or observed.st_ctime_ns != identity.ctime_ns
            ):
                raise ExecutionContractError(
                    "frozen bundle file identity mismatch",
                )
            output = os.open(
                destination,
                binary_open_flags(
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                ),
                entry.mode,
            )
            try:
                os.fchmod(output, entry.mode)
                digest = hashlib.sha256()
                while chunk := os.read(source, 1024 * 1024):
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(output, view)
                        if written <= 0:
                            raise OSError("short frozen bundle write")
                        view = view[written:]
            finally:
                os.close(output)
            after = os.fstat(source)
            if not _copied_descriptor_matches_identity(
                observed,
                after,
                digest.hexdigest(),
                identity,
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


def _copied_descriptor_matches_identity(
    before: os.stat_result,
    after: os.stat_result,
    digest: str,
    identity: FileIdentity,
) -> bool:
    return (
        stat.S_ISREG(after.st_mode)
        and after.st_dev == before.st_dev == identity.device
        and after.st_ino == before.st_ino == identity.inode
        and after.st_size == before.st_size == identity.size
        and digest == identity.sha256
    )


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
    unsafe_permissions = (
        not windows_path_has_private_acl(parent)
        if os.name == "nt"
        else (
            bool(stat.S_IMODE(observed.st_mode) & 0o022)
            or observed.st_uid != os.getuid()
        )
    )
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or unsafe_permissions
    ):
        raise ExecutionContractError(
            "frozen bundle destination is unsafe",
        )
    return parent


def materialize_frozen_bundle(
    bundle: FrozenBundleIdentity,
    destination: str | Path,
) -> Path:
    if not isinstance(bundle, FrozenBundleIdentity):
        raise ExecutionContractError("frozen bundle authority mismatch")
    try:
        bundle.validate()
    except ExecutionContractError as exc:
        raise ExecutionContractError(
            "frozen bundle authority mismatch",
        ) from exc
    target = Path(destination)
    if target.exists() and not target.is_symlink():
        attestation_failure = _materialized_bundle_attestation_failure(
            bundle,
            target,
        )
        if attestation_failure is None:
            return target.resolve(strict=True)
        raise ExecutionContractError(
            "materialized frozen bundle identity mismatch: "
            + attestation_failure,
        )
    try:
        source_root = Path(bundle.root.resolved_path).resolve(strict=True)
        excluded_paths = tuple(
            Path(path) for path in bundle.excluded_relative_paths
        )
        if _capture_layout(
            source_root,
            excluded_paths,
        ) != _expected_layout(bundle):
            raise ExecutionContractError(
                "frozen bundle source layout mismatch",
            )
    except OSError as exc:
        raise ExecutionContractError(
            "frozen bundle source layout mismatch",
        ) from exc
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
        attestation_failure = _materialized_bundle_attestation_failure(
            bundle,
            temporary,
        )
        if attestation_failure is not None:
            raise ExecutionContractError(
                "materialized frozen bundle identity mismatch: "
                + attestation_failure,
            )
        os.rename(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target.resolve(strict=True)


def frozen_bundle_destination(
    bundle: FrozenBundleIdentity,
    parent: str | Path,
) -> Path:
    if not isinstance(bundle, FrozenBundleIdentity):
        raise ExecutionContractError("invalid frozen bundle authority")
    root_name = Path(bundle.root.resolved_path).name
    destination_parent = Path(parent)
    if (
        not root_name
        or root_name in {".", ".."}
        or not destination_parent.is_absolute()
    ):
        raise ExecutionContractError("invalid frozen bundle destination")
    return destination_parent / root_name


__all__ = [
    "FrozenBundleEntry",
    "FrozenBundleIdentity",
    "attest_materialized_frozen_bundle",
    "frozen_bundle_destination",
    "materialize_frozen_bundle",
]
