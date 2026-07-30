from __future__ import annotations

import errno
import os
import stat
import uuid
from pathlib import Path

from codex_execution_common import ExecutionContractError, SHA256_RE
from codex_execution_identity import FileIdentity
from paths import (
    make_private_file,
    require_private_directory,
    require_private_file,
)
from provider_pinned_launch import sdk_launch_cache_root
from run_execution_payload_manifest import (
    ExecutionPayloadManifest,
    MANIFEST_NAME,
    PAYLOAD_ROOTS,
    SAFE_NAME,
    PayloadFile,
    canonical_bytes,
    parse_manifest,
    payload_file_mode,
)
from runs_dir import runs_root


_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_FILE_SHARE_DELETE = 0x4
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


def _manifest_mode() -> int:
    return 0o444 if os.name == "nt" else 0o400


def _is_windows() -> bool:
    return os.name == "nt"


def _fail(message: str, cause: BaseException | None = None) -> None:
    if cause is None:
        raise ExecutionContractError(message)
    raise ExecutionContractError(message) from cause


def _private_directory(path: Path, label: str) -> os.stat_result:
    try:
        observed = path.lstat()
    except OSError as exc:
        _fail(f"{label} is unavailable", exc)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or _is_windows_reparse_point(observed)
    ):
        _fail(f"{label} is not a private directory")
    try:
        require_private_directory(path)
    except PermissionError as exc:
        _fail(f"{label} is not a private directory", exc)
    return observed


def _is_windows_reparse_point(identity: os.stat_result) -> bool:
    attributes = int(getattr(identity, "st_file_attributes", 0) or 0)
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & marker)


def _validate_run_directory(run_dir: Path) -> os.stat_result:
    root = runs_root()
    if (
        not run_dir.is_absolute()
        or run_dir.name in {"", ".", ".."}
        or not SAFE_NAME.fullmatch(run_dir.name)
    ):
        _fail("run directory is invalid")
    try:
        root_resolved = root.resolve(strict=True)
        parent_resolved = run_dir.parent.resolve(strict=True)
    except OSError as exc:
        _fail("runs root is unavailable", exc)
    if parent_resolved != root_resolved:
        _fail("run directory escapes the runs root")
    return _private_directory(run_dir, "run directory")


def _sdk_cache_payload_root(
    identities: tuple[FileIdentity, ...],
) -> tuple[Path, str]:
    """The single closed root every payload file must live in: the shared,
    fingerprint-keyed SDK launch cache entry the CLI was materialized into
    (`sdk_launch_cache_root()/<sha256 fingerprint>/root`). The root is
    reconstructed from the trusted cache prefix plus the regex-validated
    fingerprint segment — never taken as a free-form path — so a manifest
    can only ever bind to a directory inside the private state-home cache.
    """
    requested_roots = {
        Path(identity.requested_path).parent for identity in identities
    }
    resolved_roots = {
        Path(identity.resolved_path).parent for identity in identities
    }
    if len(requested_roots) != 1 or len(resolved_roots) != 1:
        _fail("payload files do not share a single payload root")
    payload_root = requested_roots.pop()
    resolved_root = resolved_roots.pop()
    fingerprint = resolved_root.parent.name
    try:
        cache_root = sdk_launch_cache_root().resolve(strict=True)
        payload_root_resolved = payload_root.resolve(strict=True)
    except OSError as exc:
        _fail("payload root is unavailable", exc)
    if (
        payload_root.name != "root"
        or resolved_root.name != "root"
        or payload_root_resolved != resolved_root
        or payload_root.parent.name != fingerprint
        or not SHA256_RE.fullmatch(fingerprint)
        or resolved_root.parent.parent != cache_root
    ):
        _fail("payload root escapes the SDK launch cache")
    return payload_root, fingerprint


def _payload_file(
    payload_root: Path,
    identity: FileIdentity,
    *,
    hash_payload: bool,
) -> PayloadFile:
    requested = Path(identity.requested_path)
    resolved = Path(identity.resolved_path)
    try:
        resolved_root = payload_root.resolve(strict=True)
    except OSError as exc:
        _fail("payload root is unavailable", exc)
    if (
        requested.parent not in {payload_root, resolved_root}
        or resolved.parent != resolved_root
        or identity.symlink_chain
        or not SAFE_NAME.fullmatch(requested.name)
    ):
        _fail("payload file escapes its closed root")
    try:
        observed = requested.lstat()
    except OSError as exc:
        _fail("payload file is unavailable", exc)
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or _is_windows_reparse_point(observed)
        or observed.st_nlink != 1
        or stat.S_IMODE(observed.st_mode) != payload_file_mode()
        or not (
            identity.attest()
            if hash_payload
            else identity.attest_metadata()
        )
    ):
        _fail("payload file identity is invalid")
    try:
        require_private_file(requested)
    except PermissionError as exc:
        _fail("payload file is not private", exc)
    return PayloadFile(
        name=requested.name,
        kind="regular_file",
        mode=payload_file_mode(),
        size=identity.size,
        mtime_ns=identity.mtime_ns,
        ctime_ns=identity.ctime_ns,
        device=identity.device,
        inode=identity.inode,
        sha256=identity.sha256,
    )


def _build_manifest(
    run_dir: Path,
    payload_root_name: str,
    identities: tuple[FileIdentity, ...],
    *,
    hash_payload: bool = True,
) -> ExecutionPayloadManifest:
    run_stat = _validate_run_directory(run_dir)
    if payload_root_name not in PAYLOAD_ROOTS:
        _fail("payload root is invalid")
    if not identities:
        _fail("payload manifest must contain files")
    if any(type(identity) is not FileIdentity for identity in identities):
        _fail("payload identity is invalid")
    payload_root, fingerprint = _sdk_cache_payload_root(identities)
    _private_directory(payload_root, "payload root")
    files = tuple(
        _payload_file(
            payload_root,
            identity,
            hash_payload=hash_payload,
        )
        for identity in identities
    )
    if len({item.name for item in files}) != len(files):
        _fail("payload manifest contains duplicate files")
    return ExecutionPayloadManifest(
        run_id=run_dir.name,
        run_directory_device=run_stat.st_dev,
        run_directory_inode=run_stat.st_ino,
        payload_root=payload_root_name,
        payload_root_fingerprint=fingerprint,
        files=tuple(sorted(files, key=lambda item: item.name)),
    )


def _read_manifest_file(path: Path) -> ExecutionPayloadManifest:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        path_stat = path.lstat()
        descriptor = os.open(path, flags)
    except OSError as exc:
        _fail("payload manifest is unavailable", exc)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or _is_windows_reparse_point(path_stat)
            or _is_windows_reparse_point(before)
            or (path_stat.st_dev, path_stat.st_ino)
            != (before.st_dev, before.st_ino)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != _manifest_mode()
        ):
            _fail("payload manifest file is invalid")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        final_path_stat = path.lstat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or (
            stat.S_ISLNK(final_path_stat.st_mode)
            or not stat.S_ISREG(final_path_stat.st_mode)
            or _is_windows_reparse_point(final_path_stat)
            or final_path_stat.st_nlink != 1
            or stat.S_IMODE(final_path_stat.st_mode) != _manifest_mode()
            or (final_path_stat.st_dev, final_path_stat.st_ino)
            != (after.st_dev, after.st_ino)
        ):
            _fail("payload manifest changed during read")
    finally:
        os.close(descriptor)
    try:
        require_private_file(path)
    except PermissionError as exc:
        _fail("payload manifest file is not private", exc)
    encoded = b"".join(chunks)
    return parse_manifest(encoded)


def _attest_manifest_binding(
    run_dir: Path,
    manifest: ExecutionPayloadManifest,
) -> Path:
    run_stat = _validate_run_directory(run_dir)
    if (
        manifest.run_id != run_dir.name
        or manifest.run_directory_device != run_stat.st_dev
        or manifest.run_directory_inode != run_stat.st_ino
    ):
        _fail("payload manifest does not own this run directory")
    payload_root = (
        sdk_launch_cache_root()
        / manifest.payload_root_fingerprint
        / "root"
    )
    _private_directory(payload_root, "payload root")
    return payload_root


def _attest_manifest(run_dir: Path, manifest: ExecutionPayloadManifest) -> None:
    payload_root = _attest_manifest_binding(run_dir, manifest)
    for item in manifest.files:
        path = payload_root / item.name
        try:
            before = path.lstat()
            identity = FileIdentity.capture(path)
            after = path.lstat()
        except OSError as exc:
            _fail("payload file is unavailable", exc)
        except ExecutionContractError:
            _fail("payload file identity is unavailable")
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or _is_windows_reparse_point(before)
            or not stat.S_ISREG(after.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or _is_windows_reparse_point(after)
            or after.st_nlink != 1
            or stat.S_IMODE(after.st_mode) != item.mode
            or (after.st_dev, after.st_ino)
            != (identity.device, identity.inode)
            or identity.size != item.size
            or identity.mtime_ns != item.mtime_ns
            or identity.ctime_ns != item.ctime_ns
            or identity.device != item.device
            or identity.inode != item.inode
            or identity.sha256 != item.sha256
        ):
            _fail("payload file no longer matches its manifest")


def _flush_windows_directory(path: Path) -> None:
    import ctypes
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
    flush = kernel32.FlushFileBuffers
    flush.argtypes = (wintypes.HANDLE,)
    flush.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        raise OSError(ctypes.get_last_error(), "directory handle open failed")
    flushed = bool(flush(handle))
    flush_error = ctypes.get_last_error()
    closed = bool(close(handle))
    close_error = ctypes.get_last_error()
    if not closed:
        raise OSError(close_error, "directory handle close failed")
    if not flushed:
        raise OSError(flush_error, "directory metadata flush failed")


def _fsync_directory(path: Path) -> None:
    if _is_windows():
        _flush_windows_directory(path)
        return
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _seal_windows_manifest(path: Path) -> None:
    before = path.lstat()
    if _is_windows_reparse_point(before):
        _fail("payload manifest became a reparse point")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            _fail("payload manifest changed before sealing")
        os.chmod(path, stat.S_IREAD)
        os.fsync(descriptor)
        after = path.lstat()
        if (
            _is_windows_reparse_point(after)
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or stat.S_IMODE(after.st_mode) != _manifest_mode()
        ):
            _fail("payload manifest could not be sealed")
    finally:
        os.close(descriptor)


def read_execution_payload_manifest(
    run_dir: str | Path,
) -> ExecutionPayloadManifest:
    path = Path(run_dir)
    manifest = _read_manifest_file(path / MANIFEST_NAME)
    _attest_manifest(path, manifest)
    return manifest


def publish_execution_payload_manifest(
    run_dir: str | Path,
    payload_root: str,
    identities: tuple[FileIdentity, ...],
) -> ExecutionPayloadManifest:
    path = Path(run_dir)
    final = path / MANIFEST_NAME
    try:
        final.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _fail("payload manifest is unavailable", exc)
    else:
        existing = read_execution_payload_manifest(path)
        desired = _build_manifest(
            path,
            payload_root,
            identities,
            hash_payload=False,
        )
        if existing != desired:
            _fail("payload manifest already exists with different data")
        return existing
    manifest = _build_manifest(path, payload_root, identities)
    payload = canonical_bytes(manifest)
    temp = path / f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(temp, flags, 0o600)
        created = True
        try:
            make_private_file(temp)
            if os.name != "nt":
                os.fchmod(descriptor, 0o400)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    _fail("payload manifest write was incomplete")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temp, final, follow_symlinks=False)
        except FileExistsError:
            existing = read_execution_payload_manifest(path)
            if existing != manifest:
                _fail("payload manifest already exists with different data")
            return existing
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                existing = read_execution_payload_manifest(path)
                if existing != manifest:
                    _fail("payload manifest already exists with different data")
                return existing
            _fail("payload manifest cannot be published", exc)
        _fsync_directory(path)
        os.unlink(temp)
        created = False
        if os.name == "nt":
            _seal_windows_manifest(final)
        _fsync_directory(path)
    except ExecutionContractError:
        raise
    except OSError as exc:
        _fail("payload manifest cannot be published", exc)
    finally:
        if created:
            try:
                temp.unlink()
            except OSError:
                pass
    published = _read_manifest_file(final)
    _attest_manifest_binding(path, published)
    if published != manifest:
        _fail("published payload manifest does not match")
    return published


__all__ = [
    "ExecutionPayloadManifest",
    "MANIFEST_NAME",
    "PayloadFile",
    "publish_execution_payload_manifest",
    "read_execution_payload_manifest",
]
