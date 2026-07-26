"""Adoption helpers for a state home that has no installation profile.

A home without a profile can serve nothing, which is the correct state right
after `BETTER_AGENT_HOME` points somewhere new. Adoption resolves the one
question setup would have asked — which provider — from what is already on the
machine, so a fresh home boots usable without a network install.
"""

from __future__ import annotations

import json

import provider_setup
from cli_paths import resolve_cli_binary
from paths import bc_home


def configured_provider_kind() -> str | None:
    """The provider kind this home's config already prefers, if any."""
    try:
        state = json.loads((bc_home() / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    providers = state.get("providers")
    default_id = state.get("default_provider_id")
    if not isinstance(providers, list):
        return None
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if provider.get("id") != default_id:
            continue
        kind = provider.get("kind")
        return kind if isinstance(kind, str) and kind else None
    return None


def adoptable_provider_kind() -> str | None:
    """An installable provider whose CLI is already present on this machine.

    Prefers the kind this home is already configured for; otherwise takes the
    first installable kind that resolves, in manifest order.
    """
    installable = list(provider_setup.supported_provider_kinds())
    preferred = configured_provider_kind()
    ordered = (
        [preferred, *(k for k in installable if k != preferred)]
        if preferred in installable
        else installable
    )
    for kind in ordered:
        command = provider_setup.installer_for(kind).command
        if resolve_cli_binary(command, respect_installation_profile=False):
            return kind
    return None
