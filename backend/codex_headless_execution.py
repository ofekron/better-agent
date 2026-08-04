from __future__ import annotations

import os
from dataclasses import dataclass
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


def _admitted_authority_tuple(
    admitted: AdmittedHeadlessRequest,
) -> tuple[Any, ...]:
    authority = admitted.authority
    return (
        authority.provider_id,
        authority.provider_kind,
        authority.provider_generation,
        authority.provider_execution_revision,
    )


@dataclass(frozen=True)
class PreparedCodexHeadless:
    admitted: AdmittedHeadlessRequest
    contract: CodexExecutionContract

    def __post_init__(self) -> None:
        if not isinstance(self.admitted, AdmittedHeadlessRequest):
            raise TypeError("admitted headless request is invalid")
        if not isinstance(self.contract, CodexExecutionContract):
            raise TypeError("Codex execution contract is invalid")
        actual = (
            self.contract.provider_id,
            self.contract.provider_kind,
            self.contract.provider_generation,
            self.contract.provider_execution_revision,
        )
        if actual != _admitted_authority_tuple(self.admitted):
            raise ValueError("headless execution authority conflicts")

    @classmethod
    def create(
        cls,
        admitted: AdmittedHeadlessRequest,
        contract: CodexExecutionContract,
    ) -> PreparedCodexHeadless:
        return cls(admitted=admitted, contract=contract)

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted.to_dict(),
            "contract": self.contract.to_dict(),
        }

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
        record.get("execution_revision"),
    )


def prepare_codex_headless(
    provider: Any,
    admitted: AdmittedHeadlessRequest,
    *,
    launcher_path: str | None = None,
) -> PreparedCodexHeadless:
    if not isinstance(admitted, AdmittedHeadlessRequest):
        raise TypeError("admitted headless request is invalid")
    request = admitted.request
    authority = admitted.authority
    record = dict(provider.record)
    if (
        authority.provider_kind not in {"codex", "fugu"}
        or provider.KIND != authority.provider_kind
        or _authority_tuple(record) != _admitted_authority_tuple(admitted)
    ):
        raise ValueError("headless provider authority conflicts")
    expected_runner = str(record.get("runner") or "native")
    if authority.runner != expected_runner:
        raise ValueError("headless runner conflicts")
    if request.no_tools and not provider.supports_headless_no_tools:
        raise ValueError("headless provider cannot guarantee no-tools")
    if request.fork and not provider.supports_fork:
        raise ValueError("headless provider cannot fork")
    if authority.reasoning_effort not in provider.reasoning_effort_options:
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
    overrides = list(provider.codex_config_overrides(model=authority.model))
    if not any(value.startswith("model=") for value in overrides):
        overrides.append(f"model={toml_literal(authority.model)}")
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
    admitted = prepared.admitted
    request = admitted.request
    authority = admitted.authority
    if (
        provider.KIND != prepared.contract.provider_kind
        or _authority_tuple(runtime_record)
        != _admitted_authority_tuple(admitted)
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
                prompt=request.prompt,
                cwd=authority.cwd,
                model=authority.model,
                reasoning_effort=authority.reasoning_effort,
                session_id=authority.resume_sid,
                fork=request.fork,
                timeout=request.timeout,
                profile=prepared.contract.profile or None,
                config_overrides=overrides,
                env=env,
            )
    except ExecutionContractError as exc:
        raise RuntimeError("headless execution authority changed") from exc
