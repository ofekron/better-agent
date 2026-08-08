"""Concrete ProviderConfigSurface implementation (ADR 0007).

Reads exclusively through `backend.adapters.store_access.store_access`.
Every read-plane method on this surface returns a plain value (never a
`ProjectionResult`) per the ABC — ADR 0007's "settings render entirely
from descriptors" model has no rebuilding/stale-cursor state to project.

Live plane (D1): `bind()` subscribes to the per-concern bus facts
`backend/providers_api.py` (and `backend/runtime_profiles_api.py`) publish
at their existing mutation call sites — `provider.mutated`,
`provider.credential_state_changed`, `provider.model_catalog_changed`,
`provider.login_flow_changed` — and re-derives the affected read model
through `store_access` on receipt (the same "re-pull and re-map" idiom
`backend/adapters/session_adapter.py` uses), except for the login-flow
fact, whose source (provider_auth's transient in-flight state) has no
store_access equivalent and so carries its full projected payload
directly. See those two producer modules' docstrings for the exact fact
vocabulary and dedup/change-only guarantees.

Command plane (D4): `submit()` mirrors `ChatSurfaceAdapter.submit()`'s
duck-typed `self._command_port` idiom (see `backend/adapters/chat_adapter.
py`) — a well-formed, currently-supported intent is admitted synchronously
and its port coroutine scheduled fire-and-forget; the real outcome
surfaces later as a projection fact over the live plane above, per ADR
0006 §5 ("acks are projection facts"). The concrete port
(`backend.adapter_api.ProviderCommandPort`) is composed and injected by
`backend/adapter_api.py`'s `configure()` — `adapter_api.py` is exempt from
this module's own import boundary (see `backend/scripts/test_adapter_
boundaries.py`'s `_external_adapters_import_violations`), so it is the
sanctioned home for a port implementation that needs to call
`config_store`/`providers_api.py` mutation logic, which this file may not
import directly.

Every field this adapter cannot honestly derive from
`StoreAccess`'s current surface is called out inline as a `# gap:`
comment rather than guessed at (CLAUDE.md: "never invent"); the caller's
report enumerates them.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

from backend.adapters.projection import BusBoundProjection
from backend.adapters.store_access import ProviderRecord, store_access
from backend.event_bus import BusEvent
from backend.surface_contract.descriptors import (
    AuthFlow,
    Capabilities,
    CatalogModel,
    ConfigState,
    CredentialState,
    DeletedProviderRef,
    Display,
    FormField,
    FormFieldKind,
    InstallableDescriptor,
    InstallLine,
    InstallProgressChanged,
    InstallRun,
    InstallState,
    LoginFlowFrame,
    LoginFlowState,
    LoginPhase,
    ModelCatalog,
    ModelCatalogChanged,
    ProviderDescriptor,
    ProviderUpsert,
    RuntimeProfile,
    RuntimeProfilesChanged,
    RuntimeProfilesSnapshot,
)
from backend.surface_contract.identity import Emit, ProviderId, Subscription
from backend.surface_contract.intents import (
    BeginLogin,
    CancelLogin,
    CreateProvider,
    DeleteProvider,
    DeleteRuntimeProfile,
    IntentAccepted,
    IntentRejected,
    ProviderIntent,
    RefreshModels,
    RetryCredential,
    SaveRuntimeProfile,
    SuspendProvider,
    TransportAck,
    UpdateProvider,
)
from backend.surface_contract.nodes import SendMode
from backend.surface_contract.provider_config_surface import ProviderConfigSurface


class _SubscriptionImpl:
    def __init__(self, close_fn) -> None:
        self._close_fn = close_fn

    def close(self) -> None:
        self._close_fn()


def _capabilities(record: ProviderRecord) -> Capabilities:
    caps = record.capabilities
    return Capabilities(
        fork=caps.get("supports_fork", False),
        manager_mode=caps.get("supports_manager_mode", False),
        rewind=caps.get("supports_rewind", False),
        steering=caps.get("supports_steering", False),
        native_subagents=caps.get("supports_native_subagents", False),
        reasoning_effort=caps.get("supports_reasoning_effort", False),
        # config_store's capability matrix (`_CAPABILITY_KEYS`) never
        # carries these two keys — default False when absent, per spec.
        usage_reporting=caps.get("supports_usage_reporting", False),
        startup_monitoring=caps.get("supports_startup_monitoring", False),
    )


# config_store.provider_credential_status's exhaustive return set
# (backend/credential_session_client.py's `CredentialStatus` Literal) is
# {"available", "missing", "blocked", "unknown"} — only the two with an
# unambiguous ConfigState analog are mapped; "available" needs no
# refinement (ACTIVE already), and "unknown" (status not yet probed) isn't
# confidently either credential_required or credential_failed, so it's
# left at ACTIVE too rather than guessed. RETRYING has no source anywhere
# in this read layer: `retry_provider_credential` is an intent-triggered
# action, not a persisted status this facade can observe — stays
# unreachable.
_CREDENTIAL_STATUS_TO_CONFIG_STATE = {
    "missing": ConfigState.CREDENTIAL_REQUIRED,
    "blocked": ConfigState.CREDENTIAL_FAILED,
}


def _config_state(record: ProviderRecord) -> ConfigState:
    if record.suspended:
        return ConfigState.SUSPENDED
    # provider_credential_status only means anything for api_key providers
    # (config_store itself only probes it in that case — see
    # config_store._provider_ui_state); subscription providers have no
    # credential-broker status to read.
    if record.mode == "api_key":
        status = store_access.get_provider_credential_status(record.id)
        refined = _CREDENTIAL_STATUS_TO_CONFIG_STATE.get(status)
        if refined is not None:
            return refined
    return ConfigState.ACTIVE


# config_store's provider `mode` field ("subscription" | "api_key" — see
# frontend/src/types.ts ProviderMode) is the only auth-mode signal
# store_access can reach; NONE has no store source distinguishing it from
# "not yet configured", so it's never emitted here.
_MODE_TO_AUTH_FLOW = {
    "subscription": AuthFlow.OAUTH_SUBSCRIPTION,
    "api_key": AuthFlow.API_KEY,
}


def _auth_flows(record: ProviderRecord) -> tuple[AuthFlow, ...]:
    flow = _MODE_TO_AUTH_FLOW.get(record.mode)
    return (flow,) if flow is not None else ()


# ---- orchestration_modes / send_modes -------------------------------------
# Neither store_access nor config_store enumerates "which orchestration/send
# modes does this provider support" as a discrete list anywhere — the
# legacy frontend instead gates availability directly off boolean
# capability flags (see frontend/src/types.ts's `supports_manager_mode`
# comment: "Drives NewSessionModal gating"). These two helpers derive the
# closest honest equivalent from ONLY capabilities this adapter already
# reads, using the exact literal values already used identically elsewhere
# in the backend for the same normalization (see
# backend/surface_commands.py / backend/offline_actions_api.py's
# `if orchestration_mode == "manager": orchestration_mode = "team"`).
#
# gap: "virtual" (the third `OrchestrationMode` value in
# frontend/src/types.ts) has no discovered per-provider capability gate
# anywhere in the codebase — left out rather than guessed.
def _orchestration_modes(caps: Capabilities) -> tuple[str, ...]:
    modes = ["native"]
    if caps.manager_mode:
        modes.append("team")
    return tuple(modes)


# `queue`/`interrupt` are gated by no capability anywhere in the legacy
# system (every provider can be sent a prompt or interrupted) — `steer`
# mirrors `capabilities.steering` exactly (the one SendMode value with an
# unambiguous, already-named boolean). SendMode has no fourth value in the
# backend contract (`backend/surface_contract/nodes.py`) — the frontend's
# extra "alter" string is not a `SendMode` member there either.
def _send_modes(caps: Capabilities) -> tuple[str, ...]:
    modes = [SendMode.QUEUE.value, SendMode.INTERRUPT.value]
    if caps.steering:
        modes.append(SendMode.STEER.value)
    return tuple(modes)


def _map_runtime_profile_record(record) -> RuntimeProfile:
    """From `store_access.RuntimeProfileRecord` (read plane)."""
    return RuntimeProfile(
        runtime_profile_id=record.id,
        provider_id=record.provider_id,
        runner=record.runner,
        default_model=record.default_model,
        default_reasoning_effort=record.default_reasoning_effort or None,
        name=record.name,
        deleted_at=record.deleted_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _map_runtime_profile_raw(raw: dict) -> RuntimeProfile | None:
    """From a plain dict — `runtime_profiles_api._snapshot()`'s own
    `runtime_profiles` entries, as carried verbatim on the D1
    `provider.runtime_profiles_changed` bus fact payload (live plane)."""
    profile_id = raw.get("id")
    provider_id = raw.get("provider_id")
    if not isinstance(profile_id, str) or not profile_id:
        return None
    if not isinstance(provider_id, str) or not provider_id:
        return None
    return RuntimeProfile(
        runtime_profile_id=profile_id,
        provider_id=provider_id,
        runner=str(raw.get("runner", "")),
        default_model=str(raw.get("default_model", "")),
        default_reasoning_effort=(str(raw["default_reasoning_effort"]) if raw.get("default_reasoning_effort") else None),
        name=str(raw.get("name", "")),
        deleted_at=raw.get("deleted_at"),
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
    )


def _map_descriptor(record: ProviderRecord) -> ProviderDescriptor:
    caps = _capabilities(record)
    return ProviderDescriptor(
        provider_id=record.id,
        display=Display(
            label=record.name or record.kind,
            icon_id=record.kind,
            config_copy_key=f"provider.config_copy.{record.kind}",
        ),
        auth_flows=_auth_flows(record),
        capabilities=caps,
        orchestration_modes=_orchestration_modes(caps),
        send_modes=_send_modes(caps),
        model_catalog_ref=record.id,
        config_state=_config_state(record),
    )


# ---- D2: installable-kind catalog ------------------------------------------
# Ported from frontend/src/components/ProviderForm.tsx's `TEMPLATES` array
# and frontend/src/components/providerFormShape.ts's `PROVIDER_MODES` —
# the "hardcoded InstallableProviderKind union, static provider template
# list" ADR 0007 explicitly targets for deletion once this catalog is live
# (see the caller's report for the exact frontend deletion this enables).
# `kind` here is the TEMPLATE id (frontend `Template.id`), distinct from —
# and usually coarser than — the underlying `ProviderRecord.kind` several
# templates share (e.g. "ollama"/"zai"/"custom" all create a record with
# kind="claude"; "sakana"/"meta-muse"/"zai-openai"/"hetzner"/"custom-openai"
# all use kind="openai"). This module owns the catalog as a static, code-
# defined table per ADR 0007's decision text ("replaces the frontend union
# + template list") — there is no store backing "installable templates";
# they are presets, not persisted records.

# Kinds whose form should not offer both auth modes (ported verbatim from
# providerFormShape.ts's `PROVIDER_MODES`); every other underlying kind
# offers both, matching `modesForKind`'s default there.
_RESTRICTED_TEMPLATE_MODES: dict[str, tuple[str, ...]] = {
    "openai": ("api_key",),
    "pi": ("subscription",),
    "cursor": ("subscription",),
    "kimi": ("subscription",),
    "opencode": ("subscription",),
}


def _template_modes(underlying_kind: str) -> tuple[str, ...]:
    return _RESTRICTED_TEMPLATE_MODES.get(underlying_kind, ("subscription", "api_key"))


@dataclass(frozen=True, slots=True)
class _Template:
    id: str
    label: str
    kind: str
    mode: str
    base_url: str
    config_dir: str
    default_model: str
    default_reasoning_effort: str
    api_key: str = ""
    suspended: bool = False


# One row per frontend Template — same id/label/kind/mode/base_url/
# config_dir/default_model/default_reasoning_effort/api_key/suspended
# fields as frontend/src/components/ProviderForm.tsx's `TEMPLATES` const.
_TEMPLATES: tuple[_Template, ...] = (
    _Template("claude", "Claude", "claude", "subscription", "", "", "claude-opus-5[1m]", "medium"),
    _Template("codex", "Codex", "codex", "subscription", "", "", "gpt-5.5", "medium"),
    _Template("agy", "Antigravity", "agy", "subscription", "", "$HOME/.gemini/antigravity-cli", "Gemini 3.5 Flash (Medium)", ""),
    _Template("copilot", "Copilot", "copilot", "subscription", "", "", "auto", ""),
    _Template("pi", "pi", "pi", "subscription", "", "", "anthropic/claude-opus-4-7", ""),
    _Template("qwen", "Qwen Code", "qwen", "subscription", "", "", "coder-model", ""),
    _Template("cursor", "Cursor", "cursor", "subscription", "", "", "auto", ""),
    _Template("kimi", "Kimi CLI", "kimi", "subscription", "", "", "kimi-code/kimi-for-coding", ""),
    _Template("amp", "Amp", "amp", "subscription", "", "", "auto", ""),
    _Template("opencode", "OpenCode", "opencode", "subscription", "", "", "opencode/big-pickle", ""),
    _Template("fugu", "Sakana Fugu", "fugu", "subscription", "", "$HOME/.codex", "fugu", ""),
    _Template("sakana", "Sakana Fugu (API)", "openai", "api_key", "https://api.sakana.ai/v1", "", "fugu", ""),
    _Template("meta-muse", "Meta Muse Spark", "openai", "api_key", "https://api.meta.ai/v1", "", "muse-spark-1.1", ""),
    _Template("ollama", "Ollama", "claude", "api_key", "http://localhost:11434", "$HOME/.claude-ollama", "qwen3-coder", "medium", api_key="ollama"),
    _Template("zai", "Z.AI (Claude)", "claude", "api_key", "https://api.z.ai/api/anthropic", "~/.claude-zai", "glm-4.6", "medium"),
    _Template("zai-openai", "Z.AI (OpenAI)", "openai", "api_key", "https://api.z.ai/api/coding/paas/v4", "", "glm-5.2", ""),
    _Template("hetzner", "Hetzner Inference", "openai", "api_key", "https://inference.hetzner.com/api/v1", "", "Qwen/Qwen3.6-35B-A3B-FP8", ""),
    _Template("custom", "Custom API", "claude", "api_key", "", "", "", "medium"),
    _Template("custom-openai", "Custom OpenAI", "openai", "api_key", "", "", "", ""),
)


def _api_env_copy_for_kind(kind: str) -> tuple[str, str, str]:
    """Ported verbatim from the deleted frontend `providerFormShape.ts`'s
    `apiEnvCopyForKind` (closure 4: form-copy regression) — env-var labels
    + key placeholder for the api_key fields. openai's runner reads
    OPENAI_API_KEY/OPENAI_BASE_URL; every other api_key-capable kind uses
    ANTHROPIC_*. Returns (key_label_key, url_label_key, key_placeholder_key)."""
    if kind == "openai":
        return "setup.apiKeyLabelOpenai", "setup.baseUrlLabelOpenai", "setup.apiKeyPlaceholderEmptyOpenai"
    return "setup.apiKeyLabel", "setup.baseUrlLabel", "setup.apiKeyPlaceholderEmpty"


def _config_dir_copy_for_kind(kind: str) -> tuple[str, str, str]:
    """Ported verbatim from the deleted frontend `ProviderForm.tsx`'s
    `configDirCopyForKind` (closure 4). Returns (label_key, placeholder_key,
    hint_key)."""
    if kind in ("codex", "fugu"):
        # Fugu deploys its profile into the Codex CLI config dir (~/.codex).
        return "setup.configDirLabelCodex", "setup.configDirPlaceholderCodex", "setup.configDirHintCodex"
    if kind == "agy":
        return "setup.configDirLabelAgy", "setup.configDirPlaceholderAgy", "setup.configDirHintAgy"
    if kind == "copilot":
        return "setup.configDirLabelCopilot", "setup.configDirPlaceholderCopilot", "setup.configDirHintCopilot"
    return "setup.configDirLabelClaude", "setup.configDirPlaceholderClaude", "setup.configDirHintClaude"


def _form_schema_for(template: _Template) -> tuple[FormField, ...]:
    """Fields the legacy `ProviderForm.tsx` actually submits as
    `FormPayload` (name/nickname/mode/base_url/config_dir/api_key/
    suspended) — `default_model`/`default_reasoning_effort` are seed-only
    (they go in `defaults`, not `form_schema`: the legacy form itself never
    collects them, `RuntimeProfileWizard` does). `default_permission` and
    per-kind capability overrides are excluded too: their vocabularies
    (backend/permission.py's per-kind, multi-axis choices; capability
    tri-state inherit/on/off) don't fit `FormField`'s flat text/path/
    secret/enum/bool shape without fabricating semantics the type doesn't
    support — documented gap, not a silent drop."""
    modes = _template_modes(template.kind)
    fields = [
        FormField(name="name", kind=FormFieldKind.TEXT, label_key="setup.nameLabel", required=True),
        FormField(name="nickname", kind=FormFieldKind.TEXT, label_key="setup.nicknameLabel"),
    ]
    if len(modes) > 1:
        fields.append(
            FormField(
                name="mode", kind=FormFieldKind.ENUM, label_key="setup.modeLabel",
                required=True, choices=modes, default=modes[0],
            )
        )
    if "api_key" in modes:
        key_label, url_label, key_placeholder = _api_env_copy_for_kind(template.kind)
        fields.append(FormField(
            name="api_key", kind=FormFieldKind.SECRET, label_key=key_label,
            placeholder_key=key_placeholder,
        ))
        fields.append(FormField(name="base_url", kind=FormFieldKind.TEXT, label_key=url_label))
    if template.kind != "openai":  # showConfigDirForKind
        dir_label, dir_placeholder, dir_hint = _config_dir_copy_for_kind(template.kind)
        fields.append(FormField(
            name="config_dir", kind=FormFieldKind.PATH, label_key=dir_label,
            placeholder_key=dir_placeholder, hint_key=dir_hint,
        ))
    fields.append(FormField(name="suspended", kind=FormFieldKind.BOOL, label_key="setup.suspendProviderLabel", default=False))
    return tuple(fields)


def _installable_descriptor(template: _Template) -> InstallableDescriptor:
    defaults: dict[str, object] = {
        "name": template.label,
        "kind": template.kind,
        "mode": template.mode,
        "base_url": template.base_url,
        "config_dir": template.config_dir,
        "default_model": template.default_model,
        "default_reasoning_effort": template.default_reasoning_effort,
    }
    if template.api_key:
        defaults["api_key"] = template.api_key
    if template.suspended:
        defaults["suspended"] = template.suspended
    return InstallableDescriptor(
        kind=template.id,
        display=Display(
            label=template.label,
            icon_id=template.kind,
            config_copy_key=f"provider.config_copy.{template.kind}",
        ),
        form_schema=_form_schema_for(template),
        defaults=defaults,
        auth_flows=tuple(
            _MODE_TO_AUTH_FLOW[m] for m in _template_modes(template.kind) if m in _MODE_TO_AUTH_FLOW
        ),
    )


_INSTALLABLE_CATALOG: tuple[InstallableDescriptor, ...] = tuple(
    _installable_descriptor(t) for t in _TEMPLATES
)


# ---- D1: bus fact -> frame mapping -----------------------------------------
# Fact type strings mirror backend/providers_api.py's FACT_* constants
# exactly (that module is outside this file's import boundary, so the
# strings are duplicated here rather than imported — see this module's
# own docstring for why providers_api.py can't be a dependency).
_FACT_PROVIDER_MUTATED = "provider.mutated"
_FACT_CREDENTIAL_STATE_CHANGED = "provider.credential_state_changed"
_FACT_MODEL_CATALOG_CHANGED = "provider.model_catalog_changed"
_FACT_LOGIN_FLOW_CHANGED = "provider.login_flow_changed"
_FACT_INSTALL_PROGRESS_CHANGED = "provider.install_progress_changed"
_FACT_RUNTIME_PROFILES_CHANGED = "provider.runtime_profiles_changed"

_INSTALL_STATE_VALUES = frozenset(s.value for s in InstallState)


class ProviderConfigSurfaceAdapter(ProviderConfigSurface):
    def __init__(self) -> None:
        self._projection = BusBoundProjection()
        self._cache_lock = threading.Lock()
        self._last_descriptor: dict[ProviderId, ProviderDescriptor] = {}
        self._last_config_state: dict[ProviderId, ConfigState] = {}

    def bind(self) -> None:
        """Idempotent: `BusBoundProjection.bind` unsubscribes-then-
        resubscribes under deterministic per-pattern names."""
        self._projection.bind(
            [
                (_FACT_PROVIDER_MUTATED, self._on_provider_mutated),
                (_FACT_CREDENTIAL_STATE_CHANGED, self._on_credential_state_changed),
                (_FACT_MODEL_CATALOG_CHANGED, self._on_model_catalog_changed),
                (_FACT_LOGIN_FLOW_CHANGED, self._on_login_flow_changed),
                (_FACT_INSTALL_PROGRESS_CHANGED, self._on_install_progress_changed),
                (_FACT_RUNTIME_PROFILES_CHANGED, self._on_runtime_profiles_changed),
            ]
        )

    # ---- read plane ------------------------------------------------

    def list_providers(self) -> tuple[ProviderDescriptor, ...]:
        return tuple(_map_descriptor(r) for r in store_access.list_provider_records())

    def installable_catalog(self) -> tuple[InstallableDescriptor, ...]:
        return _INSTALLABLE_CATALOG

    def model_catalog(self, provider_id: ProviderId) -> ModelCatalog:
        record = next(
            (r for r in store_access.list_provider_records() if r.id == provider_id), None,
        )
        if record is None:
            return ModelCatalog(provider_id=provider_id, models=())
        # gap: models aren't individually tagged with a runner — best
        # available signal is the provider's own runtime profile(s); if
        # more than one runner is configured for this provider, every
        # model is stamped with the first one found (list order).
        runner = next(
            (p.runner for p in store_access.list_runtime_profiles() if p.provider_id == provider_id),
            "",
        )
        models = tuple(
            # gap: per-model reasoning-effort/retirement data DOES exist
            # (backend/runtime_profile.py's `reasoning_efforts()`;
            # backend/models.py's persisted retired-model cache) but
            # neither is reachable from this file without extending
            # backend/adapters/store_access.py — the ONE sanctioned
            # store-access boundary — which is outside this pass's
            # exclusive file scope (see the caller's report). Left at the
            # documented-empty/False default rather than guessed.
            CatalogModel(model=m, runner=runner, reasoning_efforts=(), retired=False)
            for m in record.models
        )
        return ModelCatalog(provider_id=provider_id, models=models)

    def runtime_profiles(self) -> RuntimeProfilesSnapshot:
        return RuntimeProfilesSnapshot(
            profiles=tuple(
                _map_runtime_profile_record(p)
                for p in store_access.list_runtime_profiles(include_deleted=True)
            ),
            default_runtime_profile_id=store_access.get_default_runtime_profile_id(),
            last_models=store_access.get_last_models(),
            last_reasoning_efforts=store_access.get_last_reasoning_efforts(),
            deleted_providers=tuple(
                DeletedProviderRef(
                    provider_id=r.id, name=r.name, nickname=r.nickname,
                    kind=r.kind, deleted_at=r.deleted_at,
                )
                for r in store_access.list_deleted_providers()
            ),
        )

    # ---- live plane --------------------------------------------------

    def subscribe(self, emit: Emit) -> Subscription:
        self._projection.register(emit)
        return _SubscriptionImpl(lambda: self._projection.unregister(emit))

    def reject_intent(self, intent_id: str, code: str, message: str) -> None:
        """Push an async `IntentRejected` over the live plane (ADR 0006 §5
        "acks are projection facts") — called by
        `backend.adapter_api.ProviderCommandPort` when a mutation it already
        admitted (`submit()`'s synchronous `IntentAccepted`) fails once its
        fire-and-forget coroutine actually runs the business logic.
        Broadcasts the SAME `IntentRejected` type `submit()` itself returns
        for synchronously-determinable rejections — one ack vocabulary, two
        delivery times — instead of inventing a second frame shape. No `cv`
        bump: `IntentRejected` carries no `cv` field (it is not part of the
        change-only descriptor projection any other frame here refines), and
        it is not cached/dedup'd like the descriptor frames above — each
        rejection is a one-time event for exactly one `intent_id`."""
        self._projection.broadcast(IntentRejected(intent_id=intent_id, code=code, message=message))

    # ---- command plane -------------------------------------------------

    def submit(self, intent: ProviderIntent) -> TransportAck:
        port = getattr(self, "_command_port", None)
        if port is None:
            return IntentRejected(
                intent_id=intent.intent_id,
                code="unsupported_contract_phase",
                message="command plane not wired yet; the legacy REST/WS path remains authoritative",
            )
        # Acks are accept/reject only (surface_contract/intents.py's module
        # docstring) — the real outcome surfaces later as a projection
        # fact over the live plane above, so a well-formed intent this
        # port has real support for is admitted and its coroutine
        # scheduled WITHOUT this synchronous call waiting on it, exactly
        # mirroring `backend/adapters/chat_adapter.py`'s `submit()`.
        if isinstance(intent, CreateProvider):
            coro = port.create_provider(intent.intent_id, intent.kind, intent.config)
        elif isinstance(intent, UpdateProvider):
            coro = port.update_provider(intent.intent_id, intent.provider_id, intent.config_patch)
        elif isinstance(intent, DeleteProvider):
            coro = port.delete_provider(intent.intent_id, intent.provider_id)
        elif isinstance(intent, SuspendProvider):
            coro = port.suspend_provider(intent.intent_id, intent.provider_id, intent.suspended)
        elif isinstance(intent, RetryCredential):
            coro = port.retry_credential(intent.intent_id, intent.provider_id)
        elif isinstance(intent, BeginLogin):
            coro = port.begin_login(intent.intent_id, intent.provider_id, intent.flow)
        elif isinstance(intent, CancelLogin):
            coro = port.cancel_login(intent.intent_id, intent.provider_id)
        elif isinstance(intent, RefreshModels):
            coro = port.refresh_models(intent.intent_id, intent.provider_id)
        elif isinstance(intent, SaveRuntimeProfile):
            coro = port.save_runtime_profile(intent.intent_id, intent.profile)
        elif isinstance(intent, DeleteRuntimeProfile):
            coro = port.delete_runtime_profile(intent.intent_id, intent.runtime_profile_id)
        else:
            return IntentRejected(
                intent_id=intent.intent_id,
                code="unsupported",
                message=f"{type(intent).__name__} intents are not yet supported on this transport",
            )
        asyncio.get_running_loop().create_task(coro)
        return IntentAccepted(intent_id=intent.intent_id)

    # ---- internals: live fact handlers ----------------------------------

    def _record_for(self, provider_id: str) -> ProviderRecord | None:
        return next(
            (r for r in store_access.list_provider_records() if r.id == provider_id), None,
        )

    async def _on_provider_mutated(self, event: BusEvent) -> None:
        provider_id = event.payload.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            return
        record = self._record_for(provider_id)
        if record is None:
            # gap: ADR 0007's ProviderFrame union has no removal/tombstone
            # variant — a deleted provider is only discoverable the next
            # time a client re-fetches `list_providers()` (mirrors
            # SessionRemoved's absent ProviderRemoved sibling in ADR
            # 0008). Drop the cache entry so a re-added provider with the
            # same id re-broadcasts cleanly.
            with self._cache_lock:
                self._last_descriptor.pop(provider_id, None)
            return
        descriptor = _map_descriptor(record)
        with self._cache_lock:
            if self._last_descriptor.get(provider_id) == descriptor:
                return
            self._last_descriptor[provider_id] = descriptor
        cv = self._projection.bump_render()
        self._projection.broadcast(ProviderUpsert(cv=cv, descriptor=descriptor))

    async def _on_credential_state_changed(self, event: BusEvent) -> None:
        provider_id = event.payload.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            return
        record = self._record_for(provider_id)
        if record is None:
            with self._cache_lock:
                self._last_config_state.pop(provider_id, None)
            return
        state = _config_state(record)
        with self._cache_lock:
            if self._last_config_state.get(provider_id) == state:
                return
            self._last_config_state[provider_id] = state
        cv = self._projection.bump_render()
        self._projection.broadcast(
            CredentialState(cv=cv, provider_id=provider_id, config_state=state)
        )

    async def _on_model_catalog_changed(self, event: BusEvent) -> None:
        provider_id = event.payload.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            return
        # Change-only already enforced at the source (providers_api.py /
        # runtime_profiles_api.py only publish this fact behind a real
        # diff) — no local cache needed here.
        cv = self._projection.bump_render()
        self._projection.broadcast(ModelCatalogChanged(cv=cv, provider_id=provider_id))

    async def _on_login_flow_changed(self, event: BusEvent) -> None:
        payload = event.payload
        provider_id = payload.get("provider_id")
        phase = payload.get("phase")
        if not isinstance(provider_id, str) or not provider_id or not isinstance(phase, str):
            return
        try:
            login_phase = LoginPhase(phase)
        except ValueError:
            return
        cv = self._projection.bump_render()
        self._projection.broadcast(
            LoginFlowFrame(
                cv=cv,
                state=LoginFlowState(
                    provider_id=provider_id,
                    intent_id=str(payload.get("intent_id") or ""),
                    phase=login_phase,
                    data=payload.get("data"),
                ),
            )
        )

    async def _on_install_progress_changed(self, event: BusEvent) -> None:
        """`providers_api._broadcast_install` publishes the FULL current
        run snapshot on every progress tick (not a partial delta) — this
        handler only needs to type-map that payload, never accumulate
        lines itself. `provider_setup._INSTALL_RUNS` has no store_access
        equivalent (same rationale as `_on_login_flow_changed` above), so
        the fact carries its complete projected payload directly; never
        change-only dedup'd (every tick, including a repeated line, is a
        real, distinct event worth showing live)."""
        payload = event.payload
        kind = payload.get("kind")
        state_raw = payload.get("state")
        if not isinstance(kind, str) or not kind or state_raw not in _INSTALL_STATE_VALUES:
            return
        lines_raw = payload.get("lines")
        lines = tuple(
            InstallLine(stream=str(line.get("s", "")), text=str(line.get("t", "")))
            for line in lines_raw
            if isinstance(line, dict)
        ) if isinstance(lines_raw, list) else ()
        cv = self._projection.bump_render()
        self._projection.broadcast(
            InstallProgressChanged(
                cv=cv,
                run=InstallRun(
                    kind=kind,
                    label=str(payload.get("label", "")),
                    command=str(payload.get("command", "")),
                    state=InstallState(state_raw),
                    lines=lines,
                    started_at=payload.get("started_at"),
                    finished_at=payload.get("finished_at"),
                    returncode=payload.get("returncode"),
                    installed=payload.get("installed"),
                    message=payload.get("message"),
                ),
            )
        )

    async def _on_runtime_profiles_changed(self, event: BusEvent) -> None:
        """Closure 3: `runtime_profiles_api._broadcast_changed` publishes
        the SAME full-snapshot dict (`_snapshot()`'s own shape) its legacy
        `runtime_profiles_changed` WS broadcast carries — unconditionally,
        on every mutation path including the two that carry no
        `provider_id` (`record_last_model`/`record_last_reasoning_effort`)
        and so never reach `_on_model_catalog_changed` above. Mapped, not
        re-derived: `default_runtime_profile_id`/`last_models`/etc. have no
        durable per-entity source `store_access` could re-pull from this
        fact alone (same rationale as `_on_login_flow_changed`)."""
        payload = event.payload
        raw_profiles = payload.get("runtime_profiles")
        if not isinstance(raw_profiles, list):
            return
        profiles = tuple(
            mapped for p in raw_profiles
            if isinstance(p, dict) and (mapped := _map_runtime_profile_raw(p)) is not None
        )
        last_models = payload.get("last_models")
        last_efforts = payload.get("last_reasoning_efforts")
        raw_deleted = payload.get("deleted_providers")
        deleted_providers = tuple(
            DeletedProviderRef(
                provider_id=str(r.get("id", "")),
                name=str(r.get("name", "")),
                nickname=str(r.get("nickname", "")),
                kind=str(r.get("kind", "")),
                deleted_at=r.get("deleted_at"),
            )
            for r in raw_deleted
            if isinstance(r, dict)
        ) if isinstance(raw_deleted, list) else ()
        cv = self._projection.bump_render()
        self._projection.broadcast(
            RuntimeProfilesChanged(
                cv=cv,
                snapshot=RuntimeProfilesSnapshot(
                    profiles=profiles,
                    default_runtime_profile_id=payload.get("default_runtime_profile_id"),
                    last_models=dict(last_models) if isinstance(last_models, dict) else {},
                    last_reasoning_efforts=dict(last_efforts) if isinstance(last_efforts, dict) else {},
                    deleted_providers=deleted_providers,
                ),
            )
        )
