#!/usr/bin/env python3
"""Locks the declaration-driven internal-LLM task contract:

An extension declares its internal-LLM task keys via
``permissions.internal_llm_tasks`` in its manifest. Those keys are the single
source of truth that (a) the manifest validator accepts, (b) surface as an
override row in that extension's own settings, and (c) gate the extension's
runtime readiness — defaulting to Inherit (active provider), overridable
per-extension.

Regression for: auto-tagging silently used ``default_session`` and had no
override row in its own settings, and the readiness gate did not even detect
it needed an LLM.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import _test_home
_test_home.isolate("ba-test-internal-llm-declaration-")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import extension_store as es  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS" if cond else "FAIL"), label)
    if not cond:
        FAILURES.append(label)


def main() -> None:
    # 1. Manifest validator accepts internal_llm_tasks and normalizes the list.
    validated = es._validate_permissions({
        "backend_routes": True,
        "internal_llm_tasks": ["auto_tagging", "auto_tagging"],
    })
    check(
        validated.get("internal_llm_tasks") == ["auto_tagging", "auto_tagging"],
        "permissions.internal_llm_tasks accepted as a string list",
    )

    # 2. Invalid task keys (uppercase / dots) are rejected.
    rejected = False
    try:
        es._validate_permissions({"internal_llm_tasks": ["Bad-Key", "ok_one"]})
    except es.ExtensionError:
        rejected = True
    check(rejected, "permissions.internal_llm_tasks rejects non-snake-case keys")

    # 3. Declared tasks are discovered on the extension's settings surface.
    record = {
        "manifest": {
            "id": "some.extension",
            "permissions": {"internal_llm_tasks": ["custom_task"]},
        },
    }
    check(
        es.extension_internal_llm_tasks(record) == ["custom_task"],
        "declared task surfaces in extension settings",
    )
    check(
        es.extension_provisioned_internal_llm_tasks(record) == ["custom_task"],
        "declared task gates extension runtime readiness",
    )

    # 4. Declared tasks flow into the global known-task set (normalization keeps
    #    assignments for them instead of dropping as unknown) once the extension
    #    is installed. Stub list_extensions to stand in for an install.
    original_list = es.list_extensions

    def _list_with_record() -> list[dict]:
        return [*original_list(), record]

    es.list_extensions = _list_with_record  # type: ignore[assignment]
    try:
        check(
            "custom_task" in es.all_internal_llm_task_keys(),
            "declared task is a known internal-LLM task key once installed",
        )
        check(
            "custom_task" in es.extension_internal_llm_task_keys(),
            "declared task is extension-owned once installed",
        )
    finally:
        es.list_extensions = original_list  # type: ignore[assignment]

    # 5. The real auto-tagging extension declares a dedicated task and its
    #    selector spec resolves through it (not default_session).
    private_root = ROOT.parent / "better-agent-private" / "extensions" / "auto-tagging"
    manifest_path = private_root / "better-agent-extension.json"
    spec_path = private_root / "tagging_selector" / "__init__.py"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        declared = (manifest.get("permissions") or {}).get("internal_llm_tasks") or []
        check(
            "auto_tagging" in declared,
            "auto-tagging manifest declares the auto_tagging internal-LLM task",
        )
        auto_record = {"manifest": manifest}
        check(
            es.extension_internal_llm_tasks(auto_record) == ["auto_tagging"],
            "auto-tagging exposes exactly the auto_tagging override row",
        )
    else:
        print("SKIP auto-tagging manifest assertions (private checkout absent)")

    if spec_path.is_file():
        loader = importlib.util.spec_from_file_location("auto_tagging_selector_spec", spec_path)
        module = importlib.util.module_from_spec(loader)  # type: ignore[arg-type]
        assert loader and loader.loader
        loader.loader.exec_module(module)
        check(
            module.AUTO_TAGGING_SELECTOR_SPEC.task_key == "auto_tagging",
            "auto-tagging selector spec resolves through the auto_tagging task",
        )
    else:
        print("SKIP auto-tagging spec assertion (private checkout absent)")

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
