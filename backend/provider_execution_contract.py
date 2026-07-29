from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping


FAMILY_CONTRACT_SCHEMA = 1
_FAMILY_TYPES = frozenset({"claude", "agy"})
_SECRET_KEY_RE = re.compile(
    r"(^|_)(api_?key|authorization|credential|password|secret|token)($|_)",
)


class ProviderExecutionContractError(RuntimeError):
    pass


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ProviderExecutionContractError(
            "provider execution contract must be JSON-compatible",
        ) from exc


def _reject_secrets(value: Any) -> None:
    if type(value) is list:
        for item in value:
            _reject_secrets(item)
        return
    if type(value) is not dict:
        return
    for key, item in value.items():
        if type(key) is not str:
            raise ProviderExecutionContractError(
                "provider execution contract keys must be strings",
            )
        normalized = key.lower().replace("-", "_")
        if (
            _SECRET_KEY_RE.search(normalized)
            and not normalized.endswith(("_ref", "_refs"))
            and item not in (None, "", [], {})
        ):
            raise ProviderExecutionContractError(
                "provider execution contract must be secret-free",
            )
        _reject_secrets(item)


@dataclass(frozen=True)
class _FrozenContract:
    _json: str = field(repr=False)

    @property
    def value(self) -> dict[str, Any]:
        value = json.loads(self._json)
        if type(value) is not dict:
            raise ProviderExecutionContractError(
                "provider execution contract is corrupt",
            )
        return value


def _decode_codex(raw: Mapping[str, Any]) -> _FrozenContract:
    from codex_execution_contract import CodexExecutionContract

    if type(raw) is not dict:
        raise ProviderExecutionContractError(
            "Codex execution contract must be an object",
        )
    contract = CodexExecutionContract.from_dict(raw)
    return _FrozenContract(_canonical_json(contract.to_dict()))


def _decode_family(raw: Mapping[str, Any]) -> _FrozenContract:
    expected = {
        "schema",
        "provider_id",
        "provider_kind",
        "provider_generation",
        "provider_revision",
        "payload",
    }
    if type(raw) is not dict or set(raw) != expected:
        raise ProviderExecutionContractError(
            "invalid provider family execution contract",
        )
    if (
        raw["schema"] != FAMILY_CONTRACT_SCHEMA
        or raw["provider_kind"] not in _FAMILY_TYPES
        or type(raw["provider_id"]) is not str
        or not raw["provider_id"]
        or type(raw["provider_generation"]) is not str
        or not raw["provider_generation"]
        or type(raw["provider_revision"]) is not int
        or raw["provider_revision"] < 0
        or type(raw["payload"]) is not dict
    ):
        raise ProviderExecutionContractError(
            "unsupported provider family execution contract",
        )
    _reject_secrets(raw["payload"])
    return _FrozenContract(_canonical_json(raw))


_CODECS: Mapping[str, Callable[[Mapping[str, Any]], _FrozenContract]] = MappingProxyType({
    "agy": _decode_family,
    "claude": _decode_family,
    "codex": _decode_codex,
})


def freeze_provider_contract(
    provider: Mapping[str, Any],
    value: Mapping[str, Any] | None,
) -> str | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"type", "contract"}:
        raise ProviderExecutionContractError(
            "invalid provider execution contract",
        )
    contract_type = value["type"]
    decoder = _CODECS.get(contract_type) if type(contract_type) is str else None
    if decoder is None or type(value["contract"]) is not dict:
        raise ProviderExecutionContractError(
            "unsupported provider execution contract",
        )
    contract = decoder(value["contract"])
    decoded = contract.value
    expected = (
        provider.get("id"),
        provider.get("kind"),
        provider.get("generation"),
        provider.get("revision"),
    )
    actual = (
        decoded.get("provider_id"),
        decoded.get("provider_kind"),
        decoded.get("provider_generation"),
        decoded.get("provider_revision"),
    )
    if actual != expected:
        raise ProviderExecutionContractError(
            "provider execution contract authority mismatch",
        )
    if contract_type in _FAMILY_TYPES and decoded["provider_kind"] != contract_type:
        raise ProviderExecutionContractError(
            "provider execution contract family mismatch",
        )
    return _canonical_json({
        "type": contract_type,
        "contract": decoded,
    })


def provider_family_contract(
    provider: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    kind = provider.get("kind")
    if kind not in _FAMILY_TYPES or type(payload) is not dict:
        raise ProviderExecutionContractError(
            "unsupported provider family execution contract",
        )
    envelope = {
        "type": kind,
        "contract": {
            "schema": FAMILY_CONTRACT_SCHEMA,
            "provider_id": provider.get("id"),
            "provider_kind": kind,
            "provider_generation": provider.get("generation"),
            "provider_revision": provider.get("revision"),
            "payload": dict(payload),
        },
    }
    freeze_provider_contract(provider, envelope)
    return envelope
