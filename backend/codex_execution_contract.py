from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from codex_execution_common import (
    ALLOWED_CONFIG_KEYS,
    ALLOWED_ENVIRONMENT_SELECTORS,
    CONTRACT_SCHEMA,
    SAFE_PROFILE_RE,
    SECRET_NAMES,
    SHA256_RE,
    ExecutionContractError,
    canonical_json,
    parallel_map,
    required_integer,
    required_string,
    required_string_list,
    timed_contract_step,
)
from codex_execution_identity import (
    ConfigIdentity,
    FileIdentity,
    config_identity_from_dict,
    file_identity_from_dict,
)
from codex_execution_launch import (
    LaunchChain,
    resolve_codex_launch_chain,
    validate_launch_chain,
)


@dataclass(frozen=True)
class CodexExecutionContract:
    schema: int
    provider_id: str
    provider_kind: str
    provider_generation: str
    provider_revision: int
    mode: str
    base_url: str
    profile: str
    credential_generation: int
    catalog_args: tuple[str, ...]
    runtime_args: tuple[str, ...]
    environment_selectors: tuple[tuple[str, str], ...]
    config: tuple[ConfigIdentity, ...]
    launch_chain: LaunchChain

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        if include_fingerprint:
            payload["fingerprint"] = hashlib.sha256(
                canonical_json(payload),
            ).hexdigest()
        return payload

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            canonical_json(self.to_dict(include_fingerprint=False)),
        ).hexdigest()

    @property
    def catalog_fingerprint(self) -> str:
        payload = {
            "schema": self.schema,
            "provider_id": self.provider_id,
            "provider_kind": self.provider_kind,
            "provider_generation": self.provider_generation,
            "provider_revision": self.provider_revision,
            "mode": self.mode,
            "base_url": self.base_url,
            "profile": self.profile,
            "credential_generation": self.credential_generation,
            "catalog_args": self.catalog_args,
            "environment_selectors": self.environment_selectors,
            "config": tuple(
                _catalog_config_identity(identity)
                for identity in self.config
            ),
            "launch_chain": {
                "logical_command": self.launch_chain.logical_command,
                "mode": self.launch_chain.mode,
                "launcher": _catalog_file_identity(
                    self.launch_chain.launcher,
                ),
                "argv_prefix": self.launch_chain.argv_prefix,
                "components": tuple(
                    _catalog_file_identity(identity)
                    for identity in self.launch_chain.components
                ),
                "component_argv_indexes": (
                    self.launch_chain.component_argv_indexes
                ),
                "sibling_components": tuple(
                    _catalog_file_identity(identity)
                    for identity in self.launch_chain.sibling_components
                ),
            },
        }
        return hashlib.sha256(canonical_json(payload)).hexdigest()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CodexExecutionContract:
        expected = {
            "schema",
            "provider_id",
            "provider_kind",
            "provider_generation",
            "provider_revision",
            "mode",
            "base_url",
            "profile",
            "credential_generation",
            "catalog_args",
            "runtime_args",
            "environment_selectors",
            "config",
            "launch_chain",
            "fingerprint",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ExecutionContractError("invalid execution contract")
        try:
            supplied_fingerprint = required_string(raw, "fingerprint")
            launch_raw = raw["launch_chain"]
            if type(launch_raw) is not dict:
                raise ExecutionContractError("invalid launch chain")
            if set(launch_raw) != {
                "logical_command",
                "mode",
                "launcher",
                "argv_prefix",
                "components",
                "component_argv_indexes",
                "sibling_components",
            }:
                raise ExecutionContractError("invalid launch chain")
            launcher = file_identity_from_dict(launch_raw["launcher"])
            logical_command = required_string(launch_raw, "logical_command")
            launch_mode = required_string(launch_raw, "mode")
            argv_prefix = required_string_list(launch_raw, "argv_prefix")
            components_raw = launch_raw.get("components")
            if type(components_raw) is not list:
                raise ExecutionContractError("invalid launch components")
            components = tuple(
                file_identity_from_dict(component)
                for component in components_raw
            )
            sibling_components_raw = launch_raw.get("sibling_components")
            if type(sibling_components_raw) is not list:
                raise ExecutionContractError("invalid launch sibling components")
            sibling_components = tuple(
                file_identity_from_dict(component)
                for component in sibling_components_raw
            )
            component_indexes_raw = launch_raw.get("component_argv_indexes")
            if (
                type(component_indexes_raw) is not list
                or any(type(index) is not int for index in component_indexes_raw)
            ):
                raise ExecutionContractError("invalid launch component indexes")
            launch_chain = LaunchChain(
                logical_command=logical_command,
                mode=launch_mode,
                launcher=launcher,
                argv_prefix=argv_prefix,
                components=components,
                component_argv_indexes=tuple(component_indexes_raw),
                sibling_components=sibling_components,
            )
            config_raw = raw.get("config")
            if type(config_raw) is not list:
                raise ExecutionContractError("invalid config identities")
            config = tuple(
                config_identity_from_dict(item)
                for item in config_raw
            )
            selectors_raw = raw.get("environment_selectors")
            if (
                type(selectors_raw) is not list
                or any(
                    type(item) is not list
                    or len(item) != 2
                    or any(type(value) is not str for value in item)
                    for item in selectors_raw
                )
            ):
                raise ExecutionContractError(
                    "invalid environment selectors",
                )
            contract = cls(
                schema=required_integer(raw, "schema"),
                provider_id=required_string(raw, "provider_id"),
                provider_kind=required_string(raw, "provider_kind"),
                provider_generation=required_string(
                    raw,
                    "provider_generation",
                ),
                provider_revision=required_integer(
                    raw,
                    "provider_revision",
                ),
                mode=required_string(raw, "mode"),
                base_url=required_string(raw, "base_url"),
                profile=required_string(raw, "profile"),
                credential_generation=required_integer(
                    raw,
                    "credential_generation",
                ),
                catalog_args=required_string_list(raw, "catalog_args"),
                runtime_args=required_string_list(raw, "runtime_args"),
                environment_selectors=tuple(
                    (item[0], item[1]) for item in selectors_raw
                ),
                config=config,
                launch_chain=launch_chain,
            )
        except ExecutionContractError:
            raise
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ExecutionContractError("invalid execution contract") from exc
        if (
            contract.schema != CONTRACT_SCHEMA
            or contract.provider_kind not in {"codex", "fugu"}
            or not contract.provider_id
            or not contract.provider_generation
            or contract.provider_revision < 0
            or contract.credential_generation != contract.provider_revision
            or not SAFE_PROFILE_RE.fullmatch(contract.profile)
            or not SHA256_RE.fullmatch(supplied_fingerprint)
        ):
            raise ExecutionContractError("unsupported execution contract")
        validate_launch_chain(contract.launch_chain)
        _clean_base_url(contract.base_url)
        _clean_config_args(contract.catalog_args)
        _clean_config_args(contract.runtime_args)
        _clean_environment_selectors(dict(contract.environment_selectors))
        if supplied_fingerprint != contract.fingerprint:
            raise ExecutionContractError("execution contract fingerprint mismatch")
        return contract

    def attest(self) -> bool:
        with timed_contract_step("provider.codex_contract.attest"):
            return (
                self.launch_chain.attest()
                and _attest_agent_authority_membership(self.config)
                and all(
                    parallel_map(
                        lambda identity: identity.attest(),
                        self.config,
                    ),
                )
            )

    def attest_metadata(self) -> bool:
        return (
            self.launch_chain.attest_metadata()
            and _attest_agent_authority_membership(self.config)
            and all(identity.attest_metadata() for identity in self.config)
        )


def _catalog_file_identity(identity: FileIdentity) -> dict[str, Any]:
    return {
        "requested_path": identity.requested_path,
        "resolved_path": identity.resolved_path,
        "sha256": identity.sha256,
        "size": identity.size,
        "symlink_chain": identity.symlink_chain,
    }


def _catalog_config_identity(identity: ConfigIdentity) -> dict[str, Any]:
    return {
        "root_path": identity.root_path,
        "config_path": identity.config_path,
        "config_file": (
            _catalog_file_identity(identity.config_file)
            if identity.config_file is not None
            else None
        ),
    }


def codex_authority_paths(config_root: Path) -> tuple[str, ...]:
    paths = [
        config_root / "AGENTS.md",
        config_root / "auth.json",
        config_root / "config.toml",
        *_codex_agent_authority_paths(config_root),
    ]
    return tuple(str(path) for path in sorted(paths))


def _codex_agent_authority_paths(config_root: Path) -> tuple[Path, ...]:
    agents_root = config_root / "agents"
    if agents_root.is_symlink():
        return (agents_root,)
    if not agents_root.is_dir():
        return ()
    return tuple(
        candidate
        for candidate in agents_root.rglob("*")
        if candidate.is_file() or candidate.is_symlink()
    )


def _attest_agent_authority_membership(
    config: tuple[ConfigIdentity, ...],
) -> bool:
    tracked = {Path(identity.config_path) for identity in config}
    roots = {Path(identity.root_path) for identity in config}
    for root in roots:
        agents_root = root / "agents"
        expected = {
            path
            for path in tracked
            if path.is_relative_to(agents_root)
        }
        if set(_codex_agent_authority_paths(root)) != expected:
            return False
    return True


def _clean_environment_selectors(
    raw: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    cleaned: list[tuple[str, str]] = []
    for key, value in sorted((raw or {}).items()):
        lowered = str(key).lower()
        if any(marker in lowered for marker in SECRET_NAMES):
            raise ExecutionContractError("secret selector is not allowed")
        if str(key) not in ALLOWED_ENVIRONMENT_SELECTORS:
            raise ExecutionContractError("environment selector is not allowed")
        cleaned.append((str(key), str(value)))
    return tuple(cleaned)


def _clean_config_args(raw: Sequence[str]) -> tuple[str, ...]:
    if type(raw) not in {list, tuple}:
        raise ExecutionContractError("config arguments must be a sequence")
    values = tuple(raw)
    if any(type(value) is not str for value in values) or len(values) % 2:
        raise ExecutionContractError("config arguments are invalid")
    cleaned: list[str] = []
    for index in range(0, len(values), 2):
        marker, assignment = values[index:index + 2]
        if marker != "-c" or "=" not in assignment:
            raise ExecutionContractError("config arguments are invalid")
        key, value = assignment.split("=", 1)
        if key not in ALLOWED_CONFIG_KEYS or "\x00" in value or "\n" in value:
            raise ExecutionContractError("config override is not allowed")
        if key != "shell_environment_policy.exclude" and any(
            secret in assignment.lower() for secret in SECRET_NAMES
        ):
            raise ExecutionContractError("secret config override is not allowed")
        cleaned.extend((marker, assignment))
    return tuple(cleaned)


def _clean_base_url(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ExecutionContractError("provider base URL contains credentials")
    return value


def build_codex_execution_contract(
    provider: Mapping[str, Any],
    *,
    launcher_path: str,
    profile: str | None = None,
    catalog_args: Sequence[str] = (),
    runtime_args: Sequence[str] = (),
    environment_selectors: Mapping[str, str] | None = None,
    config_paths: Sequence[str] | None = None,
    search_path: str | None = None,
    platform: str | None = None,
    architecture: str | None = None,
) -> CodexExecutionContract:
    provider_id = str(provider.get("id") or "").strip()
    provider_kind = str(provider.get("kind") or "").strip()
    generation = str(provider.get("generation") or "").strip()
    provider_revision = provider.get("revision")
    if not provider_id or provider_kind not in {"codex", "fugu"} or not generation:
        raise ExecutionContractError("provider authority is incomplete")
    if (
        type(provider_revision) is not int
        or provider_revision < 0
    ):
        raise ExecutionContractError("provider authority revision is invalid")
    cleaned_profile = str(profile or "")
    if not SAFE_PROFILE_RE.fullmatch(cleaned_profile):
        raise ExecutionContractError("provider profile is invalid")
    config_dir_raw = str(provider.get("config_dir") or "").strip()
    config_dir = (
        Path(config_dir_raw).expanduser()
        if config_dir_raw
        else Path.home() / ".codex"
    )
    if not config_dir.is_absolute():
        raise ExecutionContractError("provider config root must be absolute")
    try:
        canonical_config_dir = config_dir.resolve(strict=True)
    except OSError as exc:
        raise ExecutionContractError("provider config root is unavailable") from exc
    observed_paths = (
        tuple(Path(path) for path in config_paths)
        if config_paths is not None
        else (canonical_config_dir / "config.toml",)
    )
    config = tuple(
        ConfigIdentity.capture(canonical_config_dir, path)
        for path in observed_paths
    )
    launch_chain = resolve_codex_launch_chain(
        launcher_path,
        search_path=search_path,
        platform=platform,
        architecture=architecture,
    )
    return CodexExecutionContract(
        schema=CONTRACT_SCHEMA,
        provider_id=provider_id,
        provider_kind=provider_kind,
        provider_generation=generation,
        provider_revision=provider_revision,
        mode=str(provider.get("mode") or "subscription"),
        base_url=_clean_base_url(provider.get("base_url")),
        profile=cleaned_profile,
        credential_generation=provider_revision,
        catalog_args=_clean_config_args(catalog_args),
        runtime_args=_clean_config_args(runtime_args),
        environment_selectors=_clean_environment_selectors(environment_selectors),
        config=config,
        launch_chain=launch_chain,
    )
