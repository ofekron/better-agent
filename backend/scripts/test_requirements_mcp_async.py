#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
PKG_ROOT = REPO / "better-agent-private" / "extensions" / "requirements"
sys.path.insert(0, str(REPO / "sdk"))

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS" if cond else "FAIL"), label)
    if not cond:
        FAILURES.append(label)


def load_server_module():
    spec = importlib.util.spec_from_file_location(
        "requirements_mcp_async_test",
        PKG_ROOT / "mcp" / "server.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_fire_returns_id_before_backend_result() -> None:
    module = load_server_module()
    calls: list[tuple[str, dict, float]] = []

    class FakeClient:
        def call_internal(self, path, body=None, *, timeout=60.0):
            calls.append((path, dict(body or {}), timeout))
            time.sleep(0.15)
            return {"success": True, "requirements": [{"text": "async requirement"}], "count": 1}

    saved_client = module.Client
    saved_spill = module.spill_large_result
    module.Client = FakeClient
    module.spill_large_result = lambda result, *, label: result
    try:
        started = time.perf_counter()
        fired = module.fire_get_requirements_response(" async task ", cwd="/repo", max_matches=2)
        elapsed = time.perf_counter() - started
        polled = module.get_requirements_results_response(fired["id"], wait=0)
        waited = module.get_requirements_results_response(fired["id"], wait=1)
    finally:
        module.Client = saved_client
        module.spill_large_result = saved_spill

    check(fired["success"] is True and fired["status"] == "running", "fire returns running id")
    check(elapsed < 0.05, "fire does not block on backend result")
    check(polled["success"] is True and polled["ready"] is False, "poll returns not-ready while running")
    check(waited["success"] is True and waited["ready"] is True, "wait returns completed result")
    check(waited["result"]["requirements"][0]["text"] == "async requirement", "completed result is returned")
    check(calls[0][0] == "/api/internal/get-requirements", "worker calls existing backend endpoint")
    check(calls[0][1]["query"] == "async task", "worker trims query")
    check(calls[0][1]["max_matches"] == 2, "worker forwards max_matches")


def test_validation_and_unknown_id_fail_closed() -> None:
    module = load_server_module()

    check(
        module.fire_get_requirements_response("", cwd="/repo")["error"] == "query is required",
        "fire rejects empty query",
    )
    check(
        module.fire_get_requirements_response("task", cwds=[1])["error"] == "cwds must be a list of strings",
        "fire rejects non-string cwds",
    )
    check(
        module.get_requirements_results_response("", wait=0)["error"] == "id is required",
        "results reject empty id",
    )
    check(
        module.get_requirements_results_response("missing", wait=0)["error"] == "unknown id",
        "results fail closed for unknown id",
    )
    check(
        module.get_requirements_results_response("missing", wait=-1)["error"]
        == "wait must be a non-negative number of seconds",
        "results reject negative wait",
    )


def test_public_tool_surface_is_async() -> None:
    module = load_server_module()
    tools = {tool.name for tool in module.build_server()._tool_manager.list_tools()}

    check("fire_get_requirements" in tools, "public MCP exposes fire_get_requirements")
    check("get_requirements_results" in tools, "public MCP exposes get_requirements_results")
    check("get_requirements" not in tools, "public MCP no longer exposes blocking get_requirements")
    check("get_requirements_internal" in tools, "public MCP keeps internal raw search")


def run() -> None:
    test_fire_returns_id_before_backend_result()
    test_validation_and_unknown_id_fail_closed()
    test_public_tool_surface_is_async()
    if FAILURES:
        print("\nFAILURES:")
        for failure in FAILURES:
            print("-", failure)
        raise SystemExit(1)


if __name__ == "__main__":
    run()
