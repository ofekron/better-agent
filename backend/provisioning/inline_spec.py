from __future__ import annotations

import math
import os
import re
from pathlib import Path

from provisioning.config import ProvisionedConfig, provider_supports_fork
from provisioning.spec import DirtyPolicy, ProvisionedSessionSpec


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ALLOWED_FIELDS = {
    "key",
    "version",
    "name",
    "task_key",
    "provision_prompt",
    "instructions",
    "node_id",
    "provision_timeout",
    "retry_attempts",
    "dirty_policy",
    "lifetime_seconds",
}
_ALLOWED_DIRTY_FIELDS = {
    "max_base_bytes",
    "max_user_turns",
    "max_assistant_turns",
    "leak_markers",
}
_MAX_PROMPT_CHARS = 2_000_000


class InlineProvisionedSessionSpec(ProvisionedSessionSpec):
    def __init__(
        self,
        *,
        key: str,
        version: int,
        name: str,
        task_key: str,
        provision_prompt: str,
        instructions: str,
        node_id: str,
        provision_timeout: float,
        retry_attempts: int,
        dirty_policy: DirtyPolicy,
        lifetime_seconds: float | None,
    ) -> None:
        self.key = key
        self.version = version
        self.name = name
        self.env_prefix = "INLINE_PROVISIONED"
        self.task_key = task_key
        self.orchestration_mode = "native"
        self.bare_config = True
        self.worker_creation_policy = "deny"
        self.machine_completion = True
        self.run_mode = "fork"
        self.ephemeral_forks = True
        self.dispatch = "in_process"
        self.on_no_fork = "error"
        self.default_model = ""
        self.default_cwd = ""
        self.node_id = node_id
        self.dirty_policy = dirty_policy
        self.lifetime_seconds = lifetime_seconds
        self.provision_timeout = provision_timeout
        self.retry_attempts = retry_attempts
        self.retry_backoff = (2.0, 8.0)
        self._provision_prompt = provision_prompt
        self._instructions = instructions

    def build_provision_prompt(self, ctx: dict) -> str:
        return self._provision_prompt

    def build_instructions(self, query: str, ctx: dict) -> str:
        return self._instructions

    def parse_result(self, text: str, ctx: dict) -> str:
        return text

    def build_config(self, *, model: str | None = None) -> ProvisionedConfig | None:
        if model:
            raise RuntimeError("inline provisioned sessions do not accept per-call model overrides")
        import config_store

        resolved = config_store.resolve_internal_llm(self.task_key)
        provider_id = str(resolved.get("provider_id") or "").strip()
        selected_model = str(resolved.get("model") or "").strip()
        if not provider_id or not selected_model:
            raise RuntimeError(f"inline provisioned task {self.task_key!r} has no model configured")
        if not provider_supports_fork(provider_id):
            raise RuntimeError(
                f"inline provisioned task {self.task_key!r} requires a fork-capable provider"
            )
        return ProvisionedConfig(
            cwd=str(Path(os.getcwd()).expanduser().resolve()),
            model=selected_model,
            provider_id=provider_id,
            reasoning_effort=str(resolved.get("reasoning_effort") or ""),
            run_mode="fork",
            dispatch="in_process",
            on_no_fork="error",
            node_id=self.node_id,
            backend_url="",
            internal_token="",
            provisioned_session_id=None,
            caller_session_id=None,
            worker_description=self.name,
        )


def inline_spec_from_payload(
    payload: object,
    *,
    extension_id: str,
    allowed_task_keys: set[str],
) -> InlineProvisionedSessionSpec:
    if not isinstance(payload, dict):
        raise ValueError("inline_spec must be an object")
    unknown = sorted(str(k) for k in payload if k not in _ALLOWED_FIELDS)
    if unknown:
        raise ValueError(f"inline_spec has unsupported fields: {', '.join(unknown)}")

    if not _ID_RE.fullmatch(extension_id):
        raise ValueError("extension id is invalid")
    raw_key = _required_text(payload, "key", max_chars=80, pattern=_KEY_RE)
    task_key = _required_text(payload, "task_key", max_chars=128, pattern=_TASK_RE)
    if task_key not in allowed_task_keys:
        raise ValueError("inline_spec task_key is not declared by the calling extension")
    node_id = _optional_text(payload, "node_id", default="primary", max_chars=32)
    if node_id != "primary":
        raise ValueError("inline_spec node_id must be primary")
    return InlineProvisionedSessionSpec(
        key=f"extension:{extension_id}:{raw_key}",
        version=_int_field(payload, "version", default=1, min_value=1, max_value=1_000_000),
        name=_required_text(payload, "name", max_chars=120),
        task_key=task_key,
        provision_prompt=_required_text(
            payload, "provision_prompt", max_chars=_MAX_PROMPT_CHARS
        ),
        instructions=_required_text(payload, "instructions", max_chars=_MAX_PROMPT_CHARS),
        node_id=node_id,
        provision_timeout=_float_field(
            payload, "provision_timeout", default=900.0, min_value=1.0, max_value=3600.0
        ),
        retry_attempts=_int_field(payload, "retry_attempts", default=3, min_value=1, max_value=3),
        dirty_policy=_dirty_policy(payload.get("dirty_policy")),
        lifetime_seconds=_optional_float_field(
            payload, "lifetime_seconds", min_value=1.0, max_value=86_400.0
        ),
    )


def _required_text(
    payload: dict,
    field: str,
    *,
    max_chars: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"inline_spec {field} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"inline_spec {field} is required")
    if len(text) > max_chars:
        raise ValueError(f"inline_spec {field} is too long")
    if pattern is not None and not pattern.fullmatch(text):
        raise ValueError(f"inline_spec {field} has invalid characters")
    return text


def _optional_text(payload: dict, field: str, *, default: str, max_chars: int) -> str:
    if field not in payload:
        return default
    return _required_text(payload, field, max_chars=max_chars)


def _int_field(
    payload: dict,
    field: str,
    *,
    default: int,
    min_value: int,
    max_value: int,
) -> int:
    value = payload.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"inline_spec {field} must be an integer")
    if value < min_value or value > max_value:
        raise ValueError(f"inline_spec {field} is out of range")
    return value


def _float_field(
    payload: dict,
    field: str,
    *,
    default: float,
    min_value: float,
    max_value: float,
) -> float:
    value = payload.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"inline_spec {field} must be a number")
    out = float(value)
    if not math.isfinite(out) or out < min_value or out > max_value:
        raise ValueError(f"inline_spec {field} is out of range")
    return out


def _optional_float_field(
    payload: dict,
    field: str,
    *,
    min_value: float,
    max_value: float,
) -> float | None:
    if field not in payload or payload.get(field) is None:
        return None
    return _float_field(payload, field, default=min_value, min_value=min_value, max_value=max_value)


def _dirty_policy(value: object) -> DirtyPolicy:
    if value is None:
        return DirtyPolicy()
    if not isinstance(value, dict):
        raise ValueError("inline_spec dirty_policy must be an object")
    unknown = sorted(str(k) for k in value if k not in _ALLOWED_DIRTY_FIELDS)
    if unknown:
        raise ValueError(f"inline_spec dirty_policy has unsupported fields: {', '.join(unknown)}")
    return DirtyPolicy(
        max_base_bytes=_int_field(
            value, "max_base_bytes", default=256_000, min_value=1_000, max_value=10_000_000
        ),
        max_user_turns=_optional_int_field(
            value, "max_user_turns", default=1, min_value=0, max_value=20
        ),
        max_assistant_turns=_optional_int_field(
            value, "max_assistant_turns", default=1, min_value=0, max_value=20
        ),
        leak_markers=_leak_markers(value.get("leak_markers", ())),
    )


def _optional_int_field(
    payload: dict,
    field: str,
    *,
    default: int,
    min_value: int,
    max_value: int,
) -> int | None:
    if field not in payload:
        return default
    if payload.get(field) is None:
        return None
    return _int_field(payload, field, default=default, min_value=min_value, max_value=max_value)


def _leak_markers(value: object) -> tuple[str, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("inline_spec dirty_policy leak_markers must be a list")
    markers: list[str] = []
    for marker in value:
        if not isinstance(marker, str):
            raise ValueError("inline_spec dirty_policy leak_markers must contain strings")
        text = marker.strip()
        if not text or len(text) > 500:
            raise ValueError("inline_spec dirty_policy leak_markers contains an invalid marker")
        markers.append(text)
    return tuple(markers)
