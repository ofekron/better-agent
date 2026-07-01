#!/usr/bin/env python3
"""Locks the Internal LLM settings contract for runtime readiness:
a task left on 'Inherit' (no explicit per-task assignment) is READY when a
default provider exists, because it resolves to that provider — matching
config_store.resolve_internal_llm and the internal-LLM consumers. Only a
state with no resolvable provider at all is runtime-unready (fail-closed).

Regression for: get-requirements MCP being withheld while requirement_analysis
was on Inherit, even though a default provider was configured.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-test-internal-llm-readiness-"))
import _test_home
_test_home.isolate("ba-test-")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config_store  # noqa: E402
import extension_store as es  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS" if cond else "FAIL"), label)
    if not cond:
        FAILURES.append(label)


def _requirements_record() -> dict:
    install_root = TMP_HOME / "req-install"
    (install_root / "requirement_analysis").mkdir(parents=True, exist_ok=True)
    return {
        "manifest": {"id": es.BUILTIN_REQUIREMENTS_EXTENSION_ID},
        "source": {"install_path": str(install_root)},
    }


def _set_provider_fields(provider_id: str, **fields) -> None:
    state = config_store._load_state()  # type: ignore[attr-defined]
    for provider in state.get("providers", []):
        if provider.get("id") == provider_id:
            provider.update(fields)
            config_store._save_state(state)  # type: ignore[attr-defined]
            return
    raise AssertionError(f"provider {provider_id} not found")


def run() -> None:
    # Fresh home seeds a default Claude provider; requirement_analysis is unset.
    check(
        config_store.get_internal_llm_task("requirement_analysis") == {},
        "requirement_analysis starts on Inherit (no explicit assignment)",
    )
    check(
        config_store.get_default_provider() is not None,
        "a default provider exists (Inherit is resolvable)",
    )
    default_provider_id = config_store.list_providers()["default_provider_id"]
    _set_provider_fields(default_provider_id, mode="api_key")
    orig_read_api_key = config_store._read_api_key  # type: ignore[attr-defined]

    def fail_read_api_key(_provider_id: str) -> str:
        raise AssertionError("resolve_internal_llm must not read provider api keys")

    config_store._read_api_key = fail_read_api_key  # type: ignore[attr-defined]

    # The exact unit the bug lived in: Inherit must resolve to the default
    # provider and count as ready. Pre-fix this returned False.
    try:
        resolved_default = config_store.resolve_internal_llm("requirement_analysis")
        check(
            resolved_default.get("provider_id") == default_provider_id
            and bool(resolved_default.get("model")),
            "Inherit task resolves through api-key default without reading key",
        )
        check(
            es._internal_llm_task_ready("requirement_analysis") is True,
            "Inherit task is ready via default-provider fallback",
        )
    finally:
        config_store._read_api_key = orig_read_api_key  # type: ignore[attr-defined]

    # The assistant extension's board analyzer resolves from a dedicated
    # `assistant` internal-LLM task (its own settings row), registered as a
    # known task and ready on Inherit via the same default-provider fallback.
    check(
        "assistant" in config_store.internal_llm_tasks(),
        "assistant is a registered internal-LLM task",
    )
    for task in (
        "delegation_task",
        "delegation_message",
        "delegation_ask",
        "delegation_session_bridge",
        "extension_context_audit",
    ):
        check(
            task in config_store.internal_llm_tasks(),
            f"{task} is a registered internal-LLM task",
        )
    harness_record = {"manifest": {"id": es.BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID}}
    check(
        es.extension_internal_llm_tasks(harness_record) == ["extension_context_audit"],
        "harness extension owns extension_context_audit task",
    )
    ask_record = {"manifest": {"id": es.BUILTIN_ASK_EXTENSION_ID}}
    check(
        es.extension_internal_llm_tasks(ask_record) == [],
        "non-harness extension LLM tasks stay in global settings",
    )
    assistant_resolved = config_store.resolve_internal_llm("assistant")
    check(
        bool(assistant_resolved.get("provider_id")) and bool(assistant_resolved.get("model")),
        "assistant task resolves to a concrete provider + model on Inherit",
    )
    check(
        es._internal_llm_task_ready("assistant") is True,
        "assistant task is ready via default-provider fallback",
    )
    api_provider = config_store.add_provider({
        "name": "api-key-assigned",
        "kind": "codex",
        "mode": "subscription",
        "default_model": "gpt-test",
    })
    api_provider_id = api_provider["id"]
    _set_provider_fields(api_provider_id, mode="api_key")
    config_store.set_internal_llm_assignments({
        "requirement_analysis": {
            "provider_id": api_provider_id,
            "model": "",
            "reasoning_effort": "",
        }
    })
    orig_read_api_key = config_store._read_api_key  # type: ignore[attr-defined]
    config_store._read_api_key = fail_read_api_key  # type: ignore[attr-defined]
    try:
        assigned = config_store.resolve_internal_llm("requirement_analysis")
        check(
            assigned.get("provider_id") == api_provider_id and assigned.get("model") == "gpt-test",
            "assigned api-key provider resolves without reading key",
        )
    finally:
        config_store._read_api_key = orig_read_api_key  # type: ignore[attr-defined]
    _set_provider_fields(api_provider_id, suspended=True)
    suspended_fallback = config_store.resolve_internal_llm("requirement_analysis")
    check(
        suspended_fallback.get("provider_id") == default_provider_id,
        "suspended assigned provider falls back to default provider",
    )
    config_store.set_internal_llm_assignments({
        "requirement_analysis": {
            "provider_id": "missing-provider",
            "model": "",
            "reasoning_effort": "",
        }
    })
    missing_fallback = config_store.resolve_internal_llm("requirement_analysis")
    check(
        missing_fallback.get("provider_id") == default_provider_id,
        "missing assigned provider falls back to default provider",
    )
    state = config_store._load_state()  # type: ignore[attr-defined]
    default_raw = next(
        provider for provider in state.get("providers", [])
        if provider.get("id") == default_provider_id
    )
    options = config_store.reasoning_effort_options_for_provider(default_raw)
    chosen_effort = options[0] if options else "xhigh"
    config_store.set_internal_llm_assignments({
        "assistant": {
            "provider_id": default_provider_id,
            "model": "",
            "reasoning_effort": chosen_effort,
        }
    })
    reasoning = config_store.resolve_internal_llm("assistant")
    expected_effort = chosen_effort if chosen_effort in options else ""
    check(
        reasoning.get("reasoning_effort") == expected_effort,
        "resolver preserves reasoning_effort helper semantics",
    )
    config_store.set_internal_llm_assignments({})

    record = _requirements_record()
    orig_surface = es._record_backend_surface_ready
    es._record_backend_surface_ready = lambda _r: True  # isolate the LLM branch
    try:
        check(
            es._record_runtime_ready(record) is True,
            "requirements record runtime-ready with Inherit + default provider",
        )

        # Fail-closed: no resolvable provider/model -> unready.
        orig_resolve = config_store.resolve_internal_llm
        config_store.resolve_internal_llm = lambda _k: {"provider_id": "", "model": ""}
        try:
            check(
                es._internal_llm_task_ready("requirement_analysis") is False,
                "no resolvable provider -> task not ready (fail-closed)",
            )
            check(
                es._record_runtime_ready(record) is False,
                "no resolvable provider -> requirements record not runtime-ready",
            )
        finally:
            config_store.resolve_internal_llm = orig_resolve

        # Fail-closed: a provider resolves but has no model -> unready.
        config_store.resolve_internal_llm = lambda _k: {"provider_id": "p1", "model": ""}
        try:
            check(
                es._internal_llm_task_ready("requirement_analysis") is False,
                "resolved provider with empty model -> task not ready (fail-closed)",
            )
        finally:
            config_store.resolve_internal_llm = orig_resolve
    finally:
        es._record_backend_surface_ready = orig_surface


if __name__ == "__main__":
    try:
        run()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        sys.exit(1)
    print("\nALL PASSED")
