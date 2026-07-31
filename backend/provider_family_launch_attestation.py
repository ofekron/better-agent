from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

logger = logging.getLogger(__name__)



from codex_execution_common import (
    AttestOnceCache,
    ExecutionContractError,
    SHA256_RE,
    canonical_json,
    parallel_map,
    required_integer,
    required_string,
    timed_contract_step,
)
from codex_execution_identity import (
    ConfigIdentity,
    FileIdentity,
    config_file_target_is_admissible,
    config_identity_from_dict,
    file_identity_from_dict,
    file_identity_to_dict,
)
from provider_launch_identity import (
    AttestedLaunch,
    DirectoryIdentity,
    DirectoryScopeIdentity,
    _require_object,
    capture_cli_launch,
)
from provider_pinned_launch import (
    MaterializedSdkLaunch,
    PinnedLaunch,
    materialize_sdk_launch,
    materialize_sdk_launch_cached,
    open_pinned_launch,
    open_pinned_runner_launch,
)
from provider_runner_launch import RunnerLaunch, capture_runner_launch
from provider_manifest import artifact_family_kinds


ATTESTATION_SCHEMA = 2
_FAMILIES = artifact_family_kinds()
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _config_to_dict(identity: ConfigIdentity) -> dict[str, Any]:
    return {
        "root_path": identity.root_path,
        "parent_path": identity.parent_path,
        "parent_mode": identity.parent_mode,
        "parent_size": identity.parent_size,
        "parent_mtime_ns": identity.parent_mtime_ns,
        "parent_ctime_ns": identity.parent_ctime_ns,
        "parent_device": identity.parent_device,
        "parent_inode": identity.parent_inode,
        "config_path": identity.config_path,
        "config_file": (
            file_identity_to_dict(identity.config_file)
            if identity.config_file is not None
            else None
        ),
    }


@dataclass(frozen=True)
class ConfigScopeIdentity:
    root: DirectoryScopeIdentity
    files: tuple[ConfigIdentity, ...]
    resume: FileIdentity | None

    def attest(self) -> bool:
        return self.attest_with_reason()[0]

    def attest_with_reason(self) -> tuple[bool, str]:
        ok, reason = self.root.attest_with_reason()
        if not ok:
            return False, f"config_root:{reason}"
        for identity in self.files:
            ok, reason = identity.attest_with_reason()
            if not ok:
                return False, f"config_file:{reason}"
        if self.resume is not None:
            ok, reason = self.resume.attest_with_reason()
            if not ok:
                return False, f"config_resume:{reason}"
        return True, "ok"

    def attest_metadata(self) -> bool:
        return self.attest_metadata_with_reason()[0]

    def attest_metadata_with_reason(self) -> tuple[bool, str]:
        ok, reason = self.root.attest_with_reason()
        if not ok:
            return False, f"config_root_metadata:{reason}"
        for identity in self.files:
            ok, reason = identity.attest_metadata_with_reason()
            if not ok:
                return False, f"config_file_metadata:{reason}"
        if self.resume is not None:
            ok, reason = self.resume.attest_metadata_with_reason()
            if not ok:
                return False, f"config_resume_metadata:{reason}"
        return True, "ok"


    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root.to_dict(),
            "files": [_config_to_dict(identity) for identity in self.files],
            "resume": (
                file_identity_to_dict(self.resume)
                if self.resume is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ConfigScopeIdentity:
        _require_object(raw, {"root", "files", "resume"}, "config scope")
        files = raw["files"]
        resume = raw["resume"]
        if (
            type(raw["root"]) is not dict
            or type(files) is not list
            or any(type(value) is not dict for value in files)
            or (resume is not None and type(resume) is not dict)
        ):
            raise ExecutionContractError("invalid config scope")
        value = cls(
            root=DirectoryScopeIdentity.from_dict(raw["root"]),
            files=tuple(config_identity_from_dict(value) for value in files),
            resume=(
                file_identity_from_dict(resume)
                if resume is not None
                else None
            ),
        )
        root = Path(value.root.resolved_path)
        if (
            any(Path(item.root_path) != root for item in value.files)
            or any(
                item.config_file is not None
                and not config_file_target_is_admissible(
                    root,
                    item.config_path,
                    item.config_file,
                )
                for item in value.files
            )
            or (
                value.resume is not None
                and not Path(value.resume.resolved_path).is_relative_to(root)
            )
        ):
            raise ExecutionContractError("config scope identity escapes root")
        return value


def capture_config_scope(
    *,
    root_path: str | Path,
    config_paths: tuple[str | Path, ...] = (),
    resume_path: str | Path | None = None,
) -> ConfigScopeIdentity:
    root = DirectoryScopeIdentity.capture(root_path)
    requested_root = Path(root.requested_path)
    resolved_root = Path(root.resolved_path)
    resolved_config_paths: list[Path] = []
    for raw_candidate in config_paths:
        candidate = Path(raw_candidate)
        if (
            not candidate.is_absolute()
            or not candidate.absolute().is_relative_to(requested_root)
        ):
            raise ExecutionContractError("config path escapes config root")
        resolved_config_paths.append(
            resolved_root / candidate.absolute().relative_to(requested_root),
        )
    identities = tuple(
        ConfigIdentity.capture(resolved_root, path)
        for path in resolved_config_paths
    )
    resume: FileIdentity | None = None
    if resume_path is not None:
        candidate = Path(resume_path)
        if not candidate.is_absolute():
            raise ExecutionContractError("resume path must be absolute")
        if not candidate.absolute().is_relative_to(requested_root):
            raise ExecutionContractError("resume path escapes config root")
        if not candidate.exists() and not candidate.is_symlink():
            raise ExecutionContractError("requested resume store is unavailable")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ExecutionContractError(
                "requested resume store is unavailable",
            ) from exc
        if not resolved.is_relative_to(resolved_root):
            raise ExecutionContractError("resume path escapes config root")
        resume = FileIdentity.capture(candidate)
    return ConfigScopeIdentity(root=root, files=identities, resume=resume)


@dataclass(frozen=True)
class CriticalPackageIdentity:
    package_name: str
    root: DirectoryIdentity
    files: tuple[FileIdentity, ...]

    def attest(self) -> bool:
        return self.attest_with_reason()[0]

    def attest_with_reason(self) -> tuple[bool, str]:
        ok, reason = self.root.attest_with_reason()
        if not ok:
            return False, f"package_root:{reason}"
        for identity in self.files:
            ok, reason = identity.attest_with_reason()
            if not ok:
                return False, f"package_file:{reason}"
        return True, "ok"

    def attest_metadata(self) -> bool:
        return self.attest_metadata_with_reason()[0]

    def attest_metadata_with_reason(self) -> tuple[bool, str]:
        ok, reason = self.root.attest_with_reason()
        if not ok:
            return False, f"package_root_metadata:{reason}"
        for identity in self.files:
            ok, reason = identity.attest_metadata_with_reason()
            if not ok:
                return False, f"package_file_metadata:{reason}"
        return True, "ok"


    def to_dict(self) -> dict[str, Any]:
        return {
            "package_name": self.package_name,
            "root": self.root.to_dict(),
            "files": [
                file_identity_to_dict(identity) for identity in self.files
            ],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CriticalPackageIdentity:
        _require_object(raw, {"package_name", "root", "files"}, "package identity")
        files = raw["files"]
        if (
            type(raw["root"]) is not dict
            or type(files) is not list
            or not files
            or any(type(value) is not dict for value in files)
        ):
            raise ExecutionContractError("invalid package identity")
        value = cls(
            package_name=required_string(raw, "package_name"),
            root=DirectoryIdentity.from_dict(raw["root"]),
            files=tuple(file_identity_from_dict(value) for value in files),
        )
        root = Path(value.root.resolved_path)
        if (
            not _PACKAGE_NAME_RE.fullmatch(value.package_name)
            or any(
                not Path(identity.resolved_path).is_relative_to(root)
                for identity in value.files
            )
        ):
            raise ExecutionContractError("package identity escapes root")
        return value


def capture_critical_package(
    *,
    package_name: str,
    package_root: str | Path,
    relative_paths: tuple[str | Path, ...],
) -> CriticalPackageIdentity:
    if not _PACKAGE_NAME_RE.fullmatch(package_name) or not relative_paths:
        raise ExecutionContractError("invalid critical package")
    root = DirectoryIdentity.capture(package_root)
    resolved_root = Path(root.resolved_path)
    files: list[FileIdentity] = []
    for relative in relative_paths:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ExecutionContractError("package source escapes package root")
        identity = FileIdentity.capture(resolved_root / relative_path)
        if not Path(identity.resolved_path).is_relative_to(resolved_root):
            raise ExecutionContractError("package source escapes package root")
        files.append(identity)
    return CriticalPackageIdentity(
        package_name=package_name,
        root=root,
        files=tuple(files),
    )


@dataclass(frozen=True)
class FamilyLaunchAttestation:
    family: str
    runner: RunnerLaunch
    downstream: AttestedLaunch
    config: ConfigScopeIdentity
    critical_packages: tuple[CriticalPackageIdentity, ...]
    fingerprint: str
    schema: int = ATTESTATION_SCHEMA
    _attest_cache: AttestOnceCache = field(
        default_factory=AttestOnceCache,
        compare=False,
        hash=False,
        repr=False,
    )

    @classmethod
    def capture(
        cls,
        *,
        family: str,
        runner: RunnerLaunch,
        downstream: AttestedLaunch,
        config: ConfigScopeIdentity,
        critical_packages: tuple[CriticalPackageIdentity, ...] = (),
    ) -> FamilyLaunchAttestation:
        if (
            family not in _FAMILIES
            or runner.runner_kind != family
            or downstream.logical_command != family
        ):
            raise ExecutionContractError("provider launch family mismatch")
        unsigned = cls(
            family=family,
            runner=runner,
            downstream=downstream,
            config=config,
            critical_packages=critical_packages,
            fingerprint="",
        )
        return cls(
            family=family,
            runner=runner,
            downstream=downstream,
            config=config,
            critical_packages=critical_packages,
            fingerprint=unsigned._computed_fingerprint(),
        )

    def attest(self) -> bool:
        ok, _ = self.attest_with_reason()
        return ok

    def attest_with_reason(self) -> tuple[bool, str]:
        with timed_contract_step("provider.family_attestation.attest"):
            if self.fingerprint != self._computed_fingerprint():
                return False, "fingerprint_mismatch"

            res = self._attest_cache.resolve(
                self._full_attest_with_reason,
                self.attest_metadata_with_reason,
            )
            if not res[0]:
                logger.warning(
                    "FamilyLaunchAttestation failed: reason=%s family=%s",
                    res[1],
                    self.family,
                )
            return res

    def _full_attest_with_reason(self) -> tuple[bool, str]:
        if not self.runner.attest():
            return False, "runner_launch_attestation_failed"
        if not self.downstream.attest():
            return False, "downstream_cli_attestation_failed"
        ok, reason = self.config.attest_with_reason()
        if not ok:
            return False, f"config_scope_attestation_failed:{reason}"
        for package in self.critical_packages:
            ok, reason = package.attest_with_reason()
            if not ok:
                return False, f"critical_package_attestation_failed:{package.package_name}:{reason}"
        return True, "ok"

    def attest_metadata(self) -> bool:
        return self.attest_metadata_with_reason()[0]

    def attest_metadata_with_reason(self) -> tuple[bool, str]:
        if not self.runner.attest_metadata():
            return False, "runner_launch_metadata_attestation_failed"
        if not self.downstream.attest_metadata():
            return False, "downstream_cli_metadata_attestation_failed"
        ok, reason = self.config.attest_metadata_with_reason()
        if not ok:
            return False, f"config_scope_metadata_attestation_failed:{reason}"
        for package in self.critical_packages:
            ok, reason = package.attest_metadata_with_reason()
            if not ok:
                return False, f"critical_package_metadata_attestation_failed:{package.package_name}:{reason}"
        return True, "ok"


    def _assert_attested(self) -> None:
        if not self.attest():
            raise ExecutionContractError("family launch attestation drifted")

    @contextmanager
    def open_runner(self) -> Iterator[PinnedLaunch]:
        self._assert_attested()
        with open_pinned_runner_launch(self.runner) as pinned:
            yield pinned

    @contextmanager
    def open_downstream(self) -> Iterator[PinnedLaunch]:
        self._assert_attested()
        with open_pinned_launch(self.downstream) as pinned:
            yield pinned

    def materialize_sdk(self) -> MaterializedSdkLaunch:
        self._assert_attested()
        return materialize_sdk_launch_cached(self.downstream)

    def _unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "family": self.family,
            "runner": self.runner.to_dict(),
            "downstream": self.downstream.to_dict(),
            "config": self.config.to_dict(),
            "critical_packages": [
                package.to_dict() for package in self.critical_packages
            ],
        }

    def _computed_fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self._unsigned_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self._unsigned_dict(), "fingerprint": self.fingerprint}

    def to_payload(self) -> dict[str, Any]:
        return {"launch_attestation": self.to_dict()}

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> FamilyLaunchAttestation:
        if type(payload) is not dict or type(
            payload.get("launch_attestation"),
        ) is not dict:
            raise ExecutionContractError("invalid family launch payload")
        raw = payload["launch_attestation"]
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FamilyLaunchAttestation:
        _require_object(raw, {
            "schema",
            "family",
            "runner",
            "downstream",
            "config",
            "critical_packages",
            "fingerprint",
        }, "family launch attestation")
        packages = raw["critical_packages"]
        if (
            raw["schema"] != ATTESTATION_SCHEMA
            or type(raw["runner"]) is not dict
            or type(raw["downstream"]) is not dict
            or type(raw["config"]) is not dict
            or type(packages) is not list
            or any(type(value) is not dict for value in packages)
        ):
            raise ExecutionContractError("unsupported family launch attestation")
        value = cls(
            schema=required_integer(raw, "schema"),
            family=required_string(raw, "family"),
            runner=RunnerLaunch.from_dict(raw["runner"]),
            downstream=AttestedLaunch.from_dict(raw["downstream"]),
            config=ConfigScopeIdentity.from_dict(raw["config"]),
            critical_packages=tuple(
                CriticalPackageIdentity.from_dict(value) for value in packages
            ),
            fingerprint=required_string(raw, "fingerprint"),
        )
        if (
            value.family not in _FAMILIES
            or value.runner.runner_kind != value.family
            or value.downstream.logical_command != value.family
            or not SHA256_RE.fullmatch(value.fingerprint)
            or value.fingerprint != value._computed_fingerprint()
        ):
            raise ExecutionContractError("family launch attestation mismatch")
        return value


def build_family_launch_payload(
    attestation: FamilyLaunchAttestation,
) -> dict[str, Any]:
    if not isinstance(attestation, FamilyLaunchAttestation):
        raise ExecutionContractError("invalid family launch attestation")
    return attestation.to_payload()


def build_provider_family_launch_contract(
    provider: Mapping[str, Any],
    attestation: FamilyLaunchAttestation,
) -> dict[str, Any]:
    if provider.get("kind") != attestation.family:
        raise ExecutionContractError("provider launch family mismatch")
    from provider_execution_contract import provider_family_contract

    return provider_family_contract(
        provider,
        payload=build_family_launch_payload(attestation),
    )


__all__ = [
    "AttestedLaunch",
    "ConfigScopeIdentity",
    "CriticalPackageIdentity",
    "FamilyLaunchAttestation",
    "MaterializedSdkLaunch",
    "PinnedLaunch",
    "RunnerLaunch",
    "build_family_launch_payload",
    "build_provider_family_launch_contract",
    "capture_cli_launch",
    "capture_config_scope",
    "capture_critical_package",
    "capture_runner_launch",
    "materialize_sdk_launch",
    "open_pinned_launch",
    "open_pinned_runner_launch",
]
