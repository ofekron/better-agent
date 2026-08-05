from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from codex_execution_common import (
    ExecutionContractError,
    binary_open_flags,
    canonical_json,
    sha256_fd,
)
from codex_execution_identity import FileIdentity, file_identity_to_dict
from provider_family_launch_attestation import CriticalPackageIdentity
from provider_runtime_capability_model import (
    CAPABILITY_MANIFEST_SCHEMA,
    CAPABILITY_PAYLOAD_NAME,
    CAPABILITY_PAYLOAD_SCHEMA,
    MAX_AGENTS,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_PAYLOAD_BYTES,
    MAX_SKILLS,
    MAX_TOTAL_FILE_BYTES,
    PreparedRuntimeCapabilities,
    frozen_json,
    normalize_plan,
    normalize_prewarm_status,
    semantic_fingerprint,
)
from provider_manifest import artifact_family_kinds
from runtime_skill_templates import (
    MACHINE_ID_TEMPLATE_VARIABLE,
    RuntimeSkillSource,
    specialize_skill_text,
)


_FAMILIES = artifact_family_kinds()
_SAFE_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")


def _read_identity(identity: FileIdentity) -> bytes:
    flags = binary_open_flags(
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(identity.resolved_path, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_dev != identity.device
                or before.st_ino != identity.inode
                or before.st_size != identity.size
                or before.st_mtime_ns != identity.mtime_ns
                or before.st_ctime_ns != identity.ctime_ns
                or before.st_size > MAX_FILE_BYTES
                or sha256_fd(descriptor) != identity.sha256
                or not identity.attest_metadata()
            ):
                raise ExecutionContractError(
                    "runtime capability source identity mismatch",
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ExecutionContractError(
            "runtime capability source is unavailable",
        ) from exc
    contents = b"".join(chunks)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or hashlib.sha256(contents).hexdigest() != identity.sha256
    ):
        raise ExecutionContractError(
            "runtime capability source changed during snapshot",
        )
    return contents


def _tree_files(root: Path) -> tuple[tuple[str, int], ...]:
    if not root.is_absolute() or root.is_symlink():
        raise ExecutionContractError("runtime skill root is invalid")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ExecutionContractError("runtime skill root is unavailable") from exc
    if not resolved.is_dir():
        raise ExecutionContractError("runtime skill root is invalid")
    entries: list[tuple[str, int]] = []
    try:
        for candidate in sorted(root.rglob("*")):
            relative = candidate.relative_to(root)
            if (
                candidate.is_symlink()
                or relative.is_absolute()
                or ".." in relative.parts
                or len(relative.parts) > 32
            ):
                raise ExecutionContractError("runtime skill path is invalid")
            observed = candidate.lstat()
            if stat.S_ISDIR(observed.st_mode):
                continue
            if not stat.S_ISREG(observed.st_mode):
                raise ExecutionContractError("runtime skill entry is invalid")
            entries.append((relative.as_posix(), observed.st_mode))
    except OSError as exc:
        # Defensive: pathlib rglob swallows PermissionError and returns empty
        # rather than raising, so this is only hit by a genuine OS error or a
        # lost race between is_dir() and the walk.
        raise ExecutionContractError("runtime skill is unreadable") from exc  # pragma: no cover
    if not any(path == "SKILL.md" for path, _mode in entries):
        raise ExecutionContractError("runtime skill lacks SKILL.md")
    return tuple(entries)


def _entry(
    *,
    kind: str,
    owner: str,
    relative_path: str,
    source: Path,
    source_mode: int,
    template_variables: tuple[str, ...] = (),
    machine_id: str | None = None,
) -> dict[str, Any]:
    if source.is_symlink():
        raise ExecutionContractError("runtime capability source is a symlink")
    identity = FileIdentity.capture(source)
    contents = _read_identity(identity)
    if kind == "skill" and relative_path == "SKILL.md":
        try:
            if MACHINE_ID_TEMPLATE_VARIABLE in template_variables:
                from local_machine_identity import require_matching_local_machine_id

                require_matching_local_machine_id(machine_id)
            contents = specialize_skill_text(
                contents.decode("utf-8"),
                template_variables=template_variables,
                machine_id=machine_id,
            ).encode("utf-8")
        except (RuntimeError, UnicodeDecodeError, ValueError) as exc:
            raise ExecutionContractError(
                "runtime skill specialization is invalid",
            ) from exc
    return {
        "kind": kind,
        "owner": owner,
        "path": relative_path,
        "sha256": hashlib.sha256(contents).hexdigest(),
        "size": len(contents),
        "mode": 0o500 if source_mode & 0o111 else 0o400,
        "source_identity": file_identity_to_dict(identity),
        "contents": base64.b64encode(contents).decode("ascii"),
    }


def _skill_entries(
    sources: Mapping[str, Path | RuntimeSkillSource],
    *,
    machine_id: str | None = None,
) -> list[dict[str, Any]]:
    if type(sources) is not dict or len(sources) > MAX_SKILLS:
        raise ExecutionContractError("invalid runtime skill selection")
    entries: list[dict[str, Any]] = []
    for name in sorted(sources):
        source = sources[name]
        spec = (
            source
            if isinstance(source, RuntimeSkillSource)
            else RuntimeSkillSource(root=Path(source))
        )
        root = Path(spec.root)
        if type(name) is not str or not _SAFE_OWNER_RE.fullmatch(name):
            raise ExecutionContractError("invalid runtime skill name")
        before = _tree_files(root)
        for relative, mode in before:
            entries.append(_entry(
                kind="skill",
                owner=name,
                relative_path=relative,
                source=root / Path(relative),
                source_mode=mode,
                template_variables=spec.template_variables,
                machine_id=machine_id,
            ))
        if _tree_files(root) != before:
            raise ExecutionContractError(
                "runtime skill changed during snapshot",
            )
    return entries


def _agent_entries(
    sources: Mapping[str, Path],
) -> list[dict[str, Any]]:
    if type(sources) is not dict or len(sources) > MAX_AGENTS:
        raise ExecutionContractError("invalid runtime agent selection")
    entries: list[dict[str, Any]] = []
    for name in sorted(sources):
        source = Path(sources[name])
        if (
            type(name) is not str
            or not _SAFE_OWNER_RE.fullmatch(name)
            or Path(name).name != name
        ):
            raise ExecutionContractError("invalid runtime agent name")
        try:
            mode = source.lstat().st_mode
        except OSError as exc:
            raise ExecutionContractError(
                "runtime agent is unavailable",
            ) from exc
        entries.append(_entry(
            kind="agent",
            owner=name,
            relative_path=name,
            source=source,
            source_mode=mode,
        ))
    return entries


def _package_payload(
    identities: tuple[CriticalPackageIdentity, ...],
) -> list[dict[str, Any]]:
    names: set[str] = set()
    payload: list[dict[str, Any]] = []
    for identity in identities:
        if (
            not isinstance(identity, CriticalPackageIdentity)
            or identity.package_name in names
            or not identity.attest()
        ):
            raise ExecutionContractError(
                "critical runtime package identity mismatch",
            )
        names.add(identity.package_name)
        payload.append(identity.to_dict())
    return sorted(payload, key=lambda value: value["package_name"])


def snapshot_family_runtime_capabilities(
    *,
    family: str,
    skill_sources: Mapping[str, Path | RuntimeSkillSource],
    agent_sources: Mapping[str, Path],
    resolved_plan: Mapping[str, Any],
    extension_state: Mapping[str, Any],
    installation_decisions: Mapping[str, Any],
    package_identities: tuple[CriticalPackageIdentity, ...] = (),
    prewarm_results: Mapping[str, Any] | None = None,
    machine_id: str | None = None,
) -> PreparedRuntimeCapabilities:
    if family not in _FAMILIES:
        raise ExecutionContractError("unsupported runtime capability family")
    if family != "claude" and agent_sources:
        raise ExecutionContractError("AGY has no runtime agent file surface")
    plan = normalize_plan(resolved_plan)
    extensions = json.loads(
        frozen_json(extension_state, label="extension capability state"),
    )
    installation = json.loads(
        frozen_json(
            installation_decisions,
            label="installation profile decisions",
        ),
    )
    if type(extensions) is not dict or type(installation) is not dict:
        raise ExecutionContractError("invalid runtime capability state")
    prewarm = normalize_prewarm_status(plan, prewarm_results or {})
    packages = _package_payload(package_identities)
    files = _skill_entries(
        skill_sources,
        machine_id=machine_id,
    ) + _agent_entries(agent_sources)
    if (
        len(files) > MAX_FILES
        or sum(entry["size"] for entry in files) > MAX_TOTAL_FILE_BYTES
    ):
        raise ExecutionContractError("runtime capability files are too large")
    payload_value = {
        "schema": CAPABILITY_PAYLOAD_SCHEMA,
        "family": family,
        "plan": plan,
        "extension_state": extensions,
        "installation_decisions": installation,
        "package_identities": packages,
        "prewarm_status": prewarm,
        "files": files,
    }
    payload = canonical_json(payload_value)
    if not payload or len(payload) > MAX_PAYLOAD_BYTES:
        raise ExecutionContractError("runtime capability payload is too large")
    package_fingerprint = hashlib.sha256(canonical_json(packages)).hexdigest()
    manifest = {
        "schema": CAPABILITY_MANIFEST_SCHEMA,
        "family": family,
        "path": CAPABILITY_PAYLOAD_NAME,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "file_count": len(files),
        "skill_count": len(skill_sources),
        "agent_count": len(agent_sources),
        "extension_ids": sorted(extensions),
        "tool_names": list(plan["tools"]),
        "semantic_fingerprint": semantic_fingerprint(plan),
        "package_fingerprint": package_fingerprint,
        "prewarm_status": prewarm,
    }
    return PreparedRuntimeCapabilities.create(
        manifest=manifest,
        payload=payload,
        plan=plan,
        prewarm_status=prewarm,
    )
