from __future__ import annotations

import _test_home

_test_home.isolate("bc-test-provider-catalog-provisioning-")

import config_store
import pytest


def _payload(model: str = "glm-5.2") -> dict:
    return {
        "default_provider_id": "zai",
        "providers": [{
            "id": "zai",
            "name": "Z.AI",
            "kind": "openai",
            "runner": "better_agent_runner",
            "mode": "api_key",
            "base_url": "https://api.z.ai/api/coding/paas/v4",
            "custom_models": [model],
            "default_model": model,
            "default_reasoning_effort": "medium",
            "allowed_sinks": ["local-keychain:testape"],
            "capabilities": {},
            "suspended": False,
        }],
        "internal_llm": {
            "default_session": {
                "provider_id": "zai",
                "model": model,
            },
        },
    }


def test_catalog_provisioning_initializes_fresh_home_once():
    first = config_store.provision_provider_catalog(_payload())
    first_provider = first["providers"][0]
    assert first_provider["revision"] == 0
    persisted = config_store._load_state()
    assert persisted["provider_state_projected"] is True
    assert "delegate_task_policy" in persisted

    with pytest.raises(config_store.ProviderStateConflict) as conflict:
        config_store.provision_provider_catalog(_payload())
    assert conflict.value.reason == "already_initialized"


def test_catalog_provisioning_refuses_user_owned_state():
    config_store._config_path().unlink(missing_ok=True)
    config_store._state_cache = None
    config_store.list_providers()

    with pytest.raises(config_store.ProviderStateConflict) as conflict:
        config_store.provision_provider_catalog(_payload())

    assert conflict.value.reason == "already_initialized"
