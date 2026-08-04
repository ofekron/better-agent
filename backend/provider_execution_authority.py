from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


_SECRET_KEY_RE = re.compile(
    r"(^|_)(api_?key|authorization|credential|password|secret|token)($|_)",
)


def _reject_secrets(value: Any) -> None:
    if type(value) is list:
        for item in value:
            _reject_secrets(item)
        return
    if type(value) is not dict:
        return
    for key, item in value.items():
        if type(key) is not str:
            raise ValueError("provider execution snapshot keys must be strings")
        normalized = key.lower().replace("-", "_")
        if (
            _SECRET_KEY_RE.search(normalized)
            and not normalized.endswith(("_ref", "_refs"))
            and item not in (None, "", [], {})
        ):
            raise ValueError("provider execution snapshot must be secret-free")
        _reject_secrets(item)


def _canonical_provider(provider: Mapping[str, Any]) -> str:
    if type(provider) is not dict:
        raise TypeError("provider execution snapshot must be an object")
    _reject_secrets(provider)
    try:
        return json.dumps(
            provider,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "provider execution snapshot must be JSON-compatible",
        ) from exc


@dataclass(frozen=True)
class ProviderExecutionCredential:
    provider_id: str
    provider_kind: str
    provider_generation: str
    provider_execution_revision: int
    status: Literal["available", "missing", "blocked"]
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            generation = str(uuid.UUID(self.provider_generation))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("credential generation must be a canonical UUID") from exc
        if (
            type(self.provider_id) is not str
            or not self.provider_id
            or type(self.provider_kind) is not str
            or not self.provider_kind
            or generation != self.provider_generation
            or type(self.provider_execution_revision) is not int
            or self.provider_execution_revision < 0
            or self.status not in {"available", "missing", "blocked"}
            or type(self.api_key) is not str
        ):
            raise ValueError("provider execution credential is invalid")

    @property
    def authoritative(self) -> bool:
        return self.status == "available" and bool(self.api_key)


@dataclass(frozen=True)
class ProviderExecutionHydration:
    _provider_json: str = field(repr=False)
    credential: ProviderExecutionCredential | None = field(
        default=None,
        repr=False,
    )

    @classmethod
    def create(
        cls,
        provider: Mapping[str, Any],
        credential: ProviderExecutionCredential | None = None,
    ) -> ProviderExecutionHydration:
        snapshot = _canonical_provider(provider)
        if credential is not None:
            authority = (
                provider.get("id"),
                provider.get("kind"),
                provider.get("generation"),
                provider.get("execution_revision"),
            )
            credential_authority = (
                credential.provider_id,
                credential.provider_kind,
                credential.provider_generation,
                credential.provider_execution_revision,
            )
            if credential_authority != authority:
                raise ValueError(
                    "provider credential authority does not match snapshot",
                )
        return cls(snapshot, credential)

    @property
    def provider(self) -> dict[str, Any]:
        value = json.loads(self._provider_json)
        if type(value) is not dict:
            raise RuntimeError("provider execution snapshot is corrupt")
        return value

    def runtime_record(self) -> dict[str, Any]:
        record = self.provider
        credential = self.credential
        if credential is None:
            return record
        record["api_key"] = credential.api_key
        record["_credential_status"] = credential.status
        record["_credential_authoritative"] = credential.authoritative
        return record
