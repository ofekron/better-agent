from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, Mapping, Sequence

import config_store
from cli_paths import resolve_cli_binary
from codex_execution import (
    CodexExecutionContract,
    ExecutionContractError,
    build_codex_execution_contract,
)
from codex_model_discovery_format import (
    CatalogOutputError,
    parse_models,
)
from codex_model_discovery_process import (
    CommandCancellation,
    child_environment,
    run_catalog_command,
)
from model_catalog_authority import (
    CatalogAuthority,
    CatalogAuthorityError,
    build_catalog_authority,
)
from model_catalog_cache import build_catalog_snapshot, write_catalog_cache
from paths import resolve_provider_config_dir, user_home


DiscoveryStatus = Literal[
    "ok",
    "empty",
    "unavailable",
    "unsupported",
    "error",
]

DISCOVERY_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_TIMEOUT_SECONDS = 30.0
class CatalogDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscoveryResult:
    status: DiscoveryStatus
    reason: str
    models: tuple[str, ...] = ()
    authority: CatalogAuthority | None = None

    def __post_init__(self) -> None:
        if (
            self.status not in {
                "ok",
                "empty",
                "unavailable",
                "unsupported",
                "error",
            }
            or type(self.reason) is not str
            or type(self.models) is not tuple
            or any(type(model) is not str for model in self.models)
        ):
            raise CatalogDiscoveryError("invalid discovery result")
        authoritative = (
            self.status == "ok"
            and not self.reason
            and bool(self.models)
            and type(self.authority) is CatalogAuthority
        )
        if authoritative:
            return
        if (
            not self.reason
            or self.models
            or self.authority is not None
            or self.status == "ok"
        ):
            raise CatalogDiscoveryError("invalid discovery result")


@dataclass(frozen=True)
class _ProviderSnapshot:
    provider: dict
    state_authority: dict

    @property
    def identity(self) -> tuple[str, int, str, int, str]:
        return (
            self.provider["generation"],
            self.provider["revision"],
            self.state_authority["generation"],
            self.state_authority["revision"],
            self.state_authority["digest"],
        )


@dataclass(frozen=True)
class _DiscoveryContext:
    snapshot: _ProviderSnapshot
    contract: CodexExecutionContract
    authority: CatalogAuthority
    environment: dict[str, str] = field(repr=False)
    argv: tuple[str, ...]


class _PreparationFailure(RuntimeError):
    def __init__(self, status: DiscoveryStatus, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(reason)


def _provider_snapshot(provider_id: str) -> _ProviderSnapshot:
    state = config_store.list_providers()
    provider = next(
        (
            record
            for record in state.get("providers", [])
            if record.get("id") == provider_id
        ),
        None,
    )
    if provider is None:
        raise _PreparationFailure("unavailable", "provider_unavailable")
    if provider.get("suspended") is True:
        raise _PreparationFailure("unavailable", "provider_suspended")
    state_authority = state.get("provider_state_authority")
    if type(state_authority) is not dict:
        raise _PreparationFailure("error", "provider_authority_invalid")
    return _ProviderSnapshot(
        provider=dict(provider),
        state_authority=dict(state_authority),
    )


def _catalog_semantics(provider: Mapping[str, object]) -> tuple[
    tuple[str, ...],
    str,
]:
    kind = provider.get("kind")
    runner = provider.get("runner")
    if kind not in {"codex", "fugu"}:
        raise _PreparationFailure("unsupported", "provider_unsupported")
    if runner != "native":
        raise _PreparationFailure("unsupported", "runner_unsupported")
    if kind == "fugu":
        raise _PreparationFailure(
            "unsupported",
            "fugu_catalog_unverifiable",
        )
    return (), "codex.debug_models"


def _effective_codex_home(provider: Mapping[str, object]) -> Path:
    selected = config_store.provider_credential_env(dict(provider))
    if selected is not None:
        if selected[0] != "CODEX_HOME":
            raise _PreparationFailure("error", "account_selector_invalid")
        root = Path(selected[1])
    else:
        ambient = os.environ.get("CODEX_HOME", "").strip()
        root = (
            resolve_provider_config_dir(ambient)
            if ambient
            else user_home() / ".codex"
        )
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise _PreparationFailure(
            "unavailable",
            "config_unavailable",
        ) from exc


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise _PreparationFailure("error", "timeout")


def _build_context(
    provider_id: str,
    *,
    deadline: float | None = None,
) -> _DiscoveryContext:
    _check_deadline(deadline)
    snapshot = _provider_snapshot(provider_id)
    _check_deadline(deadline)
    catalog_args, method = _catalog_semantics(snapshot.provider)
    executable = resolve_cli_binary("codex")
    _check_deadline(deadline)
    if not executable:
        raise _PreparationFailure("unavailable", "executable_unavailable")
    codex_home = _effective_codex_home(snapshot.provider)
    _check_deadline(deadline)
    provider_for_contract = {
        **snapshot.provider,
        "config_dir": str(codex_home),
    }
    try:
        contract = build_codex_execution_contract(
            provider_for_contract,
            launcher_path=executable,
            catalog_args=catalog_args,
            environment_selectors={"CODEX_HOME": str(codex_home)},
            search_path=os.environ.get("PATH"),
        )
        authority = build_catalog_authority(
            provider_id=snapshot.provider["id"],
            provider_generation=snapshot.provider["generation"],
            provider_revision=snapshot.provider["revision"],
            provider_state_authority=snapshot.state_authority,
            execution_contract=contract,
            discovery_method=method,
            discovery_version=DISCOVERY_VERSION,
        )
    except (CatalogAuthorityError, ExecutionContractError, ValueError) as exc:
        raise _PreparationFailure("unavailable", "source_unavailable") from exc
    _check_deadline(deadline)
    profile_args = ("-p", contract.profile) if contract.profile else ()
    argv = (
        *contract.launch_chain.argv_prefix,
        *profile_args,
        *contract.catalog_args,
        "debug",
        "models",
    )
    return _DiscoveryContext(
        snapshot=snapshot,
        contract=contract,
        authority=authority,
        environment=child_environment(codex_home),
        argv=argv,
    )


def _refresh_context(
    original: _DiscoveryContext,
    *,
    deadline: float | None = None,
) -> _DiscoveryContext | None:
    _check_deadline(deadline)
    if not original.contract.attest():
        return None
    _check_deadline(deadline)
    try:
        current = _build_context(
            original.snapshot.provider["id"],
            deadline=deadline,
        )
    except _PreparationFailure as exc:
        if exc.reason == "timeout":
            raise
        return None
    if (
        current.snapshot.identity == original.snapshot.identity
        and current.contract.catalog_fingerprint
        == original.contract.catalog_fingerprint
    ):
        return current
    return None


def _parent_stabilization_context(
    original: _DiscoveryContext,
    *,
    deadline: float | None = None,
) -> _DiscoveryContext | None:
    _check_deadline(deadline)
    try:
        current = _build_context(
            original.snapshot.provider["id"],
            deadline=deadline,
        )
    except _PreparationFailure as exc:
        if exc.reason == "timeout":
            raise
        return None
    if (
        current.snapshot.identity != original.snapshot.identity
        or len(current.contract.config) != len(original.contract.config)
    ):
        return None
    normalized_config = []
    for previous, candidate in zip(
        original.contract.config,
        current.contract.config,
        strict=True,
    ):
        normalized_config.append(
            replace(
                candidate,
                parent_size=previous.parent_size,
                parent_mtime_ns=previous.parent_mtime_ns,
                parent_ctime_ns=previous.parent_ctime_ns,
            ),
        )
    if replace(
        current.contract,
        config=tuple(normalized_config),
    ) != original.contract:
        return None
    _check_deadline(deadline)
    return current


def _discover_sync(
    provider_id: str,
    timeout_seconds: float,
    cancellation: CommandCancellation,
) -> DiscoveryResult:
    deadline = time.monotonic() + timeout_seconds
    if cancellation.cancelled:
        return DiscoveryResult(status="error", reason="cancelled")
    try:
        context = _build_context(provider_id, deadline=deadline)
    except _PreparationFailure as exc:
        return DiscoveryResult(status=exc.status, reason=exc.reason)
    if not context.contract.attest():
        return DiscoveryResult(status="error", reason="authority_changed")
    if cancellation.cancelled:
        return DiscoveryResult(status="error", reason="cancelled")
    try:
        context = _refresh_context(context, deadline=deadline)
    except _PreparationFailure as exc:
        return DiscoveryResult(status=exc.status, reason=exc.reason)
    if context is None:
        return DiscoveryResult(status="error", reason="authority_changed")
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return DiscoveryResult(status="error", reason="timeout")
        command = run_catalog_command(
            context.argv,
            context.environment,
            remaining,
            cancellation,
        )
        if command.reason:
            return DiscoveryResult(status="error", reason=command.reason)
        try:
            refreshed = _refresh_context(context, deadline=deadline)
        except _PreparationFailure as exc:
            return DiscoveryResult(status=exc.status, reason=exc.reason)
        if refreshed is not None:
            context = refreshed
            break
        if attempt or cancellation.cancelled:
            return DiscoveryResult(
                status="error",
                reason=(
                    "cancelled"
                    if cancellation.cancelled
                    else "authority_changed"
                ),
            )
        try:
            context = _parent_stabilization_context(
                context,
                deadline=deadline,
            )
        except _PreparationFailure as exc:
            return DiscoveryResult(status=exc.status, reason=exc.reason)
        if context is None:
            return DiscoveryResult(
                status="error",
                reason="authority_changed",
            )
    else:
        return DiscoveryResult(status="error", reason="authority_changed")
    try:
        models = parse_models(command.output)
    except CatalogOutputError:
        return DiscoveryResult(status="error", reason="invalid_output")
    if not models:
        return DiscoveryResult(status="empty", reason="empty_catalog")
    return DiscoveryResult(
        status="ok",
        reason="",
        models=models,
        authority=context.authority,
    )


async def discover_models(
    provider_id: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> DiscoveryResult:
    if (
        type(provider_id) is not str
        or not provider_id
        or type(timeout_seconds) not in {int, float}
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > MAX_TIMEOUT_SECONDS
    ):
        raise CatalogDiscoveryError("invalid discovery request")
    cancellation = CommandCancellation()
    worker = asyncio.create_task(
        asyncio.to_thread(
            _discover_sync,
            provider_id,
            float(timeout_seconds),
            cancellation,
        ),
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancellation.cancel()
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            pass
        raise
    except Exception:
        return DiscoveryResult(status="error", reason="internal_error")


def write_discovery_cache(
    path: Path,
    result: DiscoveryResult,
    *,
    retired: Sequence[Mapping[str, object]],
    refreshed_at: float,
) -> bool:
    if (
        not isinstance(path, Path)
        or type(result) is not DiscoveryResult
        or result.status != "ok"
        or result.authority is None
    ):
        raise CatalogDiscoveryError("discovery result is not authoritative")
    with config_store.provider_state_read_transaction():
        try:
            current = _build_context(result.authority.provider_id)
        except _PreparationFailure as exc:
            raise CatalogDiscoveryError(
                "discovery authority is no longer current",
            ) from exc
        if current.authority != result.authority:
            raise CatalogDiscoveryError(
                "discovery authority is no longer current",
            )
        snapshot = build_catalog_snapshot(
            authority=result.authority,
            models=result.models,
            retired=retired,
            last_refreshed_at=refreshed_at,
            last_fetch_state="ok",
        )
        return write_catalog_cache(path, snapshot)
