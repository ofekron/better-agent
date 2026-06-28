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

    # The exact unit the bug lived in: Inherit must resolve to the default
    # provider and count as ready. Pre-fix this returned False.
    check(
        es._internal_llm_task_ready("requirement_analysis") is True,
        "Inherit task is ready via default-provider fallback",
    )

    # The assistant extension's board analyzer resolves from a dedicated
    # `assistant` internal-LLM task (its own settings row), registered as a
    # known task and ready on Inherit via the same default-provider fallback.
    check(
        "assistant" in config_store.INTERNAL_LLM_TASKS,
        "assistant is a registered internal-LLM task",
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
