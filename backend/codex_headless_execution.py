from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from codex_execution_common import ExecutionContractError
from codex_execution_contract import (
    CodexExecutionContract,
    build_codex_execution_contract,
)
from codex_execution_runtime import codex_authority_paths
from codex_execution_launch import pinned_launch
from headless_request_contract import AdmittedHeadlessRequest
from provider_run_config import toml_literal


@dataclass(frozen=True)
class PreparedCodexHeadless:
    _admitted_json: str = field(repr=False)
    contract: CodexExecutionContract

    @classmethod
    def create(
        cls,
        admitted: AdmittedHeadlessRequest,
        contract: CodexExecutionContract,
    ) -> PreparedCodexHeadless:
        admitted_payload = admitted.to_dict()
        provider = admitted_payload["provider"]
        expected = (
            provider["id"],
            provider["kind"],
            provider["generation"],
            provider["revision"],
        )
        actual = (
            contract.provider_id,
            contract.provider_kind,
            contract.provider_generation,
            contract.provider_revision,
        )
        if actual != expected:
            raise ValueError("headless execution authority conflicts")
        return cls(
            json.dumps(
                admitted_payload,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            contract,
        )

    @property
    def admitted(self) -> AdmittedHeadlessRequest:
        raw = json.loads(self._admitted_json)
        return AdmittedHeadlessRequest.from_dict(raw)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps({
            "admitted": self.admitted.to_dict(),
            "contract": self.contract.to_dict(),
        }, allow_nan=False, separators=(",", ":"), sort_keys=True))

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
    ) -> PreparedCodexHeadless:
        if type(raw) is not dict or set(raw) != {"admitted", "contract"}:
            raise ValueError("invalid prepared headless execution")
        admitted = AdmittedHeadlessRequest.from_dict(raw["admitted"])
        contract = CodexExecutionContract.from_dict(raw["contract"])
        return cls.create(admitted, contract)


def _authority_tuple(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("id"),
        record.get("kind"),
        record.get("generation"),
        record.get("revision"),
    )


def _require_model(provider: Mapping[str, Any], model: str) -> None:
    if provider.get("kind") == "fugu":
        # Fugu has no verifiable CLI catalog; the curated constant is
        # its catalog authority (see codex_model_discovery).
        from provider_fugu import FUGU_MODELS

        if model not in FUGU_MODELS:
            raise ValueError(
                "headless model is not in the current provider catalog",
            )
        return
    import model_catalog_read_projection

    projection = model_catalog_read_projection.snapshot(
        str(provider["id"]),
        str(provider["generation"]),
    )
    if (
        projection is None
        or not projection.models_current
        or model not in projection.models
    ):
        raise ValueError("headless model is not in the current provider catalog")


def prepare_codex_headless(
    provider: Any,
    admitted: AdmittedHeadlessRequest,
    *,
    launcher_path: str | None = None,
) -> PreparedCodexHeadless:
    admitted = AdmittedHeadlessRequest.from_dict(admitted.to_dict())
    payload = admitted.to_dict()
    authority = payload["provider"]
    record = dict(provider.record)
    if (
        authority["kind"] not in {"codex", "fugu"}
        or provider.KIND != authority["kind"]
        or _authority_tuple(record) != _authority_tuple(authority)
    ):
        raise ValueError("headless provider authority conflicts")
    runtime_profile = str(record.get("runner") or "native")
    if payload["runtime_profile"] != runtime_profile:
        raise ValueError("headless runtime profile conflicts")
    if payload["no_tools"] and not provider.supports_headless_no_tools:
        raise ValueError("headless provider cannot guarantee no-tools")
    if payload["fork"] and not provider.supports_fork:
        raise ValueError("headless provider cannot fork")
    _require_model(authority, payload["model"])
    if payload["reasoning_effort"] not in provider.reasoning_effort_options:
        raise ValueError("headless reasoning effort is unsupported")

    from cli_paths import resolve_cli_binary
    from paths import resolve_provider_config_dir, user_home

    launcher = launcher_path or resolve_cli_binary(provider.CODEX_BINARY)
    if not launcher:
        raise ValueError("headless Codex launcher is unavailable")
    config_dir_raw = str(record.get("config_dir") or "").strip()
    config_root = (
        resolve_provider_config_dir(config_dir_raw)
        if config_dir_raw
        else user_home() / ".codex"
    ).resolve(strict=True)
    overrides = list(provider.codex_config_overrides(model=payload["model"]))
    if not any(value.startswith("model=") for value in overrides):
        overrides.append(f"model={toml_literal(payload['model'])}")
    contract = build_codex_execution_contract(
        {**record, "config_dir": str(config_root)},
        launcher_path=launcher,
        profile=provider.CODEX_PROFILE,
        runtime_args=tuple(
            part
            for assignment in overrides
            for part in ("-c", assignment)
        ),
        environment_selectors={"CODEX_HOME": str(config_root)},
        config_paths=codex_authority_paths(config_root),
        search_path=os.environ.get("PATH"),
    )
    return PreparedCodexHeadless.create(admitted, contract)


async def execute_codex_headless(
    provider: Any,
    prepared: PreparedCodexHeadless,
    *,
    runtime_record: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(prepared, PreparedCodexHeadless):
        raise TypeError("prepared headless execution is invalid")
    prepared = PreparedCodexHeadless.from_dict(prepared.to_dict())
    admitted = prepared.admitted
    payload = admitted.to_dict()
    if (
        provider.KIND != prepared.contract.provider_kind
        or _authority_tuple(runtime_record)
        != _authority_tuple(payload["provider"])
        or not prepared.contract.attest()
    ):
        raise RuntimeError("headless execution authority changed")
    installed = getattr(provider._execution_record, "value", None)
    provider._execution_record.value = dict(runtime_record)
    try:
        env = provider.build_env()
    finally:
        if installed is None:
            del provider._execution_record.value
        else:
            provider._execution_record.value = installed
    env.update(dict(prepared.contract.environment_selectors))
    overrides = list(prepared.contract.runtime_args[1::2])
    from runner_codex import run_headless_app_server

    try:
        with pinned_launch(prepared.contract.launch_chain) as launch:
            return await run_headless_app_server(
                launch.argv_prefix,
                pass_fds=launch.pass_fds,
                prompt=payload["prompt"],
                cwd=payload["cwd"],
                model=payload["model"],
                reasoning_effort=payload["reasoning_effort"],
                session_id=payload["resume_sid"],
                fork=payload["fork"],
                timeout=payload["timeout"],
                profile=prepared.contract.profile or None,
                config_overrides=overrides,
                env=env,
            )
    except ExecutionContractError as exc:
        raise RuntimeError("headless execution authority changed") from exc
