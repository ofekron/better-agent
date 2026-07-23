from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import time
from typing import Any


class PrincipalKind(str, Enum):
    AGENT_RUN = "agent_run"
    EXTENSION_SERVER = "extension_server"
    NODE_RELAY = "node_relay"


@dataclass(frozen=True)
class RuntimePrincipal:
    kind: PrincipalKind
    principal_id: str
    issuer: str
    audience: str
    permitted_operations: tuple[str, ...]
    permitted_resources: tuple[str, ...]
    grant_generation: str
    availability_generation: str
    issued_at: float
    expires_at: float
    app_session_id: str = ""
    run_id: str = ""
    provider_id: str = ""
    node_id: str = ""
    cwd: str = ""
    server_id: str = ""
    context_complete: bool = True

    def __post_init__(self) -> None:
        if not self.principal_id or not self.issuer or not self.audience:
            raise ValueError("principal identity, issuer, and audience are required")
        if self.expires_at <= self.issued_at:
            raise ValueError("principal expiry must follow issuance")
        if not self.permitted_operations:
            raise ValueError("principal must permit at least one operation")

    def allows(self, operation: str) -> bool:
        return operation in self.permitted_operations and time.time() < self.expires_at

    def scope_digest(self) -> str:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "principal_id": self.principal_id,
            "issuer": self.issuer,
            "audience": self.audience,
            "operations": sorted(self.permitted_operations),
            "resources": sorted(self.permitted_resources),
            "grant_generation": self.grant_generation,
            "availability_generation": self.availability_generation,
            "app_session_id": self.app_session_id,
            "run_id": self.run_id,
            "provider_id": self.provider_id,
            "node_id": self.node_id,
            "cwd": self.cwd,
            "server_id": self.server_id,
            "context_complete": self.context_complete,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def idempotency_scope_digest(self) -> str:
        payload = {
            "kind": self.kind.value,
            "principal_id": self.principal_id,
            "issuer": self.issuer,
            "audience": self.audience,
            "operations": sorted(self.permitted_operations),
            "resources": sorted(self.permitted_resources),
            "grant_generation": self.grant_generation,
            "app_session_id": self.app_session_id,
            "run_id": self.run_id,
            "provider_id": self.provider_id,
            "node_id": self.node_id,
            "cwd": self.cwd,
            "server_id": self.server_id,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def claims(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "principal_id": self.principal_id,
            "issuer": self.issuer,
            "audience": self.audience,
            "permitted_operations": list(self.permitted_operations),
            "permitted_resources": list(self.permitted_resources),
            "grant_generation": self.grant_generation,
            "availability_generation": self.availability_generation,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "app_session_id": self.app_session_id,
            "run_id": self.run_id,
            "provider_id": self.provider_id,
            "node_id": self.node_id,
            "cwd": self.cwd,
            "server_id": self.server_id,
            "context_complete": self.context_complete,
        }

    def reference(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.claims().items()
            if key not in {"issued_at", "expires_at", "availability_generation"}
        }

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> RuntimePrincipal:
        return cls(
            kind=PrincipalKind(str(claims["kind"])),
            principal_id=str(claims["principal_id"]),
            issuer=str(claims["issuer"]),
            audience=str(claims["audience"]),
            permitted_operations=tuple(
                str(item) for item in claims["permitted_operations"]
            ),
            permitted_resources=tuple(
                str(item) for item in claims["permitted_resources"]
            ),
            grant_generation=str(claims["grant_generation"]),
            availability_generation=str(claims["availability_generation"]),
            issued_at=float(claims["issued_at"]),
            expires_at=float(claims["expires_at"]),
            app_session_id=str(claims.get("app_session_id") or ""),
            run_id=str(claims.get("run_id") or ""),
            provider_id=str(claims.get("provider_id") or ""),
            node_id=str(claims.get("node_id") or ""),
            cwd=str(claims.get("cwd") or ""),
            server_id=str(claims.get("server_id") or ""),
            context_complete=bool(claims.get("context_complete")),
        )

    @classmethod
    def from_reference(
        cls,
        reference: dict[str, Any],
        *,
        availability_generation: str,
        lifetime_seconds: float = 60.0,
    ) -> RuntimePrincipal:
        now = time.time()
        return cls.from_claims(
            {
                **reference,
                "availability_generation": availability_generation,
                "issued_at": now,
                "expires_at": now + lifetime_seconds,
            }
        )


def compatibility_extension_principal(
    *,
    extension_id: str,
    operation: str,
    grant_generation: str,
) -> RuntimePrincipal:
    now = time.time()
    return RuntimePrincipal(
        kind=PrincipalKind.EXTENSION_SERVER,
        principal_id=extension_id,
        issuer="better-agent-capability-api",
        audience="better-agent-operation-runtime",
        permitted_operations=(operation,),
        permitted_resources=(),
        grant_generation=grant_generation,
        availability_generation=grant_generation,
        issued_at=now,
        expires_at=now + 30.0,
        server_id="legacy-loopback",
        context_complete=False,
    )
