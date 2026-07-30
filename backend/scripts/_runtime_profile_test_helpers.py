"""Test-only helpers for the runtime-profile activation surface.

Provider-level set-default is gone; tests that used to switch the active
provider now activate a runtime profile of that provider, creating the
provider's profile first when the fixture never made one."""

from __future__ import annotations


def activate_provider(provider_id: str) -> dict:
    """Activate (creating if needed) the given provider's preferred profile."""
    import config_store

    defaults = config_store.provider_execution_defaults(provider_id)
    profile_id = defaults["runtime_profile_id"]
    if profile_id is None:
        created = config_store.add_runtime_profile(
            {"provider_id": provider_id, "runner": defaults["runner"]}
        )
        profile_id = created["id"]
    activated = config_store.activate_runtime_profile(profile_id)
    assert activated is not None
    return activated
