#!/usr/bin/env python3
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

import _test_home

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-extension-context-audit-"))
_test_home.isolate("ba-extension-context-audit-")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import extension_context_audit as audit  # noqa: E402
import extension_store  # noqa: E402


def reset_projection() -> None:
    audit._INVENTORY_PROJECTION.clear()  # type: ignore[attr-defined]
    audit._PROJECTION_IN_FLIGHT.clear()  # type: ignore[attr-defined]
    audit._CACHE_PROJECTION = None  # type: ignore[attr-defined]


def seed_projection(cwd: str, inventory: dict) -> None:
    audit._INVENTORY_PROJECTION[cwd] = (  # type: ignore[attr-defined]
        time.monotonic(),
        audit._fingerprint(inventory),  # type: ignore[attr-defined]
        inventory,
    )


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def t_cache_hit_injects_concise_context() -> None:
    reset_projection()
    result = {
        "summary": "Use session tools before direct worker creation.",
        "tool_guidance": ["Use search before delegate."],
        "attention": [{"severity": "high", "title": "Duplicate tools", "reason": "Two extensions expose similar delegation tools."}],
    }
    inventory = {"version": 1, "cwd": "/tmp/project", "extensions": [], "runtime_skills": []}
    audit._write_cache({  # type: ignore[attr-defined]
        "schema_version": 1,
        "fingerprint": audit._fingerprint(inventory),  # type: ignore[attr-defined]
        "result": result,
    })
    seed_projection("/tmp/project", inventory)
    old_build = audit.build_inventory
    try:
        audit.build_inventory = lambda _cwd: inventory  # type: ignore[assignment]
        contexts = audit.runtime_context("/tmp/project")
    finally:
        audit.build_inventory = old_build  # type: ignore[assignment]
    check(len(contexts) == 1, "cache hit injects one dynamic audit context")
    content = contexts[0]["content"]
    check("Use session tools" in content, "context includes summary")
    check("Needs user attention" in content, "context includes attention section")


def t_not_ready_suppresses_cached_context() -> None:
    reset_projection()
    inventory = {"version": 1, "cwd": "/tmp/project", "extensions": [], "runtime_skills": []}
    audit._write_cache({  # type: ignore[attr-defined]
        "schema_version": 1,
        "fingerprint": audit._fingerprint(inventory),  # type: ignore[attr-defined]
        "result": {"summary": "cached"},
    })
    seed_projection("/tmp/project", inventory)
    old_build = audit.build_inventory
    old_ready = audit._is_runtime_ready  # type: ignore[attr-defined]
    try:
        audit.build_inventory = lambda _cwd: inventory  # type: ignore[assignment]
        audit._is_runtime_ready = lambda: False  # type: ignore[attr-defined]
        contexts = audit.runtime_context("/tmp/project")
    finally:
        audit.build_inventory = old_build  # type: ignore[assignment]
        audit._is_runtime_ready = old_ready  # type: ignore[attr-defined]
    check(contexts == [], "not-ready task suppresses cached dynamic audit context")


def t_stale_cache_triggers_refresh_without_blocking() -> None:
    reset_projection()
    calls: list[str] = []
    inventory = {"version": 1, "cwd": "/tmp/project", "extensions": [{"id": "x"}], "runtime_skills": []}
    seed_projection("/tmp/project", inventory)
    old_build = audit.build_inventory
    old_trigger = audit._trigger_refresh  # type: ignore[attr-defined]
    try:
        audit.build_inventory = lambda _cwd: inventory  # type: ignore[assignment]
        audit._trigger_refresh = lambda fingerprint, _inventory: calls.append(fingerprint)  # type: ignore[attr-defined]
        contexts = audit.runtime_context("/tmp/project")
    finally:
        audit.build_inventory = old_build  # type: ignore[assignment]
        audit._trigger_refresh = old_trigger  # type: ignore[attr-defined]
    check(contexts == [], "stale cache injects nothing")
    check(calls == [audit._fingerprint(inventory)], "stale cache starts background refresh")  # type: ignore[attr-defined]


def t_cold_projection_schedules_inventory_refresh_without_blocking() -> None:
    reset_projection()
    calls: list[str] = []
    old_trigger = audit._trigger_projection_refresh  # type: ignore[attr-defined]
    old_build = audit.build_inventory
    try:
        audit._trigger_projection_refresh = lambda cwd: calls.append(cwd)  # type: ignore[attr-defined]
        audit.build_inventory = lambda _cwd: (_ for _ in ()).throw(AssertionError("hot path built inventory"))  # type: ignore[assignment]
        contexts = audit.runtime_context("/tmp/project")
    finally:
        audit._trigger_projection_refresh = old_trigger  # type: ignore[attr-defined]
        audit.build_inventory = old_build  # type: ignore[assignment]
    check(contexts == [], "cold projection injects nothing")
    check(calls == ["/tmp/project"], "cold projection schedules inventory refresh")


def t_audit_result_is_bounded_and_normalized() -> None:
    parsed = audit.AUDIT_SPEC.parse_result(
        "prefix {\"summary\":\"ok\",\"tool_guidance\":[\"a\",\"b\"],"
        "\"attention\":[{\"severity\":\"critical\",\"title\":\"T\",\"reason\":\"R\"}]} suffix",
        {},
    )
    check(parsed["attention"][0]["severity"] == "medium", "unknown severity normalizes to medium")
    check(parsed["summary"] == "ok", "summary parses from JSON object")


def t_harness_additions_include_instructions_skills_and_mcp() -> None:
    record = {
        "manifest": {
            "id": "ofek.test",
            "entrypoints": {
                "instructions": [{"name": "harness-rules", "level": "global"}],
                "skills": [{"name": "reviewer", "path": "skills/reviewer"}],
                "mcp": [{"name": "test-tool"}],
            },
        }
    }
    old_enabled = extension_store.is_mcp_server_enabled
    try:
        extension_store.is_mcp_server_enabled = lambda _extension_id, _name: True  # type: ignore[assignment]
        additions = extension_store.extension_harness_additions(record)
    finally:
        extension_store.is_mcp_server_enabled = old_enabled  # type: ignore[assignment]
    check(
        {(item["kind"], item["name"]) for item in additions}
        == {("instructions", "harness-rules"), ("skill", "reviewer"), ("mcp", "test-tool")},
        "harness additions expose instructions, skills, and MCP tools",
    )


def main() -> None:
    try:
        t_cache_hit_injects_concise_context()
        t_not_ready_suppresses_cached_context()
        t_stale_cache_triggers_refresh_without_blocking()
        t_cold_projection_schedules_inventory_refresh_without_blocking()
        t_audit_result_is_bounded_and_normalized()
        t_harness_additions_include_instructions_skills_and_mcp()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
