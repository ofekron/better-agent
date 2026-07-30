from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping


HEADLESS_REQUEST_SCHEMA = 1
HEADLESS_ADMISSION_SCHEMA = 1
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
    if timeout <= 0 or timeout > _MAX_TIMEOUT_SECONDS:
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
        prompt = raw["prompt"]
        if (
            type(prompt) is not str
            or not prompt
            or len(prompt) > _MAX_PROMPT_CHARS
            or "\x00" in prompt
        ):
            raise ValueError("headless request prompt is invalid")
        if type(raw["fork"]) is not bool:
            raise ValueError("headless request fork is invalid")
        if type(raw["no_tools"]) is not bool:
            raise ValueError("headless request no_tools is invalid")
        owner_kind, owner_id = _owner(raw["owner"])
        return cls(
            prompt=prompt,
            owner_kind=owner_kind,
            owner_id=owner_id,
            fork=raw["fork"],
            no_tools=raw["no_tools"],
            timeout=_positive_timeout(raw["timeout"]),
        )


@dataclass(frozen=True)
class HeadlessAuthority:
    owner_kind: str
    owner_id: str
    _provider_json: str = field(repr=False)
    model: str
    reasoning_effort: str
    runner: str
    permission_scope: str
    _routing_json: str = field(repr=False)
    cwd: str
    resume_sid: str | None
    supports_fork: bool
    supports_no_tools: bool

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
        if owner_kind not in {"session", "standalone"}:
            raise ValueError("headless authority owner is invalid")
        if type(owner_id) is not str or not owner_id:
            raise ValueError("headless authority owner is invalid")
        expected_provider = {
            "id",
            "kind",
            "generation",
            "revision",
        }
        _reject_secrets(provider, label="headless provider authority")
        if type(provider) is not dict or set(provider) != expected_provider:
            raise ValueError("headless provider authority is invalid")
        try:
            generation = str(uuid.UUID(provider["generation"]))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("headless provider authority is invalid") from exc
        if (
            type(provider["id"]) is not str
            or not provider["id"]
            or type(provider["kind"]) is not str
            or not provider["kind"]
            or generation != provider["generation"]
            or type(provider["revision"]) is not int
            or provider["revision"] < 0
        ):
            raise ValueError("headless provider authority is invalid")
        for value, label, allow_empty in (
            (model, "model", False),
            (reasoning_effort, "reasoning effort", True),
            (runner, "runner", False),
            (permission_scope, "permission scope", False),
            (cwd, "cwd", False),
        ):
            if type(value) is not str or (not allow_empty and not value):
                raise ValueError(f"headless authority {label} is invalid")
        if (
            "\x00" in cwd
            or not (
                PurePosixPath(cwd).is_absolute()
                or PureWindowsPath(cwd).is_absolute()
            )
        ):
            raise ValueError("headless authority cwd is invalid")
        if resume_sid is not None and (
            type(resume_sid) is not str or not resume_sid or "\x00" in resume_sid
        ):
            raise ValueError("headless authority resume sid is invalid")
        if type(supports_fork) is not bool or type(supports_no_tools) is not bool:
            raise ValueError("headless authority capabilities are invalid")
        _reject_secrets(routing, label="headless routing authority")
        if type(routing) is not dict or not routing:
            raise ValueError("headless routing authority is invalid")
        return cls(
            owner_kind=owner_kind,
            owner_id=owner_id,
            _provider_json=_canonical_json(
                provider,
                label="headless provider authority",
            ),
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
        return _json_object(
            self._provider_json,
            label="headless provider authority",
        )

    @property
    def routing(self) -> dict[str, Any]:
        return _json_object(
            self._routing_json,
            label="headless routing authority",
        )


@dataclass(frozen=True)
class AdmittedHeadlessRequest:
    _json: str = field(repr=False)

    @classmethod
    def create(
        cls,
        request: HeadlessRequest,
        authority: HeadlessAuthority,
    ) -> AdmittedHeadlessRequest:
        payload = {
            "schema": HEADLESS_ADMISSION_SCHEMA,
            "prompt": request.prompt,
            "owner": {
                "kind": authority.owner_kind,
                (
                    "id"
                    if authority.owner_kind == "session"
                    else "profile"
                ): authority.owner_id,
            },
            "provider": authority.provider,
            "model": authority.model,
            "reasoning_effort": authority.reasoning_effort,
            "runner": authority.runner,
            "permission_scope": authority.permission_scope,
            "routing": authority.routing,
            "cwd": authority.cwd,
            "resume_sid": authority.resume_sid,
            "fork": request.fork,
            "no_tools": request.no_tools,
            "timeout": request.timeout,
        }
        return cls(_canonical_json(payload, label="admitted headless request"))

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
    ) -> AdmittedHeadlessRequest:
        expected = {
            "schema", "prompt", "owner", "provider", "model",
            "reasoning_effort", "runner", "routing", "cwd",
            "permission_scope", "resume_sid", "fork", "no_tools", "timeout",
            "fingerprint",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError("invalid admitted headless request")
        request = HeadlessRequest.from_dict({
            "schema": raw["schema"],
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
        artifact = cls.create(request, authority)
        if (
            type(raw["fingerprint"]) is not str
            or raw["fingerprint"] != artifact.fingerprint
        ):
            raise ValueError("admitted headless request fingerprint mismatch")
        return artifact

    def to_dict(self) -> dict[str, Any]:
        payload = _json_object(
            self._json,
            label="admitted headless request",
        )
        payload["fingerprint"] = self.fingerprint
        return payload

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self._json.encode("utf-8")).hexdigest()


def admit_headless_request(
    request: HeadlessRequest,
    authority: HeadlessAuthority,
) -> AdmittedHeadlessRequest:
    if not isinstance(request, HeadlessRequest):
        raise TypeError("headless request is invalid")
    if not isinstance(authority, HeadlessAuthority):
        raise TypeError("headless authority is invalid")
    owner_key = "id" if request.owner_kind == "session" else "profile"
    request = HeadlessRequest.from_dict({
        "schema": HEADLESS_REQUEST_SCHEMA,
        "prompt": request.prompt,
        "owner": {
            "kind": request.owner_kind,
            owner_key: request.owner_id,
        },
        "fork": request.fork,
        "no_tools": request.no_tools,
        "timeout": request.timeout,
    })
    authority = HeadlessAuthority.create(
        owner_kind=authority.owner_kind,
        owner_id=authority.owner_id,
        provider=authority.provider,
        model=authority.model,
        reasoning_effort=authority.reasoning_effort,
        runner=authority.runner,
        permission_scope=authority.permission_scope,
        routing=authority.routing,
        cwd=authority.cwd,
        resume_sid=authority.resume_sid,
        supports_fork=authority.supports_fork,
        supports_no_tools=authority.supports_no_tools,
    )
    if (
        request.owner_kind != authority.owner_kind
        or request.owner_id != authority.owner_id
    ):
        raise HeadlessAdmissionError("headless request owner conflicts with authority")
    if request.fork:
        if not authority.supports_fork:
            raise HeadlessAdmissionError("headless provider does not support fork")
        if authority.resume_sid is None:
            raise HeadlessAdmissionError("headless fork requires a provider session")
    if request.no_tools and not authority.supports_no_tools:
        raise HeadlessAdmissionError(
            "headless provider cannot guarantee a no-tools run",
        )
    return AdmittedHeadlessRequest.create(request, authority)
