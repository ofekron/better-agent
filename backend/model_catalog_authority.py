from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping

import provider_sync_authority
from codex_execution import CodexExecutionContract, ExecutionContractError
from codex_execution_common import SHA256_RE, canonical_json


_AUTHORITY_KEYS = frozenset({
    "provider",
    "provider_state",
    "source",
    "discovery",
    "fingerprint",
})
_PROVIDER_KEYS = frozenset({
    "id",
    "generation",
    "revision",
    "execution_revision",
})
_SOURCE_KEYS = frozenset({"execution_contract", "catalog_fingerprint"})
_DISCOVERY_KEYS = frozenset({"method", "version"})
_DISCOVERY_METHOD_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class CatalogAuthorityError(RuntimeError):
    pass


def _execution_payload(contract: CodexExecutionContract) -> dict[str, Any]:
    return json.loads(canonical_json(contract.to_dict()))


@dataclass(frozen=True)
class CatalogAuthority:
    provider_id: str
    provider_generation: str
    provider_revision: int
    provider_execution_revision: int
    provider_state_generation: str
    provider_state_revision: int
    provider_state_digest: str
    execution_contract: CodexExecutionContract
    discovery_method: str
    discovery_version: int

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "provider": {
                "id": self.provider_id,
                "generation": self.provider_generation,
                "revision": self.provider_revision,
                "execution_revision": self.provider_execution_revision,
            },
            "provider_state": {
                "generation": self.provider_state_generation,
                "revision": self.provider_state_revision,
                "digest": self.provider_state_digest,
            },
            "source": {
                "execution_contract": _execution_payload(
                    self.execution_contract,
                ),
                "catalog_fingerprint": (
                    self.execution_contract.catalog_fingerprint
                ),
            },
            "discovery": {
                "method": self.discovery_method,
                "version": self.discovery_version,
            },
        }
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

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CatalogAuthority:
        try:
            if type(raw) is not dict or set(raw) != _AUTHORITY_KEYS:
                raise CatalogAuthorityError("invalid catalog authority")
            provider = raw.get("provider")
            source = raw.get("source")
            discovery = raw.get("discovery")
            if type(provider) is not dict or set(provider) != _PROVIDER_KEYS:
                raise CatalogAuthorityError("invalid provider authority")
            if type(source) is not dict or set(source) != _SOURCE_KEYS:
                raise CatalogAuthorityError("invalid catalog source")
            if (
                type(discovery) is not dict
                or set(discovery) != _DISCOVERY_KEYS
            ):
                raise CatalogAuthorityError("invalid discovery identity")
            provider_id = provider.get("id")
            if (
                type(provider_id) is not str
                or not provider_id
                or len(provider_id) > 128
                or provider_id.strip() != provider_id
                or any(ord(character) < 32 for character in provider_id)
            ):
                raise CatalogAuthorityError("invalid provider id")
            provider_authority = provider_sync_authority.parse_record_authority(
                provider.get("generation"),
                provider.get("revision"),
            )
            provider_execution_revision = provider.get("execution_revision")
            if (
                type(provider_execution_revision) is not int
                or provider_execution_revision < 0
            ):
                raise CatalogAuthorityError(
                    "invalid provider execution authority"
                )
            state_authority = provider_sync_authority.parse_authority(
                raw.get("provider_state"),
            )
            execution_raw = source.get("execution_contract")
            if type(execution_raw) is not dict:
                raise CatalogAuthorityError("invalid execution contract")
            execution_contract = CodexExecutionContract.from_dict(
                execution_raw,
            )
            if execution_contract.runtime_args:
                raise CatalogAuthorityError(
                    "catalog source contains runtime-only arguments",
                )
            catalog_fingerprint = source.get("catalog_fingerprint")
            if (
                type(catalog_fingerprint) is not str
                or not SHA256_RE.fullmatch(catalog_fingerprint)
                or catalog_fingerprint
                != execution_contract.catalog_fingerprint
            ):
                raise CatalogAuthorityError(
                    "catalog source fingerprint mismatch",
                )
            method = discovery.get("method")
            version = discovery.get("version")
            supplied_fingerprint = raw.get("fingerprint")
            if (
                type(method) is not str
                or not _DISCOVERY_METHOD_RE.fullmatch(method)
                or type(version) is not int
                or version < 1
                or type(supplied_fingerprint) is not str
                or not SHA256_RE.fullmatch(supplied_fingerprint)
            ):
                raise CatalogAuthorityError("invalid discovery identity")
            authority = cls(
                provider_id=provider_id,
                provider_generation=provider_authority["generation"],
                provider_revision=provider_authority["revision"],
                provider_execution_revision=provider_execution_revision,
                provider_state_generation=state_authority["generation"],
                provider_state_revision=state_authority["revision"],
                provider_state_digest=state_authority["digest"],
                execution_contract=execution_contract,
                discovery_method=method,
                discovery_version=version,
            )
        except CatalogAuthorityError:
            raise
        except (ExecutionContractError, ValueError) as exc:
            raise CatalogAuthorityError("invalid catalog authority") from exc
        if (
            execution_contract.provider_id != authority.provider_id
            or execution_contract.provider_generation
            != authority.provider_generation
            or execution_contract.provider_execution_revision
            != authority.provider_execution_revision
        ):
            raise CatalogAuthorityError(
                "execution contract provider authority mismatch",
            )
        if supplied_fingerprint != authority.fingerprint:
            raise CatalogAuthorityError("catalog authority fingerprint mismatch")
        return authority


def build_catalog_authority(
    *,
    provider_id: str,
    provider_generation: str,
    provider_revision: int,
    provider_state_authority: Mapping[str, Any],
    execution_contract: CodexExecutionContract,
    discovery_method: str,
    discovery_version: int,
) -> CatalogAuthority:
    if (
        type(provider_state_authority) is not dict
        or type(execution_contract) is not CodexExecutionContract
    ):
        raise CatalogAuthorityError("invalid catalog authority")
    catalog_contract = replace(execution_contract, runtime_args=())
    raw = {
        "provider": {
            "id": provider_id,
            "generation": provider_generation,
            "revision": provider_revision,
            "execution_revision": (
                execution_contract.provider_execution_revision
            ),
        },
        "provider_state": dict(provider_state_authority),
        "source": {
            "execution_contract": _execution_payload(catalog_contract),
            "catalog_fingerprint": catalog_contract.catalog_fingerprint,
        },
        "discovery": {
            "method": discovery_method,
            "version": discovery_version,
        },
    }
    raw["fingerprint"] = hashlib.sha256(canonical_json(raw)).hexdigest()
    return CatalogAuthority.from_dict(raw)
