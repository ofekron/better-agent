from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from codex_execution_common import (
    SHA256_RE,
    ExecutionContractError,
    binary_open_flags,
    cached_sha256_fd,
    get_cached_sha256,
    read_first_line_fd,
    required_integer,
    required_string,
    sha256_and_first_line_fd,
    stable_stat_identity,
    store_cached_sha256,
    symlink_chain,
)


@dataclass(frozen=True)
class FileIdentity:
    requested_path: str
    resolved_path: str
    sha256: str
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    symlink_chain: tuple[tuple[str, str], ...] = ()

    @classmethod
    def _capture(
        cls,
        raw_path: str | Path,
        hasher: Callable[[int, str, "os.stat_result"], tuple[str, bytes]],
    ) -> tuple[FileIdentity, bytes]:
        requested = Path(raw_path)
        if not requested.is_absolute():
            raise ExecutionContractError("authority file path must be absolute")
        try:
            chain_before = symlink_chain(requested)
            resolved_before = requested.resolve(strict=True)
            flags = binary_open_flags(
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(resolved_before, flags)
            try:
                stat_before = os.fstat(fd)
                if not stat.S_ISREG(stat_before.st_mode):
                    raise ExecutionContractError("authority file is unavailable")
                digest, extra = hasher(fd, str(resolved_before), stat_before)
                stat_after = os.fstat(fd)
                if stable_stat_identity(stat_before) != stable_stat_identity(
                    stat_after,
                ):
                    raise ExecutionContractError(
                        "authority file changed during identity capture",
                    )
                chain_after = symlink_chain(requested)
                resolved_after = requested.resolve(strict=True)
                path_stat = resolved_after.stat()
                if (
                    chain_before != chain_after
                    or resolved_before != resolved_after
                    or path_stat.st_dev != stat_after.st_dev
                    or path_stat.st_ino != stat_after.st_ino
                ):
                    raise ExecutionContractError(
                        "authority path changed during identity capture",
                    )
            finally:
                os.close(fd)
            identity = cls(
                requested_path=str(requested.absolute()),
                resolved_path=str(resolved_after),
                sha256=digest,
                size=stat_after.st_size,
                mtime_ns=stat_after.st_mtime_ns,
                ctime_ns=stat_after.st_ctime_ns,
                device=stat_after.st_dev,
                inode=stat_after.st_ino,
                symlink_chain=chain_after,
            )
            return identity, extra
        except ExecutionContractError:
            raise
        except OSError as exc:
            raise ExecutionContractError("authority file is unavailable") from exc

    @classmethod
    def capture(cls, raw_path: str | Path) -> FileIdentity:
        def _hash(
            fd: int,
            resolved_path: str,
            stat_before: "os.stat_result",
        ) -> tuple[str, bytes]:
            return (
                cached_sha256_fd(
                    fd,
                    (resolved_path, stable_stat_identity(stat_before)),
                ),
                b"",
            )

        identity, _ = cls._capture(raw_path, _hash)
        return identity

    @classmethod
    def capture_with_first_line(
        cls,
        raw_path: str | Path,
    ) -> tuple[FileIdentity, bytes]:
        """Capture identity and the file's first line in one pass.

        On a cold cache miss this saves a second full open+hash of the
        same file for callers (like `capture_cli_launch`) that need both
        the attested identity and the shebang line - `capture()` + a
        separate re-read to extract the first line would hash the same
        bytes twice for no added safety. On a warm cache hit (the same
        resolved path + stable stat tuple was already hashed - by a prior
        `capture()`, a prior `capture_with_first_line()`, or any other
        full-file hash of this exact file) it reuses that digest and only
        reads the small leading prefix needed for the first line, instead
        of re-hashing the whole file again.
        """
        def _hash(
            fd: int,
            resolved_path: str,
            stat_before: "os.stat_result",
        ) -> tuple[str, bytes]:
            cache_key = (resolved_path, stable_stat_identity(stat_before))
            cached_digest = get_cached_sha256(cache_key)
            if cached_digest is not None:
                return cached_digest, read_first_line_fd(fd)
            digest, first_line = sha256_and_first_line_fd(fd)
            store_cached_sha256(cache_key, digest)
            return digest, first_line

        return cls._capture(raw_path, _hash)

    def attest(self) -> bool:
        return self.attest_with_reason()[0]

    def attest_with_reason(self) -> tuple[bool, str]:
        try:
            current = FileIdentity.capture(self.requested_path)
            if current == self:
                return True, "ok"
            return False, f"file_changed:{self.requested_path}"
        except ExecutionContractError as exc:
            return False, f"file_unavailable:{self.requested_path}:{exc}"

    def attest_metadata(self) -> bool:
        return self.attest_metadata_with_reason()[0]

    def attest_metadata_with_reason(self) -> tuple[bool, str]:
        try:
            current = FileIdentity.capture_metadata(self.requested_path)
            if self.metadata_matches(current):
                return True, "ok"
            return False, f"file_metadata_changed:{self.requested_path}"
        except ExecutionContractError as exc:
            return False, f"file_metadata_unavailable:{self.requested_path}:{exc}"


    def metadata_matches(
        self,
        current: tuple[
            str,
            int,
            int,
            int,
            int,
            int,
            tuple[tuple[str, str], ...],
        ],
    ) -> bool:
        (
            resolved_path,
            size,
            mtime_ns,
            ctime_ns,
            device,
            inode,
            symlink_chain,
        ) = current
        return (
            resolved_path == self.resolved_path
            and size == self.size
            and mtime_ns == self.mtime_ns
            and (os.name == "nt" or ctime_ns == self.ctime_ns)
            and device == self.device
            and inode == self.inode
            and symlink_chain == self.symlink_chain
        )

    @classmethod
    def capture_metadata(
        cls,
        raw_path: str | Path,
    ) -> tuple[str, int, int, int, int, int, tuple[tuple[str, str], ...]]:
        requested = Path(raw_path)
        if not requested.is_absolute():
            raise ExecutionContractError("authority file path must be absolute")
        try:
            chain = symlink_chain(requested)
            resolved = requested.resolve(strict=True)
            stat_result = resolved.stat()
        except OSError as exc:
            raise ExecutionContractError("authority file is unavailable") from exc
        if not resolved.is_file():
            raise ExecutionContractError("authority file is unavailable")
        return (
            str(resolved),
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
            stat_result.st_dev,
            stat_result.st_ino,
            chain,
        )


def file_identity_to_dict(identity: FileIdentity) -> dict[str, Any]:
    return {
        "requested_path": identity.requested_path,
        "resolved_path": identity.resolved_path,
        "sha256": identity.sha256,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
        "ctime_ns": identity.ctime_ns,
        "device": identity.device,
        "inode": identity.inode,
        "symlink_chain": [list(item) for item in identity.symlink_chain],
    }


def file_identity_from_dict(raw: Mapping[str, Any]) -> FileIdentity:
    expected = {
        "requested_path",
        "resolved_path",
        "sha256",
        "size",
        "mtime_ns",
        "ctime_ns",
        "device",
        "inode",
        "symlink_chain",
    }
    if set(raw) != expected:
        raise ExecutionContractError("invalid file identity")
    requested_path = required_string(raw, "requested_path")
    resolved_path = required_string(raw, "resolved_path")
    sha256 = required_string(raw, "sha256")
    integers = {
        key: required_integer(raw, key)
        for key in ("size", "mtime_ns", "ctime_ns", "device", "inode")
    }
    symlink_chain_raw = raw.get("symlink_chain")
    if (
        type(symlink_chain_raw) is not list
        or any(
            type(item) is not list
            or len(item) != 2
            or any(type(value) is not str for value in item)
            for item in symlink_chain_raw
        )
    ):
        raise ExecutionContractError("invalid file identity symlink chain")
    if (
        not Path(requested_path).is_absolute()
        or not Path(resolved_path).is_absolute()
        or not SHA256_RE.fullmatch(sha256)
        or any(value < 0 for value in integers.values())
    ):
        raise ExecutionContractError("invalid file identity")
    return FileIdentity(
        requested_path=requested_path,
        resolved_path=resolved_path,
        sha256=sha256,
        symlink_chain=tuple(
            (item[0], item[1]) for item in symlink_chain_raw
        ),
        **integers,
    )


def config_file_target_is_admissible(
    root_path: str | Path,
    config_path: str | Path,
    identity: FileIdentity,
) -> bool:
    root = Path(root_path)
    candidate = Path(config_path)
    if Path(identity.requested_path) != candidate:
        return False
    if Path(identity.resolved_path).is_relative_to(root):
        return True
    return bool(
        identity.symlink_chain
        and Path(identity.symlink_chain[0][0]) == candidate
    )


@dataclass(frozen=True)
class ConfigIdentity:
    root_path: str
    parent_path: str
    parent_mode: int
    parent_size: int
    parent_mtime_ns: int
    parent_ctime_ns: int
    parent_device: int
    parent_inode: int
    config_path: str
    config_file: FileIdentity | None

    @classmethod
    def capture(
        cls,
        root_path: str | Path,
        config_path: str | Path,
    ) -> ConfigIdentity:
        root = Path(root_path)
        candidate = Path(config_path)
        if not root.is_absolute() or not candidate.is_absolute():
            raise ExecutionContractError("config paths must be absolute")
        try:
            canonical_root = root.resolve(strict=True)
        except OSError as exc:
            raise ExecutionContractError("config root is unavailable") from exc
        lexical = candidate.absolute()
        if not lexical.is_relative_to(root.absolute()):
            raise ExecutionContractError("config path escapes its root")
        try:
            resolved_parent = candidate.parent.resolve(strict=True)
            parent_before = resolved_parent.stat()
        except OSError as exc:
            raise ExecutionContractError("config parent is unavailable") from exc
        if (
            not resolved_parent.is_relative_to(canonical_root)
            or not stat.S_ISDIR(parent_before.st_mode)
        ):
            raise ExecutionContractError("config path escapes its root")
        if candidate.exists() or candidate.is_symlink():
            identity = FileIdentity.capture(candidate)
            if not config_file_target_is_admissible(
                canonical_root,
                lexical,
                identity,
            ):
                # Unreachable: capture(candidate) always records
                # requested_path == candidate, and a symlinked candidate is
                # admitted by the chain fallback in config_file_target_is_admissible.
                raise ExecutionContractError("config path escapes its root")  # pragma: no cover
        else:
            identity = None
        try:
            parent_after = resolved_parent.stat()
        except OSError as exc:  # pragma: no cover - TOCTOU window between parent_before/after
            raise ExecutionContractError("config parent is unavailable") from exc
        if stable_stat_identity(parent_before) != stable_stat_identity(
            parent_after,
        ):
            raise ExecutionContractError(
                "config parent changed during identity capture",
            )
        return cls(
            root_path=str(canonical_root),
            parent_path=str(resolved_parent),
            parent_mode=parent_after.st_mode,
            parent_size=parent_after.st_size,
            parent_mtime_ns=parent_after.st_mtime_ns,
            parent_ctime_ns=parent_after.st_ctime_ns,
            parent_device=parent_after.st_dev,
            parent_inode=parent_after.st_ino,
            config_path=str(lexical),
            config_file=identity,
        )

    def attest(self) -> bool:
        return self.attest_with_reason()[0]

    def attest_with_reason(self) -> tuple[bool, str]:
        root = Path(self.root_path)
        candidate = Path(self.config_path)
        try:
            if root.resolve(strict=True) != root:
                return False, f"config_root_resolved_mismatch:{self.root_path}"
            parent = candidate.parent.resolve(strict=True)
            parent_stat = parent.stat()
            if (
                parent != Path(self.parent_path)
                or parent_stat.st_mode != self.parent_mode
                or self._parent_routing_identity(parent_stat)
                != self.parent_routing_identity
            ):
                return False, f"config_parent_mismatch:{self.parent_path}"
            current = ConfigIdentity.capture(root, candidate)
        except ExecutionContractError as exc:
            return False, f"config_capture_failed:{self.config_path}:{exc}"
        except OSError as exc:
            return False, f"config_oserror:{self.config_path}:{exc}"
        if current.config_file != self.config_file:
            return False, f"config_file_mismatch:{self.config_path}"
        return True, "ok"

    @property
    def parent_routing_identity(self) -> tuple[int, int, int]:
        return (
            stat.S_IFMT(self.parent_mode),
            self.parent_device,
            self.parent_inode,
        )

    @staticmethod
    def _parent_routing_identity(value: os.stat_result) -> tuple[int, int, int]:
        return (
            stat.S_IFMT(value.st_mode),
            value.st_dev,
            value.st_ino,
        )

    def attest_metadata(self) -> bool:
        return self.attest_metadata_with_reason()[0]

    def attest_metadata_with_reason(self) -> tuple[bool, str]:
        root = Path(self.root_path)
        candidate = Path(self.config_path)
        try:
            if root.resolve(strict=True) != root:
                return False, f"config_root_resolved_mismatch:{self.root_path}"
            if not candidate.absolute().is_relative_to(root):
                # Unreachable: capture rejects escaping candidates and
                # from_dict enforces config_path.parent == parent_path under root.
                return False, f"config_candidate_escapes_root:{self.config_path}"  # pragma: no cover
            resolved_parent = candidate.parent.resolve(strict=True)
            parent_stat = resolved_parent.stat()
            if (
                not resolved_parent.is_relative_to(root)
                or resolved_parent != Path(self.parent_path)
                or parent_stat.st_mode != self.parent_mode
                or self._parent_routing_identity(parent_stat)
                != self.parent_routing_identity
            ):
                return False, f"config_parent_metadata_mismatch:{self.parent_path}"
        except OSError as exc:
            return False, f"config_metadata_oserror:{self.config_path}:{exc}"
        if self.config_file is None:
            if candidate.exists() or candidate.is_symlink():
                return False, f"config_file_unexpectedly_exists:{self.config_path}"
            return True, "ok"
        return self.config_file.attest_metadata_with_reason()



def config_identity_from_dict(raw: Mapping[str, Any]) -> ConfigIdentity:
    if type(raw) is not dict or set(raw) != {
        "root_path",
        "parent_path",
        "parent_mode",
        "parent_size",
        "parent_mtime_ns",
        "parent_ctime_ns",
        "parent_device",
        "parent_inode",
        "config_path",
        "config_file",
    }:
        raise ExecutionContractError("invalid config identity")
    root_path = required_string(raw, "root_path")
    parent_path = required_string(raw, "parent_path")
    config_path = required_string(raw, "config_path")
    integers = {
        key: required_integer(raw, key)
        for key in (
            "parent_mode",
            "parent_size",
            "parent_mtime_ns",
            "parent_ctime_ns",
            "parent_device",
            "parent_inode",
        )
    }
    if (
        not Path(root_path).is_absolute()
        or not Path(parent_path).is_absolute()
        or not Path(config_path).is_absolute()
        or Path(config_path).parent != Path(parent_path)
        or not Path(parent_path).is_relative_to(Path(root_path))
        or not stat.S_ISDIR(integers["parent_mode"])
        or any(value < 0 for value in integers.values())
    ):
        raise ExecutionContractError("invalid config identity")
    config_file_raw = raw.get("config_file")
    if config_file_raw is not None and type(config_file_raw) is not dict:
        raise ExecutionContractError("invalid config identity")
    identity = ConfigIdentity(
        root_path=root_path,
        parent_path=parent_path,
        config_path=config_path,
        config_file=(
            file_identity_from_dict(config_file_raw)
            if config_file_raw is not None
            else None
        ),
        **integers,
    )
    if (
        identity.config_file is not None
        and not config_file_target_is_admissible(
            identity.root_path,
            identity.config_path,
            identity.config_file,
        )
    ):
        raise ExecutionContractError("invalid config identity")
    return identity
