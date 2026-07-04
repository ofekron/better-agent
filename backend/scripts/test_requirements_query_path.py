#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
PKG_ROOT = REPO / "better-agent-private" / "extensions" / "requirements"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PKG_ROOT))
sys.path.insert(0, str(REPO / "sdk"))

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-test-req-query-path-"))
import _test_home
_test_home.isolate("ba-test-")

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS" if cond else "FAIL"), label)
    if not cond:
        FAILURES.append(label)


def load_server_module():
    spec = importlib.util.spec_from_file_location(
        "requirements_mcp_server_test",
        PKG_ROOT / "mcp" / "server.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_mcp_exposes_direct_index_tool_without_processed_lookup() -> None:
    module = load_server_module()
    tools = {tool.name for tool in module.build_server()._tool_manager.list_tools()}

    check("query_provider_native_transcript_index" in tools, "MCP exposes direct native index SQL tool")
    check("get_requirements_internal" in tools, "MCP keeps raw diagnostic search")
    check("fire_get_requirements" not in tools, "MCP no longer exposes fire_get_requirements")
    check("get_requirements_results" not in tools, "MCP no longer exposes get_requirements_results")


def test_index_sql_tool_routes_to_internal_endpoint() -> None:
    module = load_server_module()
    calls: list[tuple[str, dict, float]] = []

    class FakeClient:
        def call_internal(self, path, body=None, *, timeout=60.0):
            calls.append((path, dict(body or {}), timeout))
            return {"success": True, "columns": ["text"], "rows": [["direct"]]}

    saved_client = module.Client
    module.Client = FakeClient
    try:
        result = module.query_provider_native_transcript_index_response(
            "SELECT text FROM native_element_fts LIMIT 1"
        )
    finally:
        module.Client = saved_client

    check(result["success"] is True, "direct index tool returns backend result")
    check(calls == [
        (
            "/api/internal/get-requirements/index-sql",
            {"sql": "SELECT text FROM native_element_fts LIMIT 1"},
            120.0,
        )
    ], "direct index tool calls only the index-sql endpoint")
    check(
        module.query_provider_native_transcript_index_response("")["error"] == "sql is required",
        "direct index tool rejects empty SQL",
    )


def test_large_index_sql_result_spills_to_file() -> None:
    module = load_server_module()
    large_text = "x" * 45_000

    class FakeClient:
        def call_internal(self, path, body=None, *, timeout=60.0):
            return {"success": True, "columns": ["text"], "rows": [[large_text]]}

    saved_client = module.Client
    module.Client = FakeClient
    try:
        result = module.query_provider_native_transcript_index_response(
            "SELECT text FROM native_element_fts LIMIT 1"
        )
    finally:
        module.Client = saved_client

    result_path = Path(result["result_path"])
    try:
        check(result["success"] is True, "large index result keeps success metadata")
        check(result["result_spilled_to_file"] is True, "large index result spills to file")
        check(result["result_estimated_tokens"] > 10_000,
              "large index result reports estimated token length")
        check(result_path.exists(), "large index result file exists")
        check("rows" not in result, "large index result omits full rows from tool response")
        check(large_text in result_path.read_text(encoding="utf-8"),
              "large index result file contains full payload")
    finally:
        result_path.unlink(missing_ok=True)


def test_raw_search_tool_guidance_points_to_direct_index() -> None:
    src = (PKG_ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
    fn = src.split("def get_requirements_internal", 1)[1].split("def query_provider_native_transcript_index", 1)[0]

    check("include_unprocessed_prompts=True" in fn, "raw search forces unprocessed prompts")
    check("provider_native_only: bool = True" in fn, "raw search defaults to provider-native corpus")
    check("query_provider_native_transcript_index directly" in fn,
          "raw search tells normal agents to use direct index SQL")
    check("fire_get_requirements" not in fn, "raw search docs do not mention fire_get_requirements")
    check("get_requirements_results" not in fn, "raw search docs do not mention get_requirements_results")


def test_skill_teaches_direct_index_requirements_workflow() -> None:
    skill = (PKG_ROOT / "skills" / "get-requirements" / "SKILL.md").read_text(encoding="utf-8")

    check("avoid drifting from prior user requirements" in skill,
          "skill states the goal is drift avoidance")
    check("Query `query_provider_native_transcript_index` directly" in skill,
          "skill instructs direct index querying")
    check("Do not use generic search keywords" in skill,
          "skill rejects generic keyword searches")
    check("Rows are not requirements by themselves" in skill,
          "skill separates evidence rows from requirements")
    check("Search in at most two parallel rounds" in skill,
          "skill caps search rounds")
    check("Read surrounding rows" in skill,
          "skill requires surrounding transcript context")
    check("confirms, adopts, or refines" in skill,
          "skill handles assistant proposals only after user confirmation")
    check("look deeper for requirement evolution" in skill,
          "skill requires deeper investigation before calling drift")
    check("notify the user" in skill,
          "skill tells agents to notify on real requirement conflicts")
    check("fire_get_requirements" not in skill,
          "skill no longer references fire_get_requirements")
    check("get_requirements_results" not in skill,
          "skill no longer references get_requirements_results")


def test_processor_fork_wiring_is_removed() -> None:
    import requirement_context

    manifest = (PKG_ROOT / "better-agent-extension.json").read_text(encoding="utf-8")
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    runner_src = (ROOT / "requirements_query_runner.py").read_text(encoding="utf-8")

    check(not hasattr(requirement_context, "get_processed_requirements"),
          "requirement_context has no processed lookup helper")
    check(not hasattr(requirement_context, "GET_REQUIREMENTS_PROCESSOR_SPEC"),
          "requirement_context has no processor spec handle")
    check("requirement_analysis.processor_spec" not in manifest,
          "extension smoke test no longer imports processor spec")
    check('"spawn_runs"' not in manifest and '"provider_config"' not in manifest,
          "requirements extension no longer declares processor-only permissions")
    check("/api/internal/get-requirements/index-sql" in main_src,
          "backend keeps direct index SQL endpoint")
    check('@app.post("/api/internal/get-requirements")' not in main_src,
          "backend no longer exposes processed get-requirements endpoint")
    check("REQUIREMENTS_PROCESSOR_EXECUTOR" not in runner_src,
          "requirements query runner no longer has a processor executor")
    check("run_requirements_processor_query" not in runner_src,
          "requirements query runner no longer has processor runner")


def run() -> None:
    test_mcp_exposes_direct_index_tool_without_processed_lookup()
    test_index_sql_tool_routes_to_internal_endpoint()
    test_large_index_sql_result_spills_to_file()
    test_raw_search_tool_guidance_points_to_direct_index()
    test_skill_teaches_direct_index_requirements_workflow()
    test_processor_fork_wiring_is_removed()


if __name__ == "__main__":
    try:
        run()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        sys.exit(1)
    print("\nALL PASSED")
