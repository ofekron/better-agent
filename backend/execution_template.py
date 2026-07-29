from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping
from urllib.parse import urlsplit


EXECUTION_SCHEMA = 2
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SECRET_KEY_RE = re.compile(
    r"(^|_)(api_?key|authorization|credential|password|secret|token)($|_)",
)

_REQUIRED_ARGUMENTS = {
    "run_id",
    "prompt",
    "cwd",
    "model",
    "reasoning_effort",
    "session_id",
    "mode",
    "app_session_id",
}
_ARGUMENT_DEFAULTS: dict[str, Any] = {
    "images": None,
    "files": None,
    "source": None,
    "disallowed_tools": None,
    "setting_sources": None,
    "fork": False,
    "supervised": False,
    "supervisor_agent_session_id": None,
    "worker_agent_session_id": None,
    "mssg_sender_session_id": None,
    "is_worker": False,
    "browser_harness_enabled": False,
    "user_facing": False,
    "working_mode": None,
    "continuation_chain": None,
    "target_message_id": None,
    "turn_run_id": None,
    "disabled_builtin_extensions": None,
    "provisioned_tool_profile": "",
    "provider_run_config": None,
    "capability_contexts": None,
    "resolved_harness_run_config": None,
}
_VOLATILE_DEFAULTS: dict[str, Any] = {
    "internal_token": None,
    "extra_env": None,
    "backend_url": None,
}
_ALL_ARGUMENTS = (
    _REQUIRED_ARGUMENTS
    | set(_ARGUMENT_DEFAULTS)
    | set(_VOLATILE_DEFAULTS)
)


class ExecutionAuthorityError(RuntimeError):
    pass


def _validate_json_tree(value: Any, *, field_name: str, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError(f"{field_name} is nested too deeply")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{field_name} contains a non-finite number")
        return
    if type(value) is list:
        for item in value:
            _validate_json_tree(item, field_name=field_name, depth=depth + 1)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError(f"{field_name} object keys must be strings")
        for item in value.values():
            _validate_json_tree(item, field_name=field_name, depth=depth + 1)
        return
    raise ValueError(f"{field_name} must be JSON-compatible")


def _validate_optional_string(value: Any, field_name: str) -> None:
    if value is not None and type(value) is not str:
        raise ValueError(f"{field_name} must be a string or null")


def _validate_optional_string_list(value: Any, field_name: str) -> None:
    if value is None:
        return
    if type(value) is not list or any(type(item) is not str for item in value):
        raise ValueError(f"{field_name} must be a string list or null")


def _validate_attachments(value: Any, field_name: str) -> None:
    if value is None:
        return
    if type(value) is not list:
        raise ValueError(f"{field_name} must be a list or null")
    for index, item in enumerate(value):
        if type(item) is not dict:
            raise ValueError(f"{field_name}[{index}] must be an object")
        if type(item.get("data")) is not str or not item["data"]:
            raise ValueError(f"{field_name}[{index}].data must be a non-empty string")
        for key in ("name", "media_type", "mime_type"):
            if key in item and type(item[key]) is not str:
                raise ValueError(f"{field_name}[{index}].{key} must be a string")
        if "size" in item and (
            type(item["size"]) is not int or item["size"] < 0
        ):
            raise ValueError(f"{field_name}[{index}].size must be a non-negative integer")


def _reject_embedded_secrets(value: Any, field_name: str) -> None:
    if type(value) is list:
        for item in value:
            _reject_embedded_secrets(item, field_name)
        return
    if type(value) is not dict:
        return
    for key, item in value.items():
        normalized_key = key.lower().replace("-", "_")
        is_reference = normalized_key.endswith(("_ref", "_refs"))
        if (
            not is_reference
            and _SECRET_KEY_RE.search(normalized_key)
            and item not in (None, "", [], {})
        ):
            raise ValueError(f"{field_name} must contain secret references, not values")
        _reject_embedded_secrets(item, field_name)


def _is_absolute_path(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("execution arguments must be JSON-compatible") from exc


def _json_object(encoded: str) -> dict[str, Any]:
    value = json.loads(encoded)
    if type(value) is not dict:
        raise ValueError("execution arguments must be an object")
    return value


def _freeze_runtime_policy(value: Mapping[str, Any] | None) -> str:
    policy = dict(value or {})
    _validate_json_tree(policy, field_name="execution runtime policy")
    _reject_embedded_secrets(policy, "execution runtime policy")
    return _canonical_json(policy)


def _freeze_provider_contract(
    provider: Mapping[str, Any],
    value: Mapping[str, Any] | None,
) -> str | None:
    from provider_execution_contract import (
        ProviderExecutionContractError,
        freeze_provider_contract,
    )

    try:
        return freeze_provider_contract(provider, value)
    except ProviderExecutionContractError as exc:
        raise ExecutionAuthorityError(str(exc)) from exc


def _normalize_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    if type(arguments) is not dict:
        raise ValueError("execution arguments must be an object")
    unknown = set(arguments) - _ALL_ARGUMENTS
    missing = _REQUIRED_ARGUMENTS - set(arguments)
    if unknown or missing:
        raise ValueError("invalid execution arguments")
    normalized = {
        **_ARGUMENT_DEFAULTS,
        **_VOLATILE_DEFAULTS,
        **arguments,
    }
    for key in ("run_id", "cwd", "mode", "app_session_id"):
        if type(normalized[key]) is not str or not normalized[key]:
            raise ValueError(f"{key} must be a non-empty string")
    if type(normalized["prompt"]) is not str:
        raise ValueError("prompt must be a string")
    if not _RUN_ID_RE.fullmatch(normalized["run_id"]):
        raise ValueError("run_id must be a safe path component")
    if "\x00" in normalized["cwd"] or not _is_absolute_path(normalized["cwd"]):
        raise ValueError("cwd must be an absolute path")
    if normalized["mode"] not in ("native", "manager", "team"):
        raise ValueError("mode must be native, manager, or team")
    backend_url_value = normalized.get("backend_url")
    if backend_url_value is not None and type(backend_url_value) is not str:
        raise ValueError("backend_url must be a string or null")
    backend_url = backend_url_value or ""
    parsed_backend_url = urlsplit(backend_url)
    if (
        parsed_backend_url.username is not None
        or parsed_backend_url.password is not None
        or parsed_backend_url.query
        or parsed_backend_url.fragment
        or (
            backend_url
            and (
                parsed_backend_url.scheme not in ("http", "https")
                or not parsed_backend_url.hostname
            )
        )
    ):
        raise ValueError("backend_url must be an HTTP URL without credentials")
    for key in (
        "model",
        "reasoning_effort",
        "session_id",
        "source",
        "supervisor_agent_session_id",
        "worker_agent_session_id",
        "mssg_sender_session_id",
        "working_mode",
        "target_message_id",
        "turn_run_id",
    ):
        _validate_optional_string(normalized[key], key)
    if type(normalized["provisioned_tool_profile"]) is not str:
        raise ValueError("provisioned_tool_profile must be a string")
    for key in (
        "disallowed_tools",
        "setting_sources",
        "continuation_chain",
        "disabled_builtin_extensions",
    ):
        _validate_optional_string_list(normalized[key], key)
    _validate_attachments(normalized["images"], "images")
    _validate_attachments(normalized["files"], "files")
    if (
        normalized["provider_run_config"] is not None
        and type(normalized["provider_run_config"]) is not dict
    ):
        raise ValueError("provider_run_config must be an object or null")
    if (
        normalized["capability_contexts"] is not None
        and (
            type(normalized["capability_contexts"]) is not list
            or any(type(item) is not dict for item in normalized["capability_contexts"])
        )
    ):
        raise ValueError("capability_contexts must be an object list or null")
    if (
        normalized["resolved_harness_run_config"] is not None
        and type(normalized["resolved_harness_run_config"]) is not dict
    ):
        raise ValueError("resolved_harness_run_config must be an object or null")
    for key in ("provider_run_config", "resolved_harness_run_config"):
        _reject_embedded_secrets(normalized[key], key)
    if normalized["internal_token"] is not None and type(normalized["internal_token"]) is not str:
        raise ValueError("internal_token must be a string or null")
    from execution_environment import validate_extra_env

    validate_extra_env(normalized["extra_env"])
    for key in (
        "fork",
        "supervised",
        "is_worker",
        "browser_harness_enabled",
        "user_facing",
    ):
        if type(normalized[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    _validate_json_tree(normalized, field_name="execution arguments")
    _canonical_json(normalized)
    return normalized


@dataclass(frozen=True)
class ExecutionTemplate:
    _arguments_json: str = field(repr=False)

    @classmethod
    def create(cls, arguments: Mapping[str, Any]) -> ExecutionTemplate:
        normalized = _normalize_arguments(arguments)
        durable = {
            key: value
            for key, value in normalized.items()
            if key not in _VOLATILE_DEFAULTS
        }
        return cls(_canonical_json(durable))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ExecutionTemplate:
        if type(raw) is not dict or set(raw) != {"schema", "arguments"}:
            raise ValueError("invalid execution template")
        if raw["schema"] != EXECUTION_SCHEMA or type(raw["arguments"]) is not dict:
            raise ValueError("unsupported execution template")
        if set(raw["arguments"]) & set(_VOLATILE_DEFAULTS):
            raise ValueError("volatile execution data cannot be persisted")
        full = {**_VOLATILE_DEFAULTS, **raw["arguments"]}
        return cls.create(full)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EXECUTION_SCHEMA,
            "arguments": _json_object(self._arguments_json),
        }

    def arguments(self) -> dict[str, Any]:
        return _json_object(self._arguments_json)


@dataclass(frozen=True)
class ExecutionArtifact:
    provider_id: str
    provider_kind: str
    provider_generation: str
    provider_revision: int
    routing_session_id: str
    template: ExecutionTemplate
    _runtime_policy_json: str = field(repr=False)
    _provider_contract_json: str | None = field(repr=False)

    @classmethod
    def create(
        cls,
        provider: Mapping[str, Any],
        template: ExecutionTemplate,
        *,
        routing_session_id: str | None = None,
        runtime_policy: Mapping[str, Any] | None = None,
        provider_contract: Mapping[str, Any] | None = None,
    ) -> ExecutionArtifact:
        provider_id = provider.get("id")
        provider_kind = provider.get("kind")
        generation = provider.get("generation")
        revision = provider.get("revision")
        try:
            parsed_generation = uuid.UUID(generation)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ExecutionAuthorityError(
                "provider generation must be a canonical UUID",
            ) from exc
        if (
            type(provider_id) is not str
            or not provider_id
            or type(provider_kind) is not str
            or not provider_kind
            or str(parsed_generation) != generation
            or type(revision) is not int
            or revision < 0
        ):
            raise ExecutionAuthorityError("provider authority is invalid")
        route = (
            routing_session_id
            if routing_session_id is not None
            else template.arguments()["app_session_id"]
        )
        if type(route) is not str or not route:
            raise ExecutionAuthorityError("execution routing session is invalid")
        return cls(
            provider_id=provider_id,
            provider_kind=provider_kind,
            provider_generation=generation,
            provider_revision=revision,
            routing_session_id=route,
            template=template,
            _runtime_policy_json=_freeze_runtime_policy(runtime_policy),
            _provider_contract_json=_freeze_provider_contract(
                provider,
                provider_contract,
            ),
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ExecutionArtifact:
        expected = {
            "schema",
            "provider_id",
            "provider_kind",
            "provider_generation",
            "provider_revision",
            "routing_session_id",
            "template",
            "runtime_policy",
            "provider_contract",
            "fingerprint",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ExecutionAuthorityError("invalid execution artifact")
        if raw["schema"] != EXECUTION_SCHEMA or type(raw["template"]) is not dict:
            raise ExecutionAuthorityError("unsupported execution artifact")
        artifact = cls.create(
            {
                "id": raw["provider_id"],
                "kind": raw["provider_kind"],
                "generation": raw["provider_generation"],
                "revision": raw["provider_revision"],
            },
            ExecutionTemplate.from_dict(raw["template"]),
            routing_session_id=raw["routing_session_id"],
            runtime_policy=raw["runtime_policy"],
            provider_contract=raw["provider_contract"],
        )
        if type(raw["fingerprint"]) is not str or raw["fingerprint"] != artifact.fingerprint:
            raise ExecutionAuthorityError("execution artifact fingerprint mismatch")
        return artifact

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "schema": EXECUTION_SCHEMA,
            "provider_id": self.provider_id,
            "provider_kind": self.provider_kind,
            "provider_generation": self.provider_generation,
            "provider_revision": self.provider_revision,
            "routing_session_id": self.routing_session_id,
            "template": self.template.to_dict(),
            "runtime_policy": self.runtime_policy,
            "provider_contract": self.provider_contract,
        }
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload

    @property
    def fingerprint(self) -> str:
        payload = self.to_dict(include_fingerprint=False)
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def require_authority(self, provider: Mapping[str, Any]) -> None:
        expected = (
            self.provider_id,
            self.provider_kind,
            self.provider_generation,
            self.provider_revision,
        )
        actual = (
            provider.get("id"),
            provider.get("kind"),
            provider.get("generation"),
            provider.get("revision"),
        )
        if actual != expected:
            raise ExecutionAuthorityError(
                "provider authority changed after execution preparation",
            )

    @property
    def runtime_policy(self) -> dict[str, Any]:
        return _json_object(self._runtime_policy_json)

    @property
    def provider_contract(self) -> dict[str, Any] | None:
        if self._provider_contract_json is None:
            return None
        return _json_object(self._provider_contract_json)


@dataclass(frozen=True)
class PreparedExecution:
    artifact: ExecutionArtifact
    _volatile_json: str = field(repr=False)
    _admission: Future[bool] = field(
        default_factory=Future,
        compare=False,
        repr=False,
    )
    _cancel_after_admission: threading.Event = field(
        default_factory=threading.Event,
        compare=False,
        repr=False,
    )
    _spawn_completion: Future[bool] = field(
        default_factory=Future,
        compare=False,
        repr=False,
    )

    def start_arguments(self) -> dict[str, Any]:
        return {
            **self.artifact.template.arguments(),
            **_json_object(self._volatile_json),
        }

    def retry(self, **overrides: Any) -> PreparedExecution:
        arguments = self.start_arguments()
        arguments.update(overrides)
        template = ExecutionTemplate.create(arguments)
        artifact = ExecutionArtifact(
            provider_id=self.artifact.provider_id,
            provider_kind=self.artifact.provider_kind,
            provider_generation=self.artifact.provider_generation,
            provider_revision=self.artifact.provider_revision,
            routing_session_id=self.artifact.routing_session_id,
            template=template,
            _runtime_policy_json=self.artifact._runtime_policy_json,
            _provider_contract_json=self.artifact._provider_contract_json,
        )
        volatile = {
            key: arguments[key]
            for key in _VOLATILE_DEFAULTS
        }
        return PreparedExecution(artifact, _canonical_json(volatile))

    def wait_for_admission(self) -> bool:
        return self._admission.result()

    @property
    def admission_pending(self) -> bool:
        return not self._admission.done()

    @property
    def admission_cancelled(self) -> bool:
        return self._admission.done() and self._admission.exception() is None and not self._admission.result()

    def _try_commit_spawn(self) -> bool:
        if self._cancel_after_admission.is_set():
            self._resolve_admission(result=False)
            return False
        self._resolve_admission(result=True)
        return self._admission.exception() is None and self._admission.result()

    def _request_cancel_after_admission(self) -> None:
        self._cancel_after_admission.set()

    @property
    def cancel_after_admission_requested(self) -> bool:
        return self._cancel_after_admission.is_set()

    def _mark_spawn_completed(self) -> None:
        self._resolve_future(self._spawn_completion, result=True)

    def _mark_spawn_failed(self, error: BaseException) -> None:
        self._resolve_future(self._spawn_completion, error=error)

    def wait_for_spawn_completion(self) -> bool:
        return self._spawn_completion.result()

    def _mark_cancelled(self) -> None:
        self._resolve_admission(result=False)

    def _mark_admission_failed(self, error: BaseException) -> None:
        self._resolve_admission(error=error)

    def _resolve_admission(
        self,
        *,
        result: bool | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._resolve_future(self._admission, result=result, error=error)

    @staticmethod
    def _resolve_future(
        future: Future[bool],
        *,
        result: bool | None = None,
        error: BaseException | None = None,
    ) -> None:
        try:
            if error is None:
                future.set_result(bool(result))
            else:
                future.set_exception(error)
        except InvalidStateError:
            pass


def prepare_execution(
    provider: Mapping[str, Any],
    *,
    routing_session_id: str | None = None,
    runtime_policy: Mapping[str, Any] | None = None,
    provider_contract: Mapping[str, Any] | None = None,
    **start_arguments: Any,
) -> PreparedExecution:
    from codex_execution_common import timed_contract_step

    with timed_contract_step("provider.execution.prepare"):
        return _prepare_execution(
            provider,
            routing_session_id=routing_session_id,
            runtime_policy=runtime_policy,
            provider_contract=provider_contract,
            **start_arguments,
        )


def _prepare_execution(
    provider: Mapping[str, Any],
    *,
    routing_session_id: str | None = None,
    runtime_policy: Mapping[str, Any] | None = None,
    provider_contract: Mapping[str, Any] | None = None,
    **start_arguments: Any,
) -> PreparedExecution:
    normalized = _normalize_arguments(start_arguments)
    template = ExecutionTemplate.create(normalized)
    frozen_runtime_policy = dict(runtime_policy or {})
    normalized_provider_contract = (
        _json_object(_freeze_provider_contract(provider, provider_contract))
        if provider_contract is not None
        else None
    )
    if normalized_provider_contract is not None:
        from model_execution_admission import (
            issue_model_admission,
            selected_model_from_policy,
        )

        frozen_runtime_policy.pop("model_admission", None)
        frozen_runtime_policy["model_admission"] = issue_model_admission(
            provider=provider,
            selected_model=selected_model_from_policy(
                frozen_runtime_policy,
                normalized["model"],
            ),
            provider_contract=normalized_provider_contract,
        )
    volatile = {
        key: normalized[key]
        for key in _VOLATILE_DEFAULTS
    }
    return PreparedExecution(
        ExecutionArtifact.create(
            provider,
            template,
            routing_session_id=routing_session_id,
            runtime_policy=frozen_runtime_policy,
            provider_contract=normalized_provider_contract,
        ),
        _canonical_json(volatile),
    )


def restore_prepared_execution(
    artifact: ExecutionArtifact,
    **volatile_arguments: Any,
) -> PreparedExecution:
    unknown = set(volatile_arguments) - set(_VOLATILE_DEFAULTS)
    if unknown:
        raise ValueError("invalid volatile execution arguments")
    volatile = {**_VOLATILE_DEFAULTS, **volatile_arguments}
    _normalize_arguments({
        **artifact.template.arguments(),
        **volatile,
    })
    return PreparedExecution(artifact, _canonical_json(volatile))


def _legacy_value_matches(key: str, expected: Any, actual: Any) -> bool:
    if key == "mode":
        canonical = lambda value: "manager" if value in ("manager", "team") else value
        return canonical(expected) == canonical(actual)
    if expected is None:
        if key in (
            "images",
            "files",
            "disallowed_tools",
            "setting_sources",
            "continuation_chain",
            "disabled_builtin_extensions",
            "capability_contexts",
        ):
            return actual in (None, [])
        if key in (
            "model",
            "reasoning_effort",
            "session_id",
            "source",
            "backend_url",
            "supervisor_agent_session_id",
            "worker_agent_session_id",
            "mssg_sender_session_id",
            "working_mode",
            "target_message_id",
            "turn_run_id",
        ):
            return actual in (None, "")
    return actual == expected


def validate_recovery_input(
    artifact: ExecutionArtifact,
    legacy_input: Mapping[str, Any],
) -> None:
    if type(legacy_input) is not dict:
        raise ExecutionAuthorityError("recovered run input must be an object")
    expected = artifact.template.arguments()
    for key, value in expected.items():
        if key not in legacy_input:
            continue
        if not _legacy_value_matches(key, value, legacy_input[key]):
            raise ExecutionAuthorityError(
                f"recovered run input conflicts with execution authority: {key}",
            )
    for key, expected_value in (
        ("provider_id", artifact.provider_id),
        ("provider_kind", artifact.provider_kind),
    ):
        if key in legacy_input and legacy_input[key] != expected_value:
            raise ExecutionAuthorityError(
                f"recovered run input conflicts with execution authority: {key}",
            )


def validate_recovery_sessions(
    artifact: ExecutionArtifact,
    *,
    routing_session_id: str,
    persist_session_id: str,
) -> None:
    if artifact.routing_session_id != routing_session_id:
        raise ExecutionAuthorityError(
            "recovered run routing session conflicts with execution authority",
        )
    execution_session_id = artifact.template.arguments()["app_session_id"]
    if (
        artifact.routing_session_id != execution_session_id
        and execution_session_id != persist_session_id
    ):
        raise ExecutionAuthorityError(
            "recovered run persistence session conflicts with execution authority",
        )
