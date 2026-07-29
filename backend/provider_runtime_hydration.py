from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from codex_execution_common import ExecutionContractError
from provider_runtime_capability_model import PreparedRuntimeCapabilities


_SAFE_KIND_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class SecretHydrationRef:
    kind: str
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not str
            or not _SAFE_KIND_RE.fullmatch(self.kind)
            or type(self.value) is not str
            or not self.value
            or len(self.value) > 16384
        ):
            raise ExecutionContractError("invalid secret hydration reference")


@dataclass(frozen=True)
class PrewarmConnectionHydration:
    endpoint: str
    connect_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.endpoint) is not str
            or not self.endpoint
            or len(self.endpoint) > 4096
            or type(self.connect_secret) is not str
            or not self.connect_secret
            or len(self.connect_secret) > 4096
        ):
            raise ExecutionContractError("invalid MCP prewarm hydration")
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme != "tcp"
            or parsed.hostname not in _LOCAL_HOSTS
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ExecutionContractError("invalid MCP prewarm endpoint")


@dataclass(frozen=True)
class RuntimeHydrationRefs:
    provider_identity: SecretHydrationRef | None
    extension_identities: Mapping[str, SecretHydrationRef]
    runtime_broker: SecretHydrationRef | None
    backend_transport: SecretHydrationRef | None
    prewarm_connections: Mapping[str, PrewarmConnectionHydration]

    def __post_init__(self) -> None:
        for value, label in (
            (self.provider_identity, "provider identity hydration"),
            (self.runtime_broker, "runtime broker hydration"),
            (self.backend_transport, "backend transport hydration"),
        ):
            if value is not None and not isinstance(value, SecretHydrationRef):
                raise ExecutionContractError(f"invalid {label}")
        for mapping, value_type, label in (
            (
                self.extension_identities,
                SecretHydrationRef,
                "extension identity hydration",
            ),
            (
                self.prewarm_connections,
                PrewarmConnectionHydration,
                "prewarm connection hydration",
            ),
        ):
            if type(mapping) is not dict or any(
                type(key) is not str
                or not _SAFE_KIND_RE.fullmatch(key)
                or not isinstance(value, value_type)
                for key, value in mapping.items()
            ):
                raise ExecutionContractError(f"invalid {label}")
        object.__setattr__(
            self,
            "extension_identities",
            MappingProxyType(dict(self.extension_identities)),
        )
        object.__setattr__(
            self,
            "prewarm_connections",
            MappingProxyType(dict(self.prewarm_connections)),
        )


@dataclass(frozen=True)
class HydratedSpawnCapabilities:
    plan: dict
    prewarm_status: dict
    hydration: RuntimeHydrationRefs = field(repr=False)


def hydrate_spawn_capabilities(
    prepared: PreparedRuntimeCapabilities,
    hydration: RuntimeHydrationRefs,
) -> HydratedSpawnCapabilities:
    if not isinstance(prepared, PreparedRuntimeCapabilities):
        raise ExecutionContractError("invalid prepared runtime capabilities")
    manifest = prepared.manifest
    if set(hydration.extension_identities) - set(
        manifest["extension_ids"],
    ):
        raise ExecutionContractError("unknown extension identity hydration")
    required_connections = {
        name
        for name, status in prepared.prewarm_status.items()
        if status["status"] == "ready"
    }
    if set(hydration.prewarm_connections) != required_connections:
        raise ExecutionContractError("MCP prewarm hydration does not match plan")
    return HydratedSpawnCapabilities(
        plan=prepared.plan,
        prewarm_status=prepared.prewarm_status,
        hydration=hydration,
    )
