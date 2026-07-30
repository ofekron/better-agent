from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

from codex_execution_common import ExecutionContractError, binary_open_flags
from provider_runtime_capability_model import (
    CAPABILITY_FILES_DIR,
    CAPABILITY_PAYLOAD_NAME,
    RunLocalCapabilities,
)
from provider_runtime_payload_codec import decode_runtime_capability_payload
from provider_runtime_payload_store import (
    _read_private_payload,
    _validated_run_dir,
)
from paths import (
    make_private_directory,
    make_private_file,
    require_private_directory,
    require_private_file,
)


def _ensure_private_parent_tree(root: Path, parent: Path) -> None:
    current = root
    for part in parent.relative_to(root).parts:
        current /= part
        try:
            current.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            require_private_directory(current)
            continue
        make_private_directory(current)
        require_private_directory(current)


def _write_resolved_file(
    root: Path,
    path: Path,
    contents: bytes,
    mode: int,
) -> None:
    _ensure_private_parent_tree(root, path.parent)
    flags = binary_open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    creation_mode = 0o600 if os.name == "nt" else mode
    descriptor = os.open(path, flags, creation_mode)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, mode)
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short runtime capability file write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name == "nt":
        make_private_file(path)


def _resolved_path(root: Path, metadata: Mapping[str, Any]) -> Path:
    if metadata["kind"] == "agent":
        return root / "agents" / metadata["owner"]
    return root / "skills" / metadata["owner"] / Path(metadata["path"])


def _verify_resolved(
    root: Path,
    files: tuple[tuple[dict[str, Any], bytes], ...],
) -> None:
    try:
        require_private_directory(root)
    except PermissionError as exc:
        raise ExecutionContractError(
            "run-local capability tree is invalid",
        ) from exc
    if root.is_symlink() or not root.is_dir():
        raise ExecutionContractError("run-local capability tree is invalid")
    resolved_root = root.resolve(strict=True)
    for metadata, contents in files:
        candidate = _resolved_path(root, metadata)
        try:
            require_private_file(candidate)
        except PermissionError as exc:
            raise ExecutionContractError(
                "run-local capability file is unsafe",
            ) from exc
        if candidate.is_symlink():
            raise ExecutionContractError("run-local capability file is unsafe")
        try:
            resolved_candidate = candidate.resolve(strict=True)
            observed = resolved_candidate.stat()
            for parent in candidate.parents:
                if parent == root:
                    break
                require_private_directory(parent)
        except OSError as exc:
            raise ExecutionContractError(
                "run-local capability file is unavailable",
            ) from exc
        except PermissionError as exc:
            raise ExecutionContractError(
                "run-local capability directory is unsafe",
            ) from exc
        if (
            not resolved_candidate.is_relative_to(resolved_root)
            or not stat.S_ISREG(observed.st_mode)
            or (
                os.name != "nt"
                and stat.S_IMODE(observed.st_mode) != metadata["mode"]
            )
            or candidate.read_bytes() != contents
        ):
            raise ExecutionContractError(
                "run-local capability file mismatch",
            )


def resolve_run_local_capabilities(
    run_dir: Path,
    manifest: Mapping[str, Any],
) -> RunLocalCapabilities:
    root = _validated_run_dir(run_dir)
    payload = _read_private_payload(
        root / CAPABILITY_PAYLOAD_NAME,
        manifest,
    )
    decoded, files = decode_runtime_capability_payload(payload, manifest)
    target = root / CAPABILITY_FILES_DIR
    if target.exists() or target.is_symlink():
        _verify_resolved(target, files)
    else:
        temporary = Path(tempfile.mkdtemp(
            prefix=f".{CAPABILITY_FILES_DIR}.",
            dir=root,
        ))
        try:
            os.chmod(temporary, 0o700)
            make_private_directory(temporary)
            for metadata, contents in files:
                _write_resolved_file(
                    temporary,
                    _resolved_path(temporary, metadata),
                    contents,
                    metadata["mode"],
                )
            _verify_resolved(temporary, files)
            os.rename(temporary, target)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    skill_dirs = {
        metadata["owner"]: target / "skills" / metadata["owner"]
        for metadata, _contents in files
        if metadata["kind"] == "skill"
    }
    agent_files = {
        metadata["owner"]: target / "agents" / metadata["owner"]
        for metadata, _contents in files
        if metadata["kind"] == "agent"
    }
    return RunLocalCapabilities(
        plan=decoded["plan"],
        extension_state=decoded["extension_state"],
        installation_decisions=decoded["installation_decisions"],
        skill_dirs=skill_dirs,
        agent_files=agent_files,
        prewarm_status=decoded["prewarm_status"],
    )


def cleanup_installed_family_runtime_capabilities(run_dir: Path) -> None:
    root = _validated_run_dir(run_dir)
    payload = root / CAPABILITY_PAYLOAD_NAME
    files = root / CAPABILITY_FILES_DIR
    if files.is_symlink():
        raise ExecutionContractError("run-local capability tree is unsafe")
    if files.exists():
        shutil.rmtree(files)
    try:
        payload.unlink()
    except FileNotFoundError:
        pass
