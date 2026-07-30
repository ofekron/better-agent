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
    parallel_map_processes,
    required_integer,
    required_string,
    timed_contract_step,
)
from codex_execution_identity import (
    FileIdentity,
    file_identity_from_dict,
    file_identity_to_dict,
)
from provider_launch_identity import DirectoryIdentity, _require_object
from paths import ba_home, make_private_directory, windows_path_has_private_acl


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
    # Validated relative paths equal their as_posix form, so exclusion
    # matching on POSIX reduces to exact string equality/prefix tests.
    # Windows keeps the case-folding PurePath comparisons.
    if os.name == "nt":
        def is_excluded(relative_path: str) -> bool:
            relative = Path(relative_path)
            return any(
                relative == excluded or relative.is_relative_to(excluded)
                for excluded in excluded_paths
            )
    else:
        excluded_raw = tuple(path.as_posix() for path in excluded_paths)

        def is_excluded(relative_path: str) -> bool:
            return any(
                relative_path == excluded
                or relative_path.startswith(excluded + "/")
                for excluded in excluded_raw
            )

    def scan(directory: str | Path, relative_prefix: str) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ExecutionContractError("frozen bundle is unreadable") from exc
        for child in children:
            relative_path = (
                f"{relative_prefix}/{child.name}"
                if relative_prefix
                else child.name
            )
            if excluded_paths and is_excluded(relative_path):
                continue
            try:
                observed = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise ExecutionContractError(
                    "frozen bundle is unreadable",
                ) from exc
            mode = stat.S_IMODE(observed.st_mode)
            if stat.S_ISLNK(observed.st_mode):
                path = Path(child.path)
                try:
                    target = os.readlink(path)
                    resolved_target = path.resolve(strict=True)
                except OSError as exc:
                    raise ExecutionContractError(
                        "frozen bundle is unreadable",
                    ) from exc
                _safe_symlink_target(Path(relative_path), target)
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
                        path=Path(child.path),
                        relative_path=relative_path,
                        kind="directory",
                        mode=mode,
                    ),
                )
                scan(child.path, relative_path)
                continue
            if not stat.S_ISREG(observed.st_mode):
                raise ExecutionContractError(
                    "frozen bundle contains unsupported file type",
                )
            entries.append(
                _ObservedBundleEntry(
                    path=Path(child.path),
                    relative_path=relative_path,
                    kind="file",
                    mode=mode,
                ),
            )

    scan(root, "")
    return tuple(
        sorted(entries, key=lambda entry: entry.relative_path),
    )


def _capture_file_identity(path: Path) -> FileIdentity:
    return FileIdentity.capture(path)


def _capture_scanned_file(observed: _ObservedBundleEntry) -> FileIdentity:
    identity = FileIdentity.capture(observed.path)
    if Path(identity.resolved_path) != observed.path.resolve(strict=True):
        raise ExecutionContractError("frozen bundle file escapes bundle")
    return identity


def _scan_entries(
    root: Path,
    excluded_paths: tuple[Path, ...] = (),
) -> tuple[FrozenBundleEntry, ...]:
    observed_entries = _scan_observed_entries(root, excluded_paths)
    file_entries = [
        observed for observed in observed_entries if observed.kind == "file"
    ]
    identities = dict(
        zip(
            (observed.relative_path for observed in file_entries),
            parallel_map_processes(_capture_scanned_file, file_entries),
        ),
    )
    return tuple(
        FrozenBundleEntry(
            relative_path=observed.relative_path,
            kind=observed.kind,
            mode=observed.mode,
            file=identities.get(observed.relative_path),
            target=observed.target,
        )
        for observed in observed_entries
    )


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
        with timed_contract_step("provider.frozen_bundle.capture"):
            return cls._capture(
                executable_path=executable_path,
                bundle_root=bundle_root,
                sidecar_root=sidecar_root,
                excluded_relative_paths=excluded_relative_paths,
            )

    @classmethod
    def _capture(
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
        with timed_contract_step("provider.frozen_bundle.attest"):
            return self._attest()

    def _attest(self) -> bool:
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

    def attest_metadata(self) -> bool:
        """Cheap re-check for a bundle this same instance already fully
        hash-attested earlier in the same launch/spawn cycle.

        Re-walks the directory layout (stat only - no content hashing,
        no worker-process spawns) to catch added/removed/retyped
        entries, then re-checks each known file's stat tuple instead of
        re-hashing every file in the bundle (which, for a dev-venv or
        packaged-app bundle, can be hundreds of files)."""
        with timed_contract_step("provider.frozen_bundle.attest_metadata"):
            try:
                self.validate()
                if not self.root.attest():
                    return False
                root = Path(self.root.requested_path)
                excluded = tuple(
                    Path(path) for path in self.excluded_relative_paths
                )
                if _capture_layout(root, excluded) != _expected_layout(self):
                    return False
            except (ExecutionContractError, OSError):
                return False
            return all(
                entry.file.attest_metadata()
                for entry in self.entries
                if entry.file is not None
            )

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
    _relative_path(bundle.executable_relative, "executable path")
    _relative_path(bundle.sidecar_relative, "sidecar path")
    entry_raw_paths = tuple(entry.relative_path for entry in bundle.entries)
    excluded_raw_paths = bundle.excluded_relative_paths
    for path in excluded_raw_paths:
        _relative_path(path, "excluded path")
    entries_by_path = {
        entry.relative_path: entry for entry in bundle.entries
    }
    for entry in bundle.entries:
        _validate_entry(entry)
    # _relative_path guarantees raw == Path(raw).as_posix(), so on POSIX
    # string ordering, uniqueness, and prefix tests equal the PurePath
    # comparisons this used to spell out object-by-object.
    invalid = (
        not SHA256_RE.fullmatch(bundle.fingerprint)
        or bundle.fingerprint != bundle._computed_fingerprint()
        or entry_raw_paths != tuple(sorted(entry_raw_paths))
        or len(entries_by_path) != len(entry_raw_paths)
        or excluded_raw_paths != tuple(sorted(excluded_raw_paths))
        or len(set(excluded_raw_paths)) != len(excluded_raw_paths)
        or any(
            entry == excluded or entry.startswith(excluded + "/")
            for entry in entry_raw_paths
            for excluded in excluded_raw_paths
        )
        or entries_by_path.get(bundle.executable_relative) is None
        or entries_by_path[bundle.executable_relative].kind != "file"
        or entries_by_path.get(bundle.sidecar_relative) is None
        or entries_by_path[bundle.sidecar_relative].kind != "directory"
    )
    if not invalid and os.name == "nt":
        # Windows path comparison folds case; keep the PurePath-based
        # duplicate/overlap rejection exactly as strict there.
        entry_paths = tuple(Path(path) for path in entry_raw_paths)
        excluded_paths = tuple(Path(path) for path in excluded_raw_paths)
        invalid = (
            len(set(entry_paths)) != len(entry_paths)
            or len(set(excluded_paths)) != len(excluded_paths)
            or any(
                entry == excluded or entry.is_relative_to(excluded)
                for entry in entry_paths
                for excluded in excluded_paths
            )
        )
    if invalid:
        raise ExecutionContractError("invalid frozen bundle identity")
    root = Path(bundle.root.resolved_path)
    root_str = str(root)
    root_prefix = root_str.rstrip(os.sep) + os.sep
    for entry in bundle.entries:
        if entry.file is None:
            continue
        expected = os.path.join(root_str, *entry.relative_path.split("/"))
        requested_matches = entry.file.requested_path == expected or (
            Path(entry.file.requested_path) == Path(expected)
        )
        resolved_contained = entry.file.resolved_path.startswith(
            root_prefix,
        ) or Path(entry.file.resolved_path).is_relative_to(root)
        if not requested_matches or not resolved_contained:
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
        file_entries = [
            entry for entry in bundle.entries if entry.file is not None
        ]
        observed_identities = parallel_map_processes(
            _capture_file_identity,
            tuple(
                resolved / entry.relative_path for entry in file_entries
            ),
        )
        for entry, observed in zip(file_entries, observed_identities):
            assert entry.file is not None
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
            # Preserve the source mtime: a fresh mtime marks shipped
            # __pycache__ entries stale, so executing the copy would
            # regenerate them in place and break later attestation of
            # the shared cache entry.
            os.utime(
                destination,
                ns=(identity.mtime_ns, identity.mtime_ns),
                follow_symlinks=False,
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


def _adopt_materialized_bundle(
    bundle: FrozenBundleIdentity,
    target: Path,
) -> Path | None:
    if not target.exists() or target.is_symlink():
        return None
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


def materialize_frozen_bundle(
    bundle: FrozenBundleIdentity,
    destination: str | Path,
) -> Path:
    with timed_contract_step("provider.frozen_bundle.materialize"):
        return _materialize_frozen_bundle(bundle, destination)


def _materialize_frozen_bundle(
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
    adopted = _adopt_materialized_bundle(bundle, target)
    if adopted is not None:
        return adopted
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
    try:
        parent = _safe_materialization_parent(target)
    except ExecutionContractError:
        # A concurrent materialization may have claimed the target between
        # the adoption check above and here; adopt it if it attests.
        adopted = _adopt_materialized_bundle(bundle, target)
        if adopted is not None:
            return adopted
        raise
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
        try:
            os.rename(temporary, target)
        except OSError:
            # First writer wins: if a concurrent materialization already
            # populated the target with an attested copy, adopt it.
            adopted = _adopt_materialized_bundle(bundle, target)
            if adopted is None:
                raise
            shutil.rmtree(temporary, ignore_errors=True)
            return adopted
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target.resolve(strict=True)


_BUNDLE_CACHE_SEGMENTS = ("cache", "frozen-bundles")


def frozen_bundle_destination(bundle: FrozenBundleIdentity) -> Path:
    """Stable, fingerprint-keyed materialization target under the state home.

    The same bundle always maps to the same destination, so one attested
    copy is materialized once and reused by every subsequent run.
    """
    if not isinstance(bundle, FrozenBundleIdentity):
        raise ExecutionContractError("invalid frozen bundle authority")
    root_name = Path(bundle.root.resolved_path).name
    if (
        not root_name
        or root_name in {".", ".."}
        or not SHA256_RE.fullmatch(bundle.fingerprint)
    ):
        raise ExecutionContractError("invalid frozen bundle destination")
    try:
        ancestor = ba_home()
        for name in (*_BUNDLE_CACHE_SEGMENTS, bundle.fingerprint):
            ancestor = ancestor / name
            ancestor.mkdir(mode=0o700, exist_ok=True)
            make_private_directory(ancestor)
    except (OSError, PermissionError) as exc:
        raise ExecutionContractError(
            "frozen bundle cache is unavailable",
        ) from exc
    return ancestor / root_name


def remove_materialized_bundle(root: str | Path) -> None:
    target = Path(root)
    if (
        not target.is_absolute()
        or target.is_symlink()
        or not target.is_dir()
    ):
        raise ExecutionContractError("invalid materialized bundle root")
    for directory, names, _ in os.walk(target):
        for name in names:
            child = Path(directory) / name
            if not child.is_symlink():
                os.chmod(child, 0o700)
    os.chmod(target, 0o700)
    shutil.rmtree(target)


__all__ = [
    "FrozenBundleEntry",
    "FrozenBundleIdentity",
    "attest_materialized_frozen_bundle",
    "frozen_bundle_destination",
    "materialize_frozen_bundle",
    "remove_materialized_bundle",
]
