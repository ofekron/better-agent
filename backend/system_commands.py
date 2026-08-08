"""Concrete `SystemCommandPort` implementation (ADR 0011 System & Host
Surface command plane).

Sits OUTSIDE the `backend/adapters/` import boundary — same port/adapter
split as `backend/surface_commands.py`/`backend/session_commands.py` (see
those modules' docstrings and `backend/adapters/command_port.py`) — so it
is free to import `extension_store`, `extension_api`, `harness_profile_store`,
`harness_profiles_api`, `marketplace_bridge`, `stores.schedule_store`,
`scheduler`, `installation_profile`, `node_link`, `node_registry_store`,
`node_provider_credential_sync`, `node_store`, `topology`: the SAME
mutation functions the legacy REST routes call. This is the single
mutation path — every method here is a thin wrapper, never a second,
parallel implementation.

Legacy gaps documented rather than invented (CLAUDE.md: "never invent"):
`stores/schedule_store.py` has no update/pause/resume mutation
(create/list/delete only) — `update_schedule`/`pause_schedule`/
`resume_schedule` are NOT in `SystemIntent` (see
`backend/surface_contract/intents.py`) for this reason, so this port has
no corresponding methods either. Global harness-profile default
assignment (`set_default_harness_profile`) has no legacy mutation path
either — legacy only has the synthesized `DEFAULT_PROFILE_ID` sentinel
and per-session profile selection (`ADR 0006 SetSelectors.
harness_profile_id`), never an installation-wide "set this profile as
THE default" — `set_default_harness_profile` below always rejects with
`code="unsupported"`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from backend.adapters.command_port import CommandResult, SystemCommandPort

logger = logging.getLogger(__name__)


def _rejected(exc: Exception) -> CommandResult:
    if isinstance(exc, HTTPException):
        return CommandResult(accepted=False, code=str(exc.status_code), message=str(exc.detail))
    return CommandResult(accepted=False, code="invalid", message=str(exc))


class _SystemCommandPortImpl:
    # ---- §2 extension config + harness profiles ----------------------

    async def update_extension_config(
        self, extension_id: str, section: str, patch: dict[str, Any],
    ) -> CommandResult:
        import config_store
        import extension_api
        import extension_store

        try:
            if section == "settings":
                for key, value in patch.items():
                    extension_store.set_extension_setting(extension_id, key, value)
                await extension_api._broadcast_extension_changed("extension.config.settings")
            elif section == "instructions":
                extension_store.set_user_instructions(extension_id, patch.get("instructions", ""))
                await extension_api._broadcast_extension_changed("extension.config.instructions")
            elif section == "ui_settings":
                extension_store.set_ui_settings(
                    extension_id,
                    quick_button_enabled=patch.get("quick_button_enabled"),
                    page_enabled=patch.get("page_enabled"),
                )
                await extension_api._broadcast_extension_changed(
                    "extension.config.ui_settings", "extension.ui",
                )
            elif section == "internal_llm":
                record = extension_store.get_extension(extension_id)
                if record is None:
                    raise extension_store.ExtensionError("Extension not installed")
                task_keys = set(extension_store.extension_internal_llm_tasks(record))
                assignments = patch.get("assignments") or {}
                forbidden = sorted(str(k) for k in assignments if str(k) not in task_keys)
                if forbidden:
                    raise HTTPException(
                        status_code=403,
                        detail="internal LLM task is not owned by this extension",
                    )
                current = config_store.get_internal_llm_assignments()
                merged = {k: v for k, v in current.items() if k not in task_keys}
                merged.update(assignments)
                config_store.set_internal_llm_assignments(merged)
                await extension_api._broadcast_extension_changed("extension.config.internal_llm")
            elif section == "permissions":
                extension_store.set_permission_grant(
                    extension_id, patch.get("permission", ""), bool(patch.get("granted")),
                )
                await extension_api._broadcast_extension_changed(
                    "extension.config.permissions", extension_id=extension_id,
                )
            elif section == "mcp":
                extension_store.set_mcp_server_enabled(
                    extension_id, patch.get("server_name", ""), bool(patch.get("enabled")),
                )
                await extension_api._broadcast_extension_changed("extension.config.mcp")
            elif section == "skills":
                extension_store.set_runtime_skill_enabled(
                    extension_id, patch.get("skill_name", ""), bool(patch.get("enabled")),
                )
                await extension_api._broadcast_extension_changed("extension.config.skills")
            else:
                return CommandResult(accepted=False, code="unknown_section", message=section)
        except (extension_store.ExtensionError, HTTPException, ValueError) as exc:
            return _rejected(exc)
        return CommandResult(accepted=True)

    async def save_harness_profile(
        self,
        harness_profile_id: str | None,
        config: dict[str, Any],
        revision: str | None,
        writes: tuple[dict[str, Any], ...],
    ) -> CommandResult:
        """Creation (`harness_profile_id=None`) calls the SAME
        `harness_profile_store.create_profile` the legacy `POST` route
        calls. A write against an existing profile calls the SAME
        `harness_field_writer.apply_field_writes` the legacy `PATCH
        .../fields` route calls (NOT `apply_override_patch` directly —
        that's the older `/overrides` route's narrower shape, which
        `HarnessSettingsEditor.tsx`'s `writeFields` has never used).

        Rejection codes are typed by re-deriving WHICH of `apply_field_writes`'s
        (via `harness_profile_resolver.resolve_profile`, called first, then
        `harness_profile_store.apply_override_patch`/`set_profile_meta`)
        few exception sources actually fired — mirrors the exact exception
        set `harness_profiles_api.py`'s legacy `PATCH .../fields` route
        catches, typed instead of collapsed into one HTTP status:
        `HarnessProfileNotFoundError`/`HarnessProfileMissingError` ->
        `not_found`; `HarnessFieldError` -> `invalid_field`;
        `HarnessProfileResolutionError` -> `stale_revision` when its
        message names a stale/unavailable revision (the base-chain-cycle
        message it can also carry is NOT a concurrency conflict, so that
        one stays generic); the DEFAULT-profile-not-stored branch is pre-
        empted by the guard below so a bare `HarnessProfileError` (from
        `apply_override_patch`'s own later, narrower revision re-check)
        can only mean the same stale-revision conflict.
        """
        import extension_api
        import harness_field_writer
        import harness_fields
        import harness_profile_resolver
        import harness_profile_store
        import harness_profiles_api

        try:
            if harness_profile_id:
                if harness_profile_id == harness_profile_store.DEFAULT_PROFILE_ID:
                    return CommandResult(
                        accepted=False, code="invalid",
                        message="the default profile is not a stored profile",
                    )
                if not writes:
                    return CommandResult(
                        accepted=False, code="invalid", message="writes must be a non-empty list",
                    )
                profile = harness_field_writer.apply_field_writes(
                    harness_profile_id, revision, list(writes),
                )
                action = "fields_updated"
            else:
                profile = harness_profile_store.create_profile(config)
                action = "created"
        except (
            harness_profile_store.HarnessProfileNotFoundError,
            harness_profile_resolver.HarnessProfileMissingError,
        ) as exc:
            return CommandResult(accepted=False, code="not_found", message=str(exc))
        except harness_fields.HarnessFieldError as exc:
            return CommandResult(accepted=False, code="invalid_field", message=str(exc))
        except harness_profile_resolver.HarnessProfileResolutionError as exc:
            code = "stale_revision" if "revision" in str(exc).lower() else "invalid"
            return CommandResult(accepted=False, code=code, message=str(exc))
        except harness_profile_store.HarnessProfileError as exc:
            return CommandResult(accepted=False, code="stale_revision", message=str(exc))
        profile_id = profile.get("id") if profile else harness_profile_id
        await harness_profiles_api._broadcast_harness_profiles_changed({
            "action": action, "profile_id": profile_id,
            "revision": profile.get("revision") if profile else "",
        })
        await extension_api._broadcast_extension_changed("extension.config", "extension.harness.default")
        return CommandResult(accepted=True, ref=profile_id)

    async def delete_harness_profile(
        self, harness_profile_id: str, revision: str | None,
    ) -> CommandResult:
        import harness_profile_store
        import harness_profiles_api

        if harness_profile_id == harness_profile_store.DEFAULT_PROFILE_ID:
            return CommandResult(
                accepted=False, code="invalid", message="the default profile cannot be deleted",
            )
        try:
            deleted = harness_profile_store.delete_profile(harness_profile_id, revision)
        except harness_profile_store.HarnessProfileError as exc:
            # The DEFAULT-profile branch is pre-empted above, so a bare
            # `HarnessProfileError` here is the optimistic-concurrency
            # check — same "no distinguishing exception subtype" reasoning
            # as `save_harness_profile` above.
            return CommandResult(accepted=False, code="stale_revision", message=str(exc))
        if not deleted:
            return CommandResult(accepted=False, code="404", message="harness profile not found")
        await harness_profiles_api._broadcast_harness_profiles_changed({
            "action": "deleted", "profile_id": harness_profile_id, "revision": "",
        })
        return CommandResult(accepted=True)

    async def set_default_harness_profile(self, harness_profile_id: str) -> CommandResult:
        # See module docstring: no legacy mutation exists for this.
        return CommandResult(
            accepted=False, code="unsupported",
            message="no legacy mechanism sets a global default harness profile",
        )

    # ---- §4 extension catalog -------------------------------------------

    async def install_extension(self, extension_id: str, source: dict[str, Any]) -> CommandResult:
        import extension_api
        import extension_store

        try:
            if source.get("marketplace_metadata_url") or source.get("marketplace_metadata"):
                extension_store.install_from_marketplace_metadata(
                    metadata=source.get("marketplace_metadata") or None,
                    metadata_url=source.get("marketplace_metadata_url", ""),
                    entitlement_token=source.get("entitlement_token", ""),
                )
            elif source.get("artifact_url"):
                extension_store.install_from_artifact(
                    artifact_url=source.get("artifact_url", ""),
                    artifact_sha256=source.get("artifact_sha256", ""),
                    artifact_signature=source.get("artifact_signature", ""),
                    entitlement_token=source.get("entitlement_token", ""),
                )
            else:
                extension_store.install_from_repo(
                    repo_url=source.get("repo_url", ""),
                    extension_path=source.get("extension_path", ""),
                    ref=source.get("ref", ""),
                    entitlement_token=source.get("entitlement_token", ""),
                )
        except extension_store.ExtensionError as exc:
            return _rejected(exc)
        await extension_api._broadcast_extension_changed(*extension_api.EXTENSION_CATALOG_TOPICS)
        return CommandResult(accepted=True)

    async def update_extension(self, extension_id: str) -> CommandResult:
        import extension_api
        import extension_store

        try:
            result = extension_store.apply_extension_update(extension_id)
        except extension_store.ExtensionError as exc:
            return _rejected(exc)
        if result.get("updated"):
            await extension_api._broadcast_extension_changed(*extension_api.EXTENSION_CATALOG_TOPICS)
        await extension_api._broadcast_extension_updates_changed()
        return CommandResult(accepted=True)

    async def uninstall_extension(self, extension_id: str) -> CommandResult:
        import extension_api
        import extension_store

        try:
            extension_store.uninstall(extension_id)
        except extension_store.ExtensionError as exc:
            return _rejected(exc)
        await extension_api._broadcast_extension_changed(
            *extension_api.EXTENSION_CATALOG_TOPICS, extension_id=extension_id,
        )
        return CommandResult(accepted=True)

    async def _set_enabled(self, extension_id: str, enabled: bool) -> CommandResult:
        import extension_api
        import extension_store

        try:
            extension_store.set_enabled(extension_id, enabled)
        except (extension_store.ExtensionConsentRequired, extension_store.ExtensionError) as exc:
            return _rejected(exc)
        await extension_api._broadcast_extension_changed(*extension_api.EXTENSION_CATALOG_TOPICS)
        return CommandResult(accepted=True)

    async def enable_extension(self, extension_id: str) -> CommandResult:
        return await self._set_enabled(extension_id, True)

    async def disable_extension(self, extension_id: str) -> CommandResult:
        return await self._set_enabled(extension_id, False)

    # ---- §5 marketplace bridge -------------------------------------------

    async def decide_marketplace_intent(
        self, marketplace_intent_id: str, decision: str,
    ) -> CommandResult:
        import marketplace_bridge

        try:
            if decision == "approve":
                await marketplace_bridge.bridge.approve(marketplace_intent_id)
            else:
                await marketplace_bridge.bridge.reject(marketplace_intent_id)
        except ValueError as exc:
            return _rejected(exc)
        return CommandResult(accepted=True)

    async def revoke_marketplace_device(self, device_ref: str) -> CommandResult:
        import marketplace_bridge

        try:
            await marketplace_bridge.bridge.revoke(device_ref)
        except ValueError as exc:
            return _rejected(exc)
        return CommandResult(accepted=True)

    # ---- §6 schedules -----------------------------------------------------

    async def create_schedule(
        self, target_session_id: str, prompt: str, cadence: dict[str, Any],
    ) -> CommandResult:
        import scheduler
        from orchestrator import get_active_coordinator
        from stores import schedule_store

        try:
            schedule_store.create(
                app_session_id=target_session_id,
                prompt=prompt,
                kind=cadence.get("kind", "once"),
                fire_at=cadence.get("fire_at"),
                interval_seconds=cadence.get("interval_seconds"),
            )
        except ValueError as exc:
            return _rejected(exc)
        coordinator = get_active_coordinator()
        if coordinator is not None:
            await scheduler.broadcast_schedules(coordinator, target_session_id)
        return CommandResult(accepted=True)

    async def delete_schedule(self, schedule_id: str) -> CommandResult:
        import scheduler
        from orchestrator import get_active_coordinator
        from stores import schedule_store

        removed = schedule_store.delete(schedule_id)
        if removed is None:
            return CommandResult(accepted=False, code="404", message="unknown schedule_id")
        coordinator = get_active_coordinator()
        if coordinator is not None:
            await scheduler.broadcast_schedules(coordinator, removed.get("app_session_id") or "")
        return CommandResult(accepted=True)

    # ---- §8 installation capabilities --------------------------------------

    async def set_installation_capability(
        self, capability_id: str, enabled: bool, confirm: bool,
    ) -> CommandResult:
        import installation_capabilities
        import installation_profile
        from orchestrator import get_active_coordinator

        if capability_id not in installation_capabilities.TOGGLEABLE:
            return CommandResult(accepted=False, code="404", message="unknown capability")
        if (
            capability_id == installation_capabilities.INTEGRATIONS
            and not enabled
            and not confirm
        ):
            # Mirrors `mobile_desktop_api.py`'s route exactly (409 +
            # message) via the same `_rejected`/`HTTPException` idiom
            # every other method here uses for a legacy-route-shaped
            # rejection.
            return _rejected(HTTPException(
                status_code=409,
                detail="disabling integrations cancels extension-owned work; confirm to proceed",
            ))
        try:
            state = installation_profile.set_capability_enabled(capability_id, enabled)
        except installation_profile.InstallationProfileError as exc:
            return _rejected(exc)
        coordinator = get_active_coordinator()
        if coordinator is not None:
            await coordinator.broadcast_global("installation_capabilities_changed", state)
        # ADR 0011 §8 fact for `backend/adapters/system_adapter.py`'s
        # `InstallationCapability` live push — `mobile_desktop_api.py`'s
        # REST route already publishes this; this v2 command-port path
        # bypasses that route entirely, so it publishes the SAME fact
        # itself rather than leaving the v2-submitted path silently un-
        # pushed.
        try:
            from event_bus import BusEvent, bus

            await bus.publish(BusEvent(
                type="installation.fire.capability_changed",
                root_id="system", sid="system",
                payload={"capability_id": capability_id},
                persist=False,
            ))
        except Exception:
            logger.exception("installation.fire.capability_changed publish failed")
        return CommandResult(accepted=True)

    # ---- §9 machine/node topology -------------------------------------------

    async def remove_node(self, node_id: str) -> CommandResult:
        import asyncio

        import node_provider_credential_sync
        import node_registry_store
        import node_store
        import topology

        if not node_registry_store.remove(node_id):
            try:
                removed = topology.remove_node(node_id)
            except topology.TopologyError as exc:
                return _rejected(exc)
            if not removed:
                return CommandResult(accepted=False, code="404", message="unknown node_id")
        await asyncio.to_thread(node_provider_credential_sync.remove_node, node_id)
        await node_store.forget(node_id)
        return CommandResult(accepted=True)

    async def sync_node_providers(
        self, node_id: str, include_secrets: bool, provider_ids: tuple[str, ...],
    ) -> CommandResult:
        """Covers only the legacy `POST .../sync-providers` route's
        `include_secrets: true` + non-empty `provider_ids` shape (the
        credential-authorize-and-sync path — its per-provider outcomes are
        the `NodeProviderCredentialStatus` records this adapter already
        pushes via `machines.fire.credential_changed`). The route's other
        shape (`include_secrets` false/absent — a one-shot, unauthenticated
        provider-list RPC push with no durable state) is rejected as
        `unsupported`: there is nothing `store_access` could re-derive a
        result from, so no honest v2 representation exists for it (see
        `intents.py`'s `SyncNodeProviders` docstring)."""
        if not include_secrets or not provider_ids:
            return CommandResult(
                accepted=False, code="unsupported",
                message="sync_node_providers only covers the credential-sync path",
            )
        import node_provider_credential_sync

        try:
            await node_provider_credential_sync.authorize_and_sync(node_id, list(provider_ids))
        except (RuntimeError, ValueError) as exc:
            return CommandResult(accepted=False, code="409", message=str(exc))
        return CommandResult(accepted=True)

    async def resolve_node_registration(self, node_id: str, decision: str) -> CommandResult:
        import node_link

        if decision == "approved":
            rec, reason = await node_link.approve_registration(node_id)
        else:
            rec, reason = await node_link.deny_registration(node_id)
        if reason == "missing":
            return CommandResult(accepted=False, code="404", message="node request not found")
        if reason == "expired":
            return CommandResult(accepted=False, code="410", message="node request expired")
        return CommandResult(accepted=True)


def build_system_command_port() -> SystemCommandPort:
    return _SystemCommandPortImpl()
