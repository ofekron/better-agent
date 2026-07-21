from __future__ import annotations

import os
import sys

import _test_home

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_test_home.isolate("bc-test-provider-catalog-mcp-")

import config_store  # noqa: E402
from provider_catalog_mcp import available_provider_models_response  # noqa: E402


def _names(result: dict) -> set[str]:
    return {provider["name"] for provider in result["providers"]}


def test_returns_all_non_suspended_providers() -> None:
    suspended = config_store.add_provider({
        "name": "Suspended Selector",
        "kind": "claude",
        "mode": "subscription",
        "default_model": "suspended-model",
        "custom_models": ["suspended-custom"],
        "suspended": True,
    })

    result = available_provider_models_response()

    assert result["success"] is True
    assert result["count"] >= 2
    assert "Claude" in _names(result)
    assert "Codex" in _names(result)
    assert suspended["name"] not in _names(result)
    assert all("runner" in provider for provider in result["providers"])


def test_fuzzy_provider_model_effort_and_runner_filters() -> None:
    config_store.add_provider({
        "name": "Router Lab",
        "kind": "openai",
        "mode": "api_key",
        "runner": "better_agent_runner",
        "default_model": "router-default",
        "custom_models": ["custom-turbo-model"],
        "default_reasoning_effort": "high",
    })

    provider_result = available_provider_models_response(provider="ruter")
    assert _names(provider_result) == {"Router Lab"}

    model_result = available_provider_models_response(model="turbo")
    assert _names(model_result) == {"Router Lab"}
    assert model_result["providers"][0]["models"] == ["custom-turbo-model"]

    effort_result = available_provider_models_response(
        provider="codx",
        reasoning_effort="xhig",
    )
    assert _names(effort_result) == {"Codex"}
    assert "xhigh" in effort_result["providers"][0]["reasoning_efforts"]

    runner_result = available_provider_models_response(
        provider="ruter",
        runner="better agent",
    )
    assert _names(runner_result) == {"Router Lab"}
    assert runner_result["providers"][0]["runner"] == "better_agent_runner"
    assert runner_result["filters"]["runner"] == "better agent"


def main() -> int:
    test_returns_all_non_suspended_providers()
    test_fuzzy_provider_model_effort_and_runner_filters()
    print("provider catalog MCP: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
