#!/usr/bin/env python3
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import _test_home

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-extension-context-audit-"))
_test_home.isolate("ba-extension-context-audit-")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import extension_context_audit as audit  # noqa: E402
import extension_store  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def t_cache_hit_injects_concise_context() -> None:
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


def t_stale_cache_triggers_refresh_without_blocking() -> None:
    calls: list[str] = []
    inventory = {"version": 1, "cwd": "/tmp/project", "extensions": [{"id": "x"}], "runtime_skills": []}
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
                "instructions": [{"name": "Harness Rules", "level": "global"}],
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
        == {("instructions", "Harness Rules"), ("skill", "reviewer"), ("mcp", "test-tool")},
        "harness additions expose instructions, skills, and MCP tools",
    )


def main() -> None:
    try:
        t_cache_hit_injects_concise_context()
        t_stale_cache_triggers_refresh_without_blocking()
        t_audit_result_is_bounded_and_normalized()
        t_harness_additions_include_instructions_skills_and_mcp()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
