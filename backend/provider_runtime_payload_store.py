from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Mapping

from codex_execution_common import ExecutionContractError
from provider_runtime_capability_model import (
    CAPABILITY_PAYLOAD_NAME,
    PreparedRuntimeCapabilities,
)
from provider_runtime_payload_codec import (
    decode_runtime_capability_payload,
    validate_runtime_capability_manifest,
)
from paths import (
    make_private_directory,
    make_private_file,
    require_private_directory,
    require_private_file,
)


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _prepared_root() -> Path:
    from paths import ba_home

    return ba_home() / "prepared-execution-payloads"


def _stage_dir(run_id: str) -> Path:
    if type(run_id) is not str or not _RUN_ID_RE.fullmatch(run_id):
        raise ExecutionContractError("invalid runtime capability run id")
    key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return _prepared_root() / key


def _uses_windows_acl() -> bool:
    return os.name == "nt"


def _secure_directory(path: Path, *, create: bool) -> Path:
    try:
        if create:
            path.mkdir(parents=True, mode=0o700, exist_ok=False)
        observed = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ExecutionContractError(
            "runtime capability directory is unavailable",
        ) from exc
    if path.is_symlink() or not stat.S_ISDIR(observed.st_mode):
        raise ExecutionContractError("runtime capability directory is unsafe")
    try:
        if create:
            make_private_directory(path)
        require_private_directory(path)
    except PermissionError as exc:
        raise ExecutionContractError(
            "runtime capability directory is unsafe",
        ) from exc
    return resolved


def _ensure_secure_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        return _secure_directory(path, create=False)
    except OSError as exc:
        raise ExecutionContractError(
            "runtime capability directory is unavailable",
        ) from exc
    try:
        make_private_directory(path)
        return _secure_directory(path, create=False)
    except BaseException:
        try:
            path.rmdir()
        except OSError:
            pass
        raise


def _read_private_payload(
    path: Path,
    manifest: Mapping[str, Any],
) -> bytes:
    expected = validate_runtime_capability_manifest(manifest)
    try:
        require_private_file(path)
    except PermissionError as exc:
        raise ExecutionContractError(
            "runtime capability payload is unsafe",
        ) from exc
    if path.is_symlink():
        raise ExecutionContractError("runtime capability payload is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_size != expected["size"]
            ):
                raise ExecutionContractError(
                    "runtime capability payload is unsafe",
                )
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ExecutionContractError(
            "runtime capability payload is unavailable",
        ) from exc
    payload = b"".join(chunks)
    decode_runtime_capability_payload(payload, expected)
    return payload


def _write_private_payload(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600)
        if not _uses_windows_acl():
            os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short runtime capability payload write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if _uses_windows_acl():
            try:
                make_private_file(temporary)
            except PermissionError as exc:
                raise ExecutionContractError(
                    "runtime capability payload is unsafe",
                ) from exc
        if path.exists() or path.is_symlink():
            raise ExecutionContractError(
                "runtime capability payload already exists",
            )
        os.replace(temporary, path)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _stage_payload(
    run_id: str,
    *,
    manifest: Mapping[str, Any],
    payload: bytes,
) -> None:
    manifest = validate_runtime_capability_manifest(manifest)
    decode_runtime_capability_payload(payload, manifest)
    root = _ensure_secure_directory(_prepared_root())
    directory = _secure_directory(_stage_dir(run_id), create=True)
    try:
        _write_private_payload(
            directory / CAPABILITY_PAYLOAD_NAME,
            payload,
        )
    except BaseException:
        try:
            directory.rmdir()
        except OSError:
            pass
        raise


def stage_family_runtime_capabilities(
    run_id: str,
    prepared: PreparedRuntimeCapabilities,
) -> None:
    if not isinstance(prepared, PreparedRuntimeCapabilities):
        raise ExecutionContractError("invalid prepared runtime capabilities")
    _stage_payload(
        run_id,
        manifest=prepared.manifest,
        payload=prepared.payload,
    )


def cleanup_staged_family_runtime_capabilities(run_id: str) -> None:
    root = _prepared_root()
    try:
        _secure_directory(root, create=False)
    except ExecutionContractError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return
        raise
    directory = _stage_dir(run_id)
    try:
        _secure_directory(directory, create=False)
    except ExecutionContractError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return
        raise
    payload = directory / CAPABILITY_PAYLOAD_NAME
    try:
        require_private_file(payload)
    except FileNotFoundError:
        pass
    else:
        payload.unlink()
    try:
        directory.rmdir()
    except FileNotFoundError:
        pass


def _validated_run_dir(run_dir: Path) -> Path:
    from paths import ba_home

    if not run_dir.is_absolute() or run_dir.is_symlink():
        raise ExecutionContractError("runtime capability run directory is invalid")
    try:
        resolved = run_dir.resolve(strict=True)
        state_root = ba_home().resolve(strict=True)
        observed = resolved.stat()
    except OSError as exc:
        raise ExecutionContractError(
            "runtime capability run directory is unavailable",
        ) from exc
    if not resolved.is_relative_to(state_root) or not stat.S_ISDIR(observed.st_mode):
        raise ExecutionContractError("runtime capability run directory escapes state")
    try:
        require_private_directory(run_dir)
    except PermissionError as exc:
        raise ExecutionContractError(
            "runtime capability run directory is unsafe",
        ) from exc
    return resolved


def install_staged_family_runtime_capabilities(
    run_dir: Path,
    *,
    run_id: str,
    manifest: Mapping[str, Any],
) -> None:
    target_dir = _validated_run_dir(run_dir)
    source_dir = _secure_directory(_stage_dir(run_id), create=False)
    source = source_dir / CAPABILITY_PAYLOAD_NAME
    payload = _read_private_payload(source, manifest)
    target = target_dir / CAPABILITY_PAYLOAD_NAME
    if target.exists() or target.is_symlink():
        raise ExecutionContractError(
            "runtime capability payload target already exists",
        )
    try:
        os.link(source, target, follow_symlinks=False)
    except OSError as exc:
        raise ExecutionContractError(
            "runtime capability payload cannot be installed atomically",
        ) from exc
    try:
        _read_private_payload(target, manifest)
        if target.read_bytes() != payload:
            raise ExecutionContractError(
                "installed runtime capability payload mismatch",
            )
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    source.unlink()
    source_dir.rmdir()


def clone_family_runtime_capabilities(
    source_run_dir: Path,
    *,
    target_run_id: str,
    manifest: Mapping[str, Any],
) -> None:
    source = _validated_run_dir(source_run_dir) / CAPABILITY_PAYLOAD_NAME
    payload = _read_private_payload(source, manifest)

    _stage_payload(
        target_run_id,
        manifest=manifest,
        payload=payload,
    )
