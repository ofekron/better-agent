from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping


HEADLESS_REQUEST_SCHEMA = 1
HEADLESS_ADMISSION_SCHEMA = 3
_MAX_PROMPT_CHARS = 1_000_000
_MAX_TIMEOUT_SECONDS = 86_400.0
_SECRET_KEY_RE = re.compile(
    r"(^|_)(api_?key|authorization|credential|password|secret|token)($|_)",
)


class HeadlessAdmissionError(RuntimeError):
    pass


def _canonical_json(value: Any, *, label: str) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON-compatible") from exc


def _json_object(encoded: str, *, label: str) -> dict[str, Any]:
    value = json.loads(encoded)
    if type(value) is not dict:
        raise RuntimeError(f"{label} is corrupt")
    return value


def _reject_secrets(value: Any, *, label: str, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError(f"{label} is nested too deeply")
    if value is None or type(value) in (str, bool, int, float):
        return
    if type(value) is list:
        for item in value:
            _reject_secrets(item, label=label, depth=depth + 1)
        return
    if type(value) is not dict:
        raise ValueError(f"{label} must be JSON-compatible")
    for key, item in value.items():
        if type(key) is not str:
            raise ValueError(f"{label} keys must be strings")
        normalized = key.lower().replace("-", "_")
        if (
            _SECRET_KEY_RE.search(normalized)
            and not normalized.endswith(("_ref", "_refs"))
            and item not in (None, "", [], {})
        ):
            raise ValueError(f"{label} must be secret-free")
        _reject_secrets(item, label=label, depth=depth + 1)


def _owner(raw: Any) -> tuple[str, str]:
    if type(raw) is not dict:
        raise ValueError("headless request owner is invalid")
    kind = raw.get("kind")
    if kind == "session" and set(raw) == {"kind", "id"}:
        owner_id = raw["id"]
    elif kind == "standalone" and set(raw) == {"kind", "profile"}:
        owner_id = raw["profile"]
    else:
        raise ValueError("headless request owner is invalid")
    if type(owner_id) is not str or not owner_id.strip():
        raise ValueError("headless request owner is invalid")
    return kind, owner_id.strip()


def _positive_timeout(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ValueError("headless request timeout is invalid")
    timeout = float(value)
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or timeout > _MAX_TIMEOUT_SECONDS
    ):
        raise ValueError("headless request timeout is invalid")
    return timeout


@dataclass(frozen=True)
class HeadlessRequest:
    prompt: str
    owner_kind: str
    owner_id: str
    fork: bool
    no_tools: bool
    timeout: float | None

    def __post_init__(self) -> None:
        if (
            type(self.prompt) is not str
            or not self.prompt
            or len(self.prompt) > _MAX_PROMPT_CHARS
            or "\x00" in self.prompt
        ):
            raise ValueError("headless request prompt is invalid")
        _, owner_id = _owner({
            "kind": self.owner_kind,
            (
                "id" if self.owner_kind == "session" else "profile"
            ): self.owner_id,
        })
        object.__setattr__(self, "owner_id", owner_id)
        if type(self.fork) is not bool:
            raise ValueError("headless request fork is invalid")
        if type(self.no_tools) is not bool:
            raise ValueError("headless request no_tools is invalid")
        object.__setattr__(self, "timeout", _positive_timeout(self.timeout))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> HeadlessRequest:
        expected = {
            "schema",
            "prompt",
            "owner",
            "fork",
            "no_tools",
            "timeout",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError("invalid headless request")
        if raw["schema"] != HEADLESS_REQUEST_SCHEMA:
            raise ValueError("unsupported headless request schema")
        owner_kind, owner_id = _owner(raw["owner"])
        return cls(
            prompt=raw["prompt"],
            owner_kind=owner_kind,
            owner_id=owner_id,
            fork=raw["fork"],
            no_tools=raw["no_tools"],
            timeout=raw["timeout"],
        )


@dataclass(frozen=True)
class HeadlessAuthority:
    owner_kind: str
    owner_id: str
    provider_id: str
    provider_kind: str
    provider_generation: str
    provider_execution_revision: int
    model: str
    reasoning_effort: str
    runner: str
    permission_scope: str
    _routing_json: str = field(repr=False)
    cwd: str
    resume_sid: str | None
    supports_fork: bool
    supports_no_tools: bool

    def __post_init__(self) -> None:
        _, owner_id = _owner({
            "kind": self.owner_kind,
            (
                "id" if self.owner_kind == "session" else "profile"
            ): self.owner_id,
        })
        object.__setattr__(self, "owner_id", owner_id)
        try:
            generation = str(uuid.UUID(self.provider_generation))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("headless provider authority is invalid") from exc
        if (
            type(self.provider_id) is not str
            or not self.provider_id
            or type(self.provider_kind) is not str
            or not self.provider_kind
            or generation != self.provider_generation
            or type(self.provider_execution_revision) is not int
            or self.provider_execution_revision < 0
        ):
            raise ValueError("headless provider authority is invalid")
        for value, label, allow_empty in (
            (self.model, "model", False),
            (self.reasoning_effort, "reasoning effort", True),
            (self.runner, "runner", False),
            (self.permission_scope, "permission scope", False),
            (self.cwd, "cwd", False),
        ):
            if type(value) is not str or (not allow_empty and not value):
                raise ValueError(f"headless authority {label} is invalid")
        if (
            "\x00" in self.cwd
            or not (
                PurePosixPath(self.cwd).is_absolute()
                or PureWindowsPath(self.cwd).is_absolute()
            )
        ):
            raise ValueError("headless authority cwd is invalid")
        if self.resume_sid is not None and (
            type(self.resume_sid) is not str
            or not self.resume_sid
            or "\x00" in self.resume_sid
        ):
            raise ValueError("headless authority resume sid is invalid")
        if (
            type(self.supports_fork) is not bool
            or type(self.supports_no_tools) is not bool
        ):
            raise ValueError("headless authority capabilities are invalid")
        if type(self._routing_json) is not str:
            raise ValueError("headless routing authority is invalid")
        try:
            routing = json.loads(self._routing_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("headless routing authority is invalid") from exc
        _reject_secrets(routing, label="headless routing authority")
        if type(routing) is not dict or not routing:
            raise ValueError("headless routing authority is invalid")
        object.__setattr__(
            self,
            "_routing_json",
            _canonical_json(routing, label="headless routing authority"),
        )

    @classmethod
    def create(
        cls,
        *,
        owner_kind: str,
        owner_id: str,
        provider: Mapping[str, Any],
        model: str,
        reasoning_effort: str,
        runner: str,
        permission_scope: str,
        routing: Mapping[str, Any],
        cwd: str,
        resume_sid: str | None,
        supports_fork: bool,
        supports_no_tools: bool,
    ) -> HeadlessAuthority:
        expected_provider = {
            "id",
            "kind",
            "generation",
            "execution_revision",
        }
        _reject_secrets(provider, label="headless provider authority")
        if type(provider) is not dict or set(provider) != expected_provider:
            raise ValueError("headless provider authority is invalid")
        return cls(
            owner_kind=owner_kind,
            owner_id=owner_id,
            provider_id=provider["id"],
            provider_kind=provider["kind"],
            provider_generation=provider["generation"],
            provider_execution_revision=provider["execution_revision"],
            model=model,
            reasoning_effort=reasoning_effort,
            runner=runner,
            permission_scope=permission_scope,
            _routing_json=_canonical_json(
                routing,
                label="headless routing authority",
            ),
            cwd=cwd,
            resume_sid=resume_sid,
            supports_fork=supports_fork,
            supports_no_tools=supports_no_tools,
        )

    @property
    def provider(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "kind": self.provider_kind,
            "generation": self.provider_generation,
            "execution_revision": self.provider_execution_revision,
        }

    @property
    def routing(self) -> dict[str, Any]:
        return _json_object(
            self._routing_json,
            label="headless routing authority",
        )


@dataclass(frozen=True)
class AdmittedHeadlessRequest:
    request: HeadlessRequest = field(repr=False)
    authority: HeadlessAuthority = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, HeadlessRequest):
            raise TypeError("headless request is invalid")
        if not isinstance(self.authority, HeadlessAuthority):
            raise TypeError("headless authority is invalid")
        if (
            self.request.owner_kind != self.authority.owner_kind
            or self.request.owner_id != self.authority.owner_id
        ):
            raise HeadlessAdmissionError(
                "headless request owner conflicts with authority",
            )
        if self.request.fork:
            if not self.authority.supports_fork:
                raise HeadlessAdmissionError(
                    "headless provider does not support fork",
                )
            if self.authority.resume_sid is None:
                raise HeadlessAdmissionError(
                    "headless fork requires a provider session",
                )
        if self.request.no_tools and not self.authority.supports_no_tools:
            raise HeadlessAdmissionError(
                "headless provider cannot guarantee a no-tools run",
            )

    @classmethod
    def create(
        cls,
        request: HeadlessRequest,
        authority: HeadlessAuthority,
    ) -> AdmittedHeadlessRequest:
        return cls(request=request, authority=authority)

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
    ) -> AdmittedHeadlessRequest:
        expected = {
            "schema", "prompt", "owner", "provider", "model",
            "reasoning_effort", "runner", "routing", "cwd",
            "permission_scope", "resume_sid", "fork", "no_tools", "timeout",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError("invalid admitted headless request")
        if raw["schema"] != HEADLESS_ADMISSION_SCHEMA:
            raise ValueError("unsupported admitted headless request schema")
        request = HeadlessRequest.from_dict({
            "schema": HEADLESS_REQUEST_SCHEMA,
            "prompt": raw["prompt"],
            "owner": raw["owner"],
            "fork": raw["fork"],
            "no_tools": raw["no_tools"],
            "timeout": raw["timeout"],
        })
        authority = HeadlessAuthority.create(
            owner_kind=request.owner_kind,
            owner_id=request.owner_id,
            provider=raw["provider"],
            model=raw["model"],
            reasoning_effort=raw["reasoning_effort"],
            runner=raw["runner"],
            permission_scope=raw["permission_scope"],
            routing=raw["routing"],
            cwd=raw["cwd"],
            resume_sid=raw["resume_sid"],
            supports_fork=request.fork,
            supports_no_tools=request.no_tools,
        )
        return cls.create(request, authority)

    def to_dict(self) -> dict[str, Any]:
        owner_key = "id" if self.authority.owner_kind == "session" else "profile"
        return {
            "schema": HEADLESS_ADMISSION_SCHEMA,
            "prompt": self.request.prompt,
            "owner": {
                "kind": self.authority.owner_kind,
                owner_key: self.authority.owner_id,
            },
            "provider": self.authority.provider,
            "model": self.authority.model,
            "reasoning_effort": self.authority.reasoning_effort,
            "runner": self.authority.runner,
            "permission_scope": self.authority.permission_scope,
            "routing": self.authority.routing,
            "cwd": self.authority.cwd,
            "resume_sid": self.authority.resume_sid,
            "fork": self.request.fork,
            "no_tools": self.request.no_tools,
            "timeout": self.request.timeout,
        }


def admit_headless_request(
    request: HeadlessRequest,
    authority: HeadlessAuthority,
) -> AdmittedHeadlessRequest:
    if not isinstance(request, HeadlessRequest):
        raise TypeError("headless request is invalid")
    if not isinstance(authority, HeadlessAuthority):
        raise TypeError("headless authority is invalid")
    return AdmittedHeadlessRequest.create(request, authority)
