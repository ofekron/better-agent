from __future__ import annotations

import copy
import hashlib
import json
import threading
from typing import Any

import capability_contexts as capability_contexts_mod
import config_store
import extension_instructions
import extension_store
import harness_fields
import harness_profile_store
import provider_kinds


class HarnessProfileResolutionError(ValueError):
    pass


class HarnessProfileMissingError(HarnessProfileResolutionError):
    """The profile being resolved is gone (or its pinned revision is). The API
    layer answers 404 for it so a deleted selection reads as a selection
    change rather than a server failure."""


_CACHE_LOCK = threading.RLock()
_DEFAULT_PROFILE_CACHE: tuple[tuple[Any, ...], dict[str, Any]] | None = None
_RUN_SNAPSHOT_CACHE_MAX = 64
_RUN_SNAPSHOT_CACHE: dict[
    tuple[Any, ...],
    tuple[tuple[tuple[str, str | None], ...], dict[str, Any]],
] = {}


def invalidate_cache() -> None:
    global _DEFAULT_PROFILE_CACHE
    with _CACHE_LOCK:
        _DEFAULT_PROFILE_CACHE = None
        _RUN_SNAPSHOT_CACHE.clear()


def _default_profile_cache_key() -> tuple[Any, ...]:
    # Deliberately narrower than "every file this module touches": each
    # entry here must be an actual read inside _compute_default_profile_uncached,
    # or an unrelated write (a provider-state field, a named harness profile
    # edit) spuriously invalidates the cache and forces the next caller —
    # possibly a turn — to pay a full resynthesis, including an OS-keychain
    # probe per extension secret, for a profile that didn't actually change.
    # config_store.config_fingerprint()/harness_profile_store.store_fingerprint()
    # are whole-file mtimes; config.json also holds provider/session state,
    # and harness_profile_store's named-profile store isn't read by
    # _compute_default_profile_uncached at all, so it's excluded outright.
    return (
        config_store.disabled_builtins_fingerprint(),
        extension_store.store_fingerprint(),
        extension_store.extension_settings_fingerprint(),
    )


def _json_cache_token(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        encoded = repr(value).encode()
    return hashlib.sha256(encoded).hexdigest()


def _run_snapshot_cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        cached = _RUN_SNAPSHOT_CACHE.get(key)
    if cached is None:
        return None
    package_fingerprints, snapshot = cached
    current_fingerprints = _selected_package_fingerprints(
        (snapshot.get("extension_revisions") or {}).keys()
    )
    if current_fingerprints != package_fingerprints:
        with _CACHE_LOCK:
            if _RUN_SNAPSHOT_CACHE.get(key) == cached:
                _RUN_SNAPSHOT_CACHE.pop(key, None)
        return None
    return copy.deepcopy(snapshot)


def _run_snapshot_cache_put(key: tuple[Any, ...], value: dict[str, Any]) -> dict[str, Any]:
    package_fingerprints = _selected_package_fingerprints(
        (value.get("extension_revisions") or {}).keys()
    )
    with _CACHE_LOCK:
        if len(_RUN_SNAPSHOT_CACHE) >= _RUN_SNAPSHOT_CACHE_MAX:
            _RUN_SNAPSHOT_CACHE.pop(next(iter(_RUN_SNAPSHOT_CACHE)))
        _RUN_SNAPSHOT_CACHE[key] = (package_fingerprints, copy.deepcopy(value))
    return value


def _enforce_cross_provider_parity(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate cross-provider consistency of a resolved snapshot.

    Provider scoping is owned by the canonical per-provider resolution path
    (``_provider_context_blocks`` fans each section out to its declared
    ``providers``; an empty list means every provider kind). This function
    must NOT fabricate parity by copying one provider's content into another
    -- provider-specific content differs by design, and copying it would
    inject another provider's instructions where they do not apply.

    Instead it validates structure and fails closed on a genuine
    determinism bug: a capability context whose ``outputs`` contain more
    than one entry for the same provider kind (ambiguous resolution).
    """
    for ctx in snapshot.get("capability_contexts") or []:
        seen: set[str] = set()
        for out in ctx.get("outputs") or []:
            kind = out.get("provider_kind")
            if not kind:
                continue
            if kind in seen:
                raise HarnessProfileResolutionError(
                    f"Capability context {ctx.get('source_id')!r} has duplicate "
                    f"output for provider {kind!r}"
                )
            seen.add(kind)
    return snapshot


def _selected_package_fingerprints(extension_ids: Any) -> tuple[tuple[str, str | None], ...]:
    fingerprints: list[tuple[str, str | None]] = []
    for extension_id in sorted(str(item) for item in extension_ids if str(item or "").strip()):
        record = extension_store.get_extension(extension_id)
        fingerprint = (
            extension_store.runtime_package_content_fingerprint(record)
            if isinstance(record, dict)
            else None
        )
        fingerprints.append((extension_id, fingerprint))
    return tuple(fingerprints)


# browserHarness folded onto the extension identity for the browser-harness
# role (see extension_store.extension_id_for_role); no separate identity.
def _browser_harness_extension_id() -> str | None:
    return extension_store.extension_id_for_role("browser-harness")


def _extension_revision(record: dict[str, Any]) -> str:
    source = record.get("source") or {}
    return str(source.get("commit_sha") or record.get("version") or record.get("updated_at") or "")


def _setting_schema_hash(record: dict[str, Any], key: str) -> str:
    try:
        return harness_fields.setting_schema_hash(record, key)
    except harness_fields.HarnessFieldError as exc:
        raise HarnessProfileResolutionError(str(exc)) from exc


def _run_extension_record(extension_id: str) -> dict[str, Any] | None:
    """Second readiness read for a run snapshot, split by DURABILITY.

    `compute_default_profile` already gated this extension on
    `is_extension_runtime_ready`, so this is a re-read of the same fact and
    readiness can flip in between (auto-quarantine, entitlement expiry, an
    internal-LLM reassignment, the install-path window during an update).

    Returns None when the flip is DURABLE — the record vanished or its
    `enabled` flag went False. Neither recovers without a user re-enable or a
    new extension generation, so a retried turn would run identically degraded
    and the next default synthesis excludes the extension anyway. The caller
    drops the instance and surfaces the id instead of failing the turn.

    Raises when the flip is RECOVERABLE — still `enabled`, but not
    runtime-ready. Those restore themselves with no user action, so failing
    the turn is correct: the retry gets the COMPLETE harness instead of
    silently running once without this extension's instructions.
    """
    record = extension_store.get_extension(extension_id)
    if not record or record.get("enabled") is not True:
        return None
    if not extension_store.is_extension_runtime_ready(extension_id):
        reason = extension_store.runtime_not_ready_reason(extension_id) or "not_ready"
        raise HarnessProfileResolutionError(
            f"Harness profile requires enabled runtime-ready extension: {extension_id} ({reason})"
        )
    return record


def _extension_items(record: dict[str, Any]) -> dict[str, set[str]]:
    manifest = record.get("manifest") or {}
    entrypoints = manifest.get("entrypoints") or {}
    return {
        "instructions": {
            str(item.get("name") or "")
            for item in extension_instructions.instruction_items_from_entrypoints(entrypoints) or []
            if isinstance(item, dict) and item.get("name")
        },
        "skills": {
            str(item.get("name") or "")
            for item in entrypoints.get("skills") or []
            if isinstance(item, dict) and item.get("name")
        },
        "mcp": {
            str(item.get("name") or "")
            for item in extension_store.extension_mcp_servers(str(manifest.get("id") or ""))
            if isinstance(item, dict) and item.get("name")
        },
    }


# ---------------------------------------------------------------------------
# Default synthesis — aggregates LIVE state and is cached by owner-store
# fingerprints. The cache is disposable; profile/extension writes invalidate it.
# Mirrors the read logic the deleted `_package_current_harness_profile_payload`
# used to hand-assemble, and the same live reads turn_manager's no-profile
# fallback path already performs.
# ---------------------------------------------------------------------------
def compute_default_profile() -> dict[str, Any]:
    global _DEFAULT_PROFILE_CACHE
    key = _default_profile_cache_key()
    with _CACHE_LOCK:
        cached = _DEFAULT_PROFILE_CACHE
        if cached is not None and cached[0] == key:
            return copy.deepcopy(cached[1])
    profile = _compute_default_profile_uncached()
    with _CACHE_LOCK:
        _DEFAULT_PROFILE_CACHE = (key, copy.deepcopy(profile))
    return profile


def _compute_default_profile_uncached() -> dict[str, Any]:
    extension_instances: dict[str, Any] = {}
    for record in extension_store.list_extensions(include_hidden=True):
        manifest = record.get("manifest") or {}
        extension_id = str(manifest.get("id") or "").strip()
        if not extension_id or not extension_store.is_extension_runtime_ready(extension_id):
            continue
        instance: dict[str, Any] = {
            "mcp_servers": [
                item["name"]
                for item in extension_store.extension_mcp_servers(extension_id)
                if item.get("enabled")
            ],
            "skills": [
                item["name"]
                for item in extension_store.extension_runtime_skills(extension_id)
                if item.get("enabled")
            ],
            "instruction_names": [],
        }
        instruction_state = extension_instructions.normalize_state(record)
        if instruction_state.get("global"):
            for item in extension_instructions.instruction_items_from_entrypoints(manifest.get("entrypoints") or {}) or []:
                if isinstance(item, dict) and item.get("name"):
                    instance["instruction_names"].append(str(item["name"]))
        settings = extension_store.get_extension_settings(extension_id)
        overlays: dict[str, Any] = {}
        secret_refs: list[str] = []
        for item in settings.get("schema") or []:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            key = str(item["key"])
            if item.get("type") == "secret":
                if (settings.get("secret_present") or {}).get(key):
                    secret_refs.append(f"extension-setting:{extension_id}:{key}")
                continue
            overlays[key] = {
                "value": copy.deepcopy((settings.get("values") or {}).get(key)),
                "schema_hash": _setting_schema_hash(record, key),
            }
        instance["setting_overlays"] = overlays
        instance["secret_refs"] = secret_refs
        if extension_id == _browser_harness_extension_id():
            # "headless" is a normal browserHarness extension setting, written
            # via PATCH /api/extensions/{id}/settings like any other setting;
            # read it back from the same settings values instead of
            # hardcoding, so Default reflects the live setting.
            instance["headless"] = bool((settings.get("values") or {}).get("headless", True))
        extension_instances[extension_id] = instance

    instruction_sources: dict[str, Any] = {}
    for extension_id, instance in extension_instances.items():
        for name in instance["instruction_names"]:
            instruction_sources[name] = {"kind": "extension", "extension_id": extension_id}
        user_instructions = extension_store.get_user_instructions(extension_id).strip()
        if user_instructions:
            name = harness_fields.user_instruction_source_name(extension_id)
            instruction_sources[name] = {"kind": "inline", "content": user_instructions}

    return {
        "id": "default",
        "extension_instances": extension_instances,
        "disabled_builtin_tools": config_store.get_disabled_builtin_tools(),
        "disabled_builtin_extensions": config_store.get_disabled_builtin_extensions(),
        "disabled_runtime_skills": [],
        "instruction_sources": instruction_sources,
    }


def _apply_delta(default_list: list[str], delta: dict[str, Any] | None) -> tuple[list[str], bool]:
    was_overridden = delta is not None
    if not was_overridden:
        return list(default_list), False
    add = delta.get("add") or []
    remove = set(delta.get("remove") or [])
    merged = [item for item in default_list if item not in remove]
    for item in add:
        if item not in merged:
            merged.append(item)
    return merged, True


def _field(resolved: Any, override: Any | None) -> dict[str, Any]:
    return {"resolved": resolved, "override": override, "is_overridden": override is not None}


def _apply_extension_instance_override(
    default_instance: dict[str, Any], override: dict[str, Any] | None,
) -> dict[str, Any]:
    override = override or {}
    fields: dict[str, Any] = {}
    for leaf in ("mcp_servers", "skills", "instruction_names"):
        delta = override.get(leaf)
        merged, overridden = _apply_delta(default_instance.get(leaf) or [], delta)
        fields[leaf] = _field(merged, delta if overridden else None)

    default_overlays = default_instance.get("setting_overlays") or {}
    override_overlays = override.get("setting_overlays")
    overlay_fields: dict[str, Any] = {}
    for key, default_entry in default_overlays.items():
        if override_overlays and key in override_overlays:
            entry = override_overlays[key]
            overlay_fields[key] = _field(entry, entry)
        else:
            overlay_fields[key] = _field(default_entry, None)
    if override_overlays:
        for key, entry in override_overlays.items():
            if key not in overlay_fields:
                overlay_fields[key] = _field(entry, entry)
    fields["setting_overlays"] = overlay_fields

    if "headless" in override and override.get("headless") is not None:
        fields["headless"] = _field(bool(override["headless"]), bool(override["headless"]))
    else:
        fields["headless"] = _field(bool(default_instance.get("headless", False)), None)

    # secret_refs is a Default-only, non-overridable derived field (which
    # extension settings are secret-typed and currently populated).
    fields["secret_refs"] = _field(list(default_instance.get("secret_refs") or []), None)

    # extension_revision is a drift pin: this profile's own pin wins, else the
    # base carries it forward (so a child inherits a base's pin), else unset.
    # Live Default never pins — it always tracks live extension state.
    if "extension_revision" in override:
        pinned = override.get("extension_revision")
    else:
        pinned = default_instance.get("extension_revision")
    fields["extension_revision"] = _field(pinned, pinned)
    return fields


def _resolved_to_default_shape(resolved: dict[str, Any]) -> dict[str, Any]:
    """Flatten a fully-resolved profile's per-field {resolved, ...} snapshot
    back to the plain compute_default_profile() shape, so it can serve as the
    base a derived profile applies its own sparse deltas on top of. Reuses the
    exact leaves `_apply_extension_instance_override`/`_apply_delta` read."""
    extension_instances: dict[str, Any] = {}
    for extension_id, fields in resolved["extension_instances"].items():
        instance: dict[str, Any] = {
            "mcp_servers": list(fields["mcp_servers"]["resolved"]),
            "skills": list(fields["skills"]["resolved"]),
            "instruction_names": list(fields["instruction_names"]["resolved"]),
            "setting_overlays": {
                key: copy.deepcopy(entry["resolved"])
                for key, entry in (fields.get("setting_overlays") or {}).items()
            },
            "headless": bool(fields.get("headless", {}).get("resolved", False)),
            "secret_refs": list(fields.get("secret_refs", {}).get("resolved") or []),
        }
        pinned = fields.get("extension_revision", {}).get("resolved")
        if pinned:
            instance["extension_revision"] = pinned
        extension_instances[extension_id] = instance
    return {
        "id": resolved["id"],
        "extension_instances": extension_instances,
        "disabled_builtin_tools": list(resolved["disabled_builtin_tools"]["resolved"]),
        "disabled_builtin_extensions": list(resolved["disabled_builtin_extensions"]["resolved"]),
        "disabled_runtime_skills": list(resolved["disabled_runtime_skills"]["resolved"]),
        "instruction_sources": {
            name: copy.deepcopy(entry["resolved"])
            for name, entry in resolved["instruction_sources"].items()
        },
    }


def resolve_profile(
    profile_id: str,
    revision: str | None = None,
    default: dict[str, Any] | None = None,
    _chain: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Top-level entry point for the API layer (main.py's harness-profile
    endpoints). Returns a serializable per-field {resolved, override,
    is_overridden} snapshot for `_profile_response`/`_default_profile_response`.

    `default` lets a caller resolving many profiles in one request (e.g.
    `list_harness_profiles`) pass in an already-computed Default synthesis
    instead of triggering a fresh `compute_default_profile()` per profile.

    A profile with `base_profile_id` resolves that base first (recursively) and
    applies its own deltas on top of the base's resolved state; without one the
    base is the live Default synthesis. `_chain` guards against a base cycle at
    resolve time (the store also rejects one at save time).
    """
    if default is None:
        default = compute_default_profile()
    if profile_id != harness_profile_store.DEFAULT_PROFILE_ID and profile_id in _chain:
        raise HarnessProfileResolutionError(
            "harness profile base chain cycle: " + " -> ".join(_chain + (profile_id,))
        )
    base_pins = {
        "default_provider_id": None,
        "default_model": None,
        "default_reasoning_effort": None,
        "provisioning_prompt": None,
    }
    if profile_id == harness_profile_store.DEFAULT_PROFILE_ID:
        stored: dict[str, Any] | None = None
        overrides: dict[str, Any] = {}
        base_shape = default
    else:
        stored = harness_profile_store.get_profile(profile_id, revision)
        if not stored:
            # get_profile answers None for both "no such profile" and "the
            # pinned revision moved". Only the first is a 404 — a stale
            # revision is a conflict the caller resolves by reloading, so the
            # two must not collapse into one error.
            if harness_profile_store.get_profile(profile_id) is None:
                raise HarnessProfileMissingError("Harness profile is missing")
            raise HarnessProfileResolutionError("Harness profile's pinned revision is unavailable")
        overrides = stored.get("overrides") or {}
        base_profile_id = stored.get("base_profile_id")
        if base_profile_id:
            base_resolved = resolve_profile(
                base_profile_id,
                stored.get("base_profile_revision"),
                default=default,
                _chain=_chain + (profile_id,),
            )
            base_shape = _resolved_to_default_shape(base_resolved)
            for key in base_pins:
                base_pins[key] = base_resolved.get(key)
        else:
            base_shape = default

    override_instances = overrides.get("extension_instances") or {}
    extension_instances: dict[str, Any] = {}
    for extension_id, default_instance in base_shape["extension_instances"].items():
        extension_instances[extension_id] = _apply_extension_instance_override(
            default_instance, override_instances.get(extension_id)
        )
    # An override may reference an extension id that is not actually
    # live-enabled/runtime-ready in `default` (not installed, disabled, or
    # never existed). Such an extension is NOT part of the live base, so an
    # override on it must be a no-op for resolution purposes — it must
    # never cause the extension to appear "present" in extension_instances.
    # Presence here is a security-relevant signal (e.g. main.py's File Edit
    # and Browser Harness gates key off it), so a profile author must not be
    # able to conjure an absent extension into apparent existence merely by
    # referencing its id in an override.

    disabled_tools, tools_overridden = _apply_delta(
        base_shape["disabled_builtin_tools"], overrides.get("disabled_builtin_tools")
    )
    disabled_extensions, extensions_overridden = _apply_delta(
        base_shape["disabled_builtin_extensions"], overrides.get("disabled_builtin_extensions")
    )
    disabled_runtime_skills, runtime_skills_overridden = _apply_delta(
        base_shape.get("disabled_runtime_skills") or [], overrides.get("disabled_runtime_skills")
    )

    instruction_sources: dict[str, Any] = {}
    override_sources = overrides.get("instruction_sources") or {}
    for name, default_source in base_shape["instruction_sources"].items():
        if name in override_sources:
            instruction_sources[name] = _field(override_sources[name], override_sources[name])
        else:
            instruction_sources[name] = _field(default_source, None)
    for name, source in override_sources.items():
        if name not in instruction_sources:
            instruction_sources[name] = _field(source, source)

    return {
        "id": profile_id,
        "revision": (stored or {}).get("revision"),
        "name": (stored or {}).get("name"),
        "description": (stored or {}).get("description"),
        "created_at": (stored or {}).get("created_at"),
        "updated_at": (stored or {}).get("updated_at"),
        "base_profile_id": (stored or {}).get("base_profile_id"),
        "base_profile_revision": (stored or {}).get("base_profile_revision"),
        # Effective pins: this profile's own pin wins, else the base chain's.
        "default_provider_id": (stored or {}).get("default_provider_id") or base_pins["default_provider_id"],
        "default_model": (stored or {}).get("default_model") or base_pins["default_model"],
        "default_reasoning_effort": (stored or {}).get("default_reasoning_effort") or base_pins["default_reasoning_effort"],
        "provisioning_prompt": (stored or {}).get("provisioning_prompt") or base_pins["provisioning_prompt"],
        "extension_instances": extension_instances,
        "disabled_builtin_tools": _field(disabled_tools, overrides.get("disabled_builtin_tools") if tools_overridden else None),
        "disabled_builtin_extensions": _field(
            disabled_extensions, overrides.get("disabled_builtin_extensions") if extensions_overridden else None
        ),
        "disabled_runtime_skills": _field(
            disabled_runtime_skills,
            overrides.get("disabled_runtime_skills") if runtime_skills_overridden else None,
        ),
        "instruction_sources": instruction_sources,
        "mcp_overrides": copy.deepcopy((stored or {}).get("mcp_overrides") or {}),
        "skill_overrides": copy.deepcopy((stored or {}).get("skill_overrides") or {}),
        "native_harness_overrides": copy.deepcopy((stored or {}).get("native_harness_overrides") or {}),
        "provider_run_config_overlay": copy.deepcopy((stored or {}).get("provider_run_config_overlay") or {}),
        "secret_refs": copy.deepcopy((stored or {}).get("secret_refs") or {}),
        "capability_contexts": copy.deepcopy((stored or {}).get("capability_contexts") or []),
    }


def profile_selector_defaults(profile_id: str | None, revision: str | None = None) -> dict[str, str | None]:
    """The effective provider/model/reasoning-effort pins for a profile
    (inherited from its base chain when the profile itself pins none). Returns
    all-None for the Default profile or no profile — Default carries no pin."""
    empty = {"provider_id": None, "model": None, "reasoning_effort": None}
    selected = str(profile_id or "").strip()
    if not selected or selected == harness_profile_store.DEFAULT_PROFILE_ID:
        return empty
    resolved = resolve_profile(selected, str(revision or "").strip() or None)
    return {
        "provider_id": resolved.get("default_provider_id"),
        "model": resolved.get("default_model"),
        "reasoning_effort": resolved.get("default_reasoning_effort"),
    }


def merge_selector_defaults(
    explicit: dict[str, Any], profile_id: str | None, revision: str | None = None
) -> dict[str, str | None]:
    """Fill provider/model/reasoning_effort from a profile's pins ONLY where
    the caller omitted them (value is None). Explicit caller values always win;
    with no profile or no pin this is an identity over `explicit`."""
    pins = profile_selector_defaults(profile_id, revision)
    return {key: (explicit.get(key) if explicit.get(key) is not None else pins[key]) for key in pins}


def _instruction_blocks(
    instruction_sources: dict[str, dict[str, Any]],
    selected: dict[str, dict[str, Any]],
    skip_names: set[str],
) -> list[dict[str, str]]:
    """`skip_names` are sources whose owning extension was dropped from the run
    (see `_run_extension_record`): omit them instead of raising the
    not-selected error, so a durable mid-resolve disable reduces the harness
    rather than failing the turn."""
    blocks: list[dict[str, str]] = []
    seen: set[str] = set()
    for name, source in instruction_sources.items():
        if name in seen or name in skip_names:
            continue
        if source.get("kind") == "inline":
            seen.add(name)
            blocks.append({
                "source": "profile",
                "name": name,
                "content": source.get("content") or "",
                "providers": [],
            })
            continue
        extension_id = source.get("extension_id")
        if extension_id not in selected:
            raise HarnessProfileResolutionError(f"Instruction source extension is not selected: {extension_id}")
        record = selected[extension_id]["record"]
        install_root = extension_store.runtime_package_root_for_record(record)
        if install_root is None:
            raise HarnessProfileResolutionError(f"Extension package is unavailable: {extension_id}")
        for item in extension_instructions.instruction_items_from_entrypoints((record.get("manifest") or {}).get("entrypoints") or {}) or []:
            if not isinstance(item, dict) or item.get("name") != name:
                continue
            content_path = (install_root / item["path"]).resolve()
            root = install_root.resolve()
            if not content_path.is_relative_to(root) or not content_path.is_file():
                raise HarnessProfileResolutionError(f"Instruction path is unavailable: {extension_id}.{name}")
            seen.add(name)
            blocks.append({
                "source": extension_id,
                "name": name,
                "content": content_path.read_text(encoding="utf-8").strip(),
                # Manifest provider scope; empty means every provider kind.
                "providers": list(item.get("providers") or []),
            })
            break
        else:
            raise HarnessProfileResolutionError(f"Unknown extension instruction: {extension_id}.{name}")
    return [block for block in blocks if block.get("content")]


def _selected_instruction_sources(
    instruction_sources: dict[str, dict[str, Any]],
    extension_instruction_names: dict[str, list[str]],
    extension_all_instruction_names: dict[str, set[str]],
) -> dict[str, dict[str, Any]]:
    """The per-extension `instruction_names` selection is the authority for
    which extension-owned instruction sections a run injects; the inherited
    `instruction_sources` map only supplies the body override and the block
    order.

    Without this filter a named profile's deselection is cosmetic — the
    section keeps its inherited source entry and its text still reaches the
    provider — and a section its Default never enabled can never be added.
    """
    owner_by_name: dict[str, str] = {}
    for extension_id, names in extension_instruction_names.items():
        for name in names:
            owner_by_name[name] = extension_id
    deselected = {
        name
        for extension_id, names in extension_all_instruction_names.items()
        for name in names
        if name not in (extension_instruction_names.get(extension_id) or [])
    }

    out: dict[str, dict[str, Any]] = {}
    for name, source in instruction_sources.items():
        if not isinstance(source, dict):
            continue
        if source.get("kind") == "inline":
            owner = harness_fields.user_instruction_source_owner(name)
            if owner is not None and owner not in extension_instruction_names:
                continue
            if name in deselected:
                continue
            out[name] = source
            continue
        if owner_by_name.get(name) == source.get("extension_id"):
            out[name] = source
    for name, extension_id in owner_by_name.items():
        if name not in out:
            out[name] = {"kind": "extension", "extension_id": extension_id}
    return out


def _dropped_instruction_names(
    instruction_sources: dict[str, dict[str, Any]], dropped_extension_ids: list[str],
) -> set[str]:
    """Instruction-source keys owned by a dropped extension: its manifest
    instruction entrypoints AND its free-text user instructions (keyed by
    `harness_fields.user_instruction_source_name`, the single owner of that
    key). Injecting an absent extension's instructions would describe
    capabilities the run does not have, so both are omitted."""
    dropped = set(dropped_extension_ids)
    if not dropped:
        return set()
    names = {
        name
        for name, source in instruction_sources.items()
        if isinstance(source, dict)
        and source.get("kind") != "inline"
        and source.get("extension_id") in dropped
    }
    for extension_id in dropped:
        user_name = harness_fields.user_instruction_source_name(extension_id)
        if user_name in instruction_sources:
            names.add(user_name)
    return names


def _provider_context_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One capability context whose per-provider outputs each carry only the
    sections that apply to that provider kind.

    A manifest section may declare `providers` (validated by
    `extension_store`); this filter honors that scope on the run path so a
    provider-scoped section never reaches a provider outside its scope. An
    empty/absent list means every provider kind.
    """
    if not blocks:
        return []
    outputs: list[dict[str, Any]] = []
    for kind in provider_kinds.all_provider_kinds():
        scoped = [
            block for block in blocks
            if not block.get("providers") or kind in block["providers"]
        ]
        if not scoped:
            continue
        outputs.append({
            "provider_kind": kind,
            "provider_name": "",
            "content_kind": "instructions",
            "content": "\n\n".join(f"## {block['name']}\n\n{block['content']}" for block in scoped),
        })
    if not outputs:
        return []
    return [{
        "source_id": "harness_profile:instructions",
        "capability_id": "harness_profile:instructions",
        "name": "Harness Profile Instructions",
        "category": "harness_profile",
        "outputs": outputs,
    }]


def _skill_entries(selected: dict[str, dict[str, Any]]) -> dict[str, str]:
    entries: dict[str, str] = {}
    owners: dict[str, str] = {}
    for extension_id, item in selected.items():
        record = item["record"]
        instance = item["instance"]
        requested = set(instance.get("skills") or [])
        if not requested:
            continue
        install_root = extension_store.runtime_package_root_for_record(record)
        if install_root is None:
            raise HarnessProfileResolutionError(f"Extension package is unavailable: {extension_id}")
        root = install_root.resolve()
        for skill in (record.get("manifest") or {}).get("entrypoints", {}).get("skills") or []:
            if not isinstance(skill, dict) or skill.get("name") not in requested:
                continue
            skill_name = str(skill["name"])
            owner = owners.get(skill_name)
            if owner and owner != extension_id:
                raise HarnessProfileResolutionError(f"Duplicate selected skill name: {skill_name}")
            skill_dir = (root / str(skill.get("path") or "")).resolve()
            skill_md = skill_dir / "SKILL.md"
            if not skill_dir.is_relative_to(root) or not skill_md.is_file():
                raise HarnessProfileResolutionError(f"Skill path is unavailable: {extension_id}.{skill_name}")
            entries[skill_name] = skill_md.read_text(encoding="utf-8")
            owners[skill_name] = extension_id
    return entries


def _normalize_capability_contexts(value: Any) -> list[dict[str, Any]]:
    try:
        return capability_contexts_mod.normalize_capability_contexts(value)
    except ValueError as exc:
        raise HarnessProfileResolutionError(str(exc)) from exc


def resolve_for_session(
    session: dict[str, Any] | None,
    *,
    profile_id: str | None = None,
    turn_capability_contexts: list[dict] | None = None,
) -> dict[str, Any] | None:
    session = session or {}
    # No explicit profile on the call or the session means "use the default
    # profile", same as `create_session`'s own `harness_profile_id or
    # DEFAULT_PROFILE_ID` substitution -- not "no profile resolution at all".
    # Returning None here (as before) makes every turn's
    # `resolved_harness_run_config` collapse to `{}`, which empties
    # `launcher_projection.extension_mcp_servers` and drops every extension's
    # MCP servers from every session, harness-profile-selected or not.
    selected_id = str(
        profile_id or session.get("harness_profile_id") or harness_profile_store.DEFAULT_PROFILE_ID
    ).strip()
    if not selected_id:
        return None

    base_snapshot = session.get("harness_profile_snapshot")
    temporal_snapshot = session.get("turn_harness_profile_snapshot")

    # A stored snapshot is valid only for the profile it resolved. Explicit
    # reassignment (including back to default) must resolve the newly selected
    # profile instead of retaining the previous profile's capabilities.
    if (
        isinstance(base_snapshot, dict)
        and str(base_snapshot.get("profile_id") or "").strip() == selected_id
    ):
        if temporal_snapshot:
            merged = merge_profile_snapshots(base_snapshot, temporal_snapshot)
        else:
            merged = base_snapshot

        merged = _enforce_cross_provider_parity(merged)
        return merged

    active_capability_ids = tuple(
        str(item)
        for item in session.get("active_capability_ids") or []
        if str(item or "").strip()
    )
    # No revision in the key: a session names a profile, never a revision of
    # it, and every profile mutation clears this cache outright
    # (`invalidate_cache`), so a stale entry cannot survive an edit.
    cache_key = (
        _default_profile_cache_key(),
        selected_id,
        bool(session.get("bare_config")),
        active_capability_ids,
        _json_cache_token(turn_capability_contexts or []),
    )
    cached = _run_snapshot_cache_get(cache_key)
    if cached is not None:
        return cached
    resolved = resolve_profile(selected_id)

    selected: dict[str, dict[str, Any]] = {}
    extension_mcp_servers: dict[str, list[str]] = {}
    extension_skills: dict[str, list[str]] = {}
    extension_instruction_names: dict[str, list[str]] = {}
    extension_all_instruction_names: dict[str, set[str]] = {}
    extension_revisions: dict[str, str] = {}
    extension_setting_overlays: dict[str, dict[str, Any]] = {}
    secret_refs: dict[str, list[str]] = {}
    dropped_extension_ids: list[str] = []
    disabled_builtin_extensions = list(
        resolved["disabled_builtin_extensions"]["resolved"]
    )
    disabled_extension_ids = set(disabled_builtin_extensions)
    for extension_id, fields in resolved["extension_instances"].items():
        if extension_id in disabled_extension_ids:
            continue
        mcp_servers = fields["mcp_servers"]["resolved"]
        skills = fields["skills"]["resolved"]
        instruction_names = fields["instruction_names"]["resolved"]
        overlay_fields = fields.get("setting_overlays") or {}
        if not (mcp_servers or skills or instruction_names or overlay_fields):
            continue
        record = _run_extension_record(extension_id)
        if record is None:
            dropped_extension_ids.append(extension_id)
            continue
        actual_revision = _extension_revision(record)
        # The drift check is scoped ONLY to instances that carry an explicit
        # pinned extension_revision override; inherited (unpinned) instances
        # never raise here.
        expected_revision = fields.get("extension_revision", {}).get("override")
        if expected_revision and actual_revision and str(expected_revision) != actual_revision:
            raise HarnessProfileResolutionError(f"Extension revision changed: {extension_id}")
        items = _extension_items(record)
        for server_name in mcp_servers:
            if server_name not in items["mcp"]:
                raise HarnessProfileResolutionError(f"Unknown extension MCP server: {extension_id}.{server_name}")
        for skill_name in skills:
            if skill_name not in items["skills"]:
                raise HarnessProfileResolutionError(f"Unknown extension skill: {extension_id}.{skill_name}")
        for instruction_name in instruction_names:
            if instruction_name not in items["instructions"]:
                raise HarnessProfileResolutionError(f"Unknown extension instruction: {extension_id}.{instruction_name}")
        selected[extension_id] = {"record": record, "instance": {"skills": skills}}
        extension_revisions[extension_id] = actual_revision
        extension_mcp_servers[extension_id] = list(mcp_servers)
        extension_skills[extension_id] = list(skills)
        extension_instruction_names[extension_id] = list(instruction_names)
        extension_all_instruction_names[extension_id] = set(items["instructions"])
        settings: dict[str, Any] = {}
        for key, entry in overlay_fields.items():
            item = entry["resolved"]
            expected = str((item or {}).get("schema_hash") or "")
            actual = _setting_schema_hash(record, key)
            if expected and expected != actual:
                raise HarnessProfileResolutionError(f"Extension setting schema changed: {extension_id}.{key}")
            settings[key] = item
        if settings:
            extension_setting_overlays[extension_id] = settings
        live_secret_refs = fields.get("secret_refs", {}).get("resolved") or []
        if live_secret_refs:
            secret_refs[extension_id] = list(live_secret_refs)
    secret_refs.update({
        extension_id: copy.deepcopy(refs)
        for extension_id, refs in (resolved.get("secret_refs") or {}).items()
        if extension_id not in disabled_extension_ids
    })
    inherited_instruction_sources = {
        name: entry["resolved"] for name, entry in resolved["instruction_sources"].items()
    }
    instruction_sources = _selected_instruction_sources(
        inherited_instruction_sources, extension_instruction_names, extension_all_instruction_names
    )
    # Reported against the INHERITED map: a dropped extension's sections never
    # survive selection, so computing this from the filtered map would report
    # an empty drop set for exactly the case it exists to surface.
    dropped_instruction_names = _dropped_instruction_names(
        inherited_instruction_sources, dropped_extension_ids
    )
    instruction_blocks = _instruction_blocks(instruction_sources, selected, dropped_instruction_names)
    profile_contexts = _provider_context_blocks(instruction_blocks)
    profile_contexts.extend(_normalize_capability_contexts(resolved.get("capability_contexts")))
    if turn_capability_contexts:
        profile_contexts.extend(turn_capability_contexts)
    provider_run_config = copy.deepcopy(resolved.get("provider_run_config_overlay") or {})
    skill_entries = _skill_entries(selected)
    if skill_entries:
        provider_run_config["skills"] = {
            **copy.deepcopy(provider_run_config.get("skills") or {}),
            **skill_entries,
        }
    snapshot = {
        "profile_id": resolved["id"],
        "profile_revision": resolved.get("revision") or "",
        "profile_name": resolved.get("name") or resolved["id"],
        # base_mode was removed from the profile schema in v2 (section 1);
        # "bare" is now purely a session-level concern.
        "bare_config": bool(session.get("bare_config")),
        "capability_contexts": profile_contexts,
        "provider_run_config": provider_run_config,
        "extra_mcp_servers": sorted({server for servers in extension_mcp_servers.values() for server in servers}),
        "active_capability_ids": list(active_capability_ids),
        "disabled_builtin_tools": list(resolved["disabled_builtin_tools"]["resolved"]),
        "disabled_builtin_extensions": disabled_builtin_extensions,
        "disabled_runtime_skills": list(resolved["disabled_runtime_skills"]["resolved"]),
        "extension_revisions": extension_revisions,
        "extension_mcp_servers": extension_mcp_servers,
        "extension_skills": extension_skills,
        "extension_instruction_names": extension_instruction_names,
        "extension_setting_overlays": extension_setting_overlays,
        "secret_refs": secret_refs,
        "instruction_blocks": instruction_blocks,
        # Extensions the default synthesis selected but that were durably
        # unavailable by the time this run resolved (see
        # `_run_extension_record`). Surfaced so the run truthfully reports the
        # reduced MCP/skill/instruction set instead of degrading silently.
        "dropped_extension_ids": sorted(dropped_extension_ids),
        "dropped_instruction_names": sorted(dropped_instruction_names),
    }
    snapshot["launcher_projection"] = {
        "profile_id": snapshot["profile_id"],
        "profile_revision": snapshot["profile_revision"],
        "extension_selection_authoritative": True,
        "bare_config": snapshot["bare_config"],
        "extension_revisions": extension_revisions,
        "extension_mcp_servers": extension_mcp_servers,
        "extension_setting_overlays": snapshot["extension_setting_overlays"],
        "secret_refs": snapshot["secret_refs"],
        "disabled_builtin_extensions": snapshot["disabled_builtin_extensions"],
        "disabled_builtin_tools": snapshot["disabled_builtin_tools"],
        "disabled_runtime_skills": snapshot["disabled_runtime_skills"],
        "active_capability_ids": snapshot["active_capability_ids"],
    }
    if dropped_extension_ids:
        # A degraded snapshot is an artifact of live state at this instant, not
        # a function of the cache key (store_fingerprint's 0.5s TTL can hide a
        # re-enable), so it is never cached — the next resolve re-reads.
        return snapshot
    return _run_snapshot_cache_put(cache_key, snapshot)


def merge_profile_snapshots(
    base: dict,  # session harness_profile_snapshot
    temporal: dict | None  # turn_harness_profile_snapshot
) -> dict:
    """Merge temporal profile into base profile for turn execution.

    Turn-level capabilities are additive to session-level capabilities.
    For conflicts, temporal capabilities take precedence.
    """
    if not temporal:
        return base

    # Start with base as foundation
    merged = copy.deepcopy(base)

    # Merge capability contexts (additive)
    base_capability_contexts = list(merged.get("capability_contexts") or [])
    temporal_capability_contexts = list(temporal.get("capability_contexts") or [])

    # Dedupe by source_id, temporal wins
    capability_contexts_by_source = {}
    for ctx in base_capability_contexts:
        capability_contexts_by_source[ctx.get("source_id")] = ctx
    for ctx in temporal_capability_contexts:
        capability_contexts_by_source[ctx.get("source_id")] = ctx

    merged["capability_contexts"] = list(capability_contexts_by_source.values())

    # Merge extension instances (additive MCP servers, skills, instructions)
    base_extension_instances = dict(merged.get("extension_instances") or {})
    temporal_extension_instances = dict(temporal.get("extension_instances") or {})

    for ext_id, temporal_instance in temporal_extension_instances.items():
        if ext_id not in base_extension_instances:
            # New extension from temporal profile
            base_extension_instances[ext_id] = temporal_instance
        else:
            # Merge existing extension
            base_instance = base_extension_instances[ext_id]

            # Merge MCP servers (additive)
            base_mcps = set(base_instance.get("mcp_servers") or [])
            temporal_mcps = set(temporal_instance.get("mcp_servers") or [])
            base_instance["mcp_servers"] = list(base_mcps | temporal_mcps)

            # Merge skills (additive)
            base_skills = set(base_instance.get("skills") or [])
            temporal_skills = set(temporal_instance.get("skills") or [])
            base_instance["skills"] = list(base_skills | temporal_skills)

            # Merge instruction names (additive)
            base_instructions = set(base_instance.get("instruction_names") or [])
            temporal_instructions = set(temporal_instance.get("instruction_names") or [])
            base_instance["instruction_names"] = list(base_instructions | temporal_instructions)

            # Merge settings overlays (temporal wins for conflicts)
            base_overlays = dict(base_instance.get("setting_overlays") or {})
            temporal_overlays = dict(temporal_instance.get("setting_overlays") or {})
            base_overlays.update(temporal_overlays)
            base_instance["setting_overlays"] = base_overlays

    merged["extension_instances"] = base_extension_instances

    # Provider transport consumes this canonical resolved map. Temporal
    # settings override base settings per extension and key.
    extension_setting_overlays = copy.deepcopy(
        merged.get("extension_setting_overlays") or {}
    )
    for extension_id, settings in (
        temporal.get("extension_setting_overlays") or {}
    ).items():
        current = dict(extension_setting_overlays.get(extension_id) or {})
        current.update(copy.deepcopy(settings or {}))
        extension_setting_overlays[extension_id] = current
    merged["extension_setting_overlays"] = extension_setting_overlays

    launcher_projection = copy.deepcopy(merged.get("launcher_projection") or {})
    launcher_projection["extension_setting_overlays"] = copy.deepcopy(
        extension_setting_overlays
    )
    merged["launcher_projection"] = launcher_projection

    # Merge disabled lists. Disabled = restriction. A turn profile may only
    # ADD restrictions on top of the session profile, never relax one: a tool
    # the session disabled stays disabled regardless of the turn overlay.
    # Union (not intersection) preserves the session-level boundary.
    base_disabled_tools = set(merged.get("disabled_builtin_tools") or [])
    temporal_disabled_tools = set(temporal.get("disabled_builtin_tools") or [])
    merged["disabled_builtin_tools"] = list(base_disabled_tools | temporal_disabled_tools)

    base_disabled_extensions = set(merged.get("disabled_builtin_extensions") or [])
    temporal_disabled_extensions = set(temporal.get("disabled_builtin_extensions") or [])
    merged["disabled_builtin_extensions"] = list(base_disabled_extensions | temporal_disabled_extensions)

    base_disabled_skills = set(merged.get("disabled_runtime_skills") or [])
    temporal_disabled_skills = set(temporal.get("disabled_runtime_skills") or [])
    merged["disabled_runtime_skills"] = list(base_disabled_skills | temporal_disabled_skills)

    # Merge provider run config (temporal wins)
    base_provider_config = dict(merged.get("provider_run_config") or {})
    temporal_provider_config = dict(temporal.get("provider_run_config") or {})

    for key in ["skills", "mcp_overrides", "skill_overrides", "native_harness_overrides"]:
        if key in temporal_provider_config:
            base_provider_config[key] = temporal_provider_config[key]

    merged["provider_run_config"] = base_provider_config

    # Merge extra MCP servers (additive)
    base_extra_mcps = set(merged.get("extra_mcp_servers") or [])
    temporal_extra_mcps = set(temporal.get("extra_mcp_servers") or [])
    merged["extra_mcp_servers"] = list(base_extra_mcps | temporal_extra_mcps)

    # Preserve temporal metadata
    merged["temporal_profile_id"] = temporal.get("profile_id")
    merged["temporal_profile_revision"] = temporal.get("revision")

    return merged
