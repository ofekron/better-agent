#!/usr/bin/env python3
"""Locks the requirements execution-model redesign:

1. get-requirements query path is cheap + non-blocking: public and processor
   raw lookups use local-read preparation while the detached background runner
   owns prompt sync, unit extraction, and downstream DAG work.
2. The detached runner is spawned with an injected PYTHONPATH so its child
   interpreter can import the requirements package + backend modules.
3. The batch packer falls back to a feasible greedy packing when the MILP
   solver fails (e.g. HiGHS time-limit on a large backlog) instead of raising.
4. The processor's internal search tool always includes the not-yet-extracted
   raw prompts (best-effort while the backlog drains).
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]                       # .../backend
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


def test_greedy_packing_respects_capacity_and_cap() -> None:
    from requirement_analysis import prephase

    Item = prephase._PackItem
    # Four 60-token items, two bins of 100: each bin holds one (120 > 100), so
    # 2 fit and 2 overflow → dropped (stay pending, no extra bin opened).
    items = [Item(index=i, plan=None, size=60, cluster_index=0) for i in range(4)]
    bins = prephase._greedy_bin_packing(items, max_bins=2, max_input_tokens=100)
    check(len(bins) <= 2, "greedy never exceeds max_bins")
    check(all(sum(it.size for it in b) <= 100 for b in bins), "no bin exceeds capacity")
    placed = [it.index for b in bins for it in b]
    check(len(placed) == len(set(placed)), "each item placed at most once")
    check(len(placed) < len(items), "overflow items dropped when max_bins reached")


def test_milp_failure_falls_back_to_greedy() -> None:
    from requirement_analysis import prephase
    import scipy.optimize as opt

    Item = prephase._PackItem
    items = [Item(index=i, plan=None, size=40, cluster_index=0) for i in range(4)]
    orig = opt.milp
    opt.milp = lambda *a, **k: types.SimpleNamespace(
        success=False, message="Time limit reached. (HiGHS Status 13)", x=None
    )
    try:
        bins = prephase._solve_token_bin_packing_milp(
            items, cluster_count=1, max_bins=4, max_input_tokens=100
        )
    finally:
        opt.milp = orig
    check(isinstance(bins, list) and bins, "MILP timeout falls back to a non-empty greedy packing (no raise)")
    check(all(sum(it.size for it in b) <= 100 for b in bins), "greedy fallback respects capacity")


def test_query_path_has_no_inline_extraction() -> None:
    import requirement_context as rc

    src = inspect.getsource(rc.prepare_requirements_context)
    check("sync_units" not in src, "prepare_requirements_context never calls sync_units")
    check("_ensure_requirement_units_current" not in src, "inline extraction step removed")
    check("_ensure_background_extraction" in src, "prepare ensures the background runner instead")
    check(not hasattr(rc, "_maybe_run_downstream_in_background"), "in-backend downstream daemon removed")
    check(not hasattr(rc, "_sync_requirement_units"), "inline unit-sync helper removed")


def test_public_get_requirements_keeps_processor_off_sync_path() -> None:
    import requirement_context as rc

    saved = {
        "prepare": rc.prepare_requirements_context,
        "local_prepare": rc.prepare_requirements_local_read_context,
        "run_sync": rc.provisioning.run_sync,
        "ensure_importable": rc._ensure_requirements_importable,
        "freshness": rc._requirement_unit_freshness,
        "background": rc._ensure_background_extraction,
    }
    order: list[str] = []

    def fail(*_args, **_kwargs):
        raise AssertionError("public requirements lookup must not run sync preparation")

    class _Result:
        value = {
            "requirements": [{
                "text": "Semantic processor keeps requirements feature intact.",
                "kind": "explicit",
                "origin": "user_prompt",
                "polarity": "positive",
                "strength": "high",
                "source": "user",
                "cwd": "/repo",
            }]
        }

    def local_prepare(**_kwargs):
        order.append("local_prepare")
        return {"success": True, "sync": {"skipped": "local_read"}, "freshness": {"fresh": True}}

    def run_sync(spec, query, ctx):
        order.append("processor")
        return _Result()

    rc.prepare_requirements_context = fail
    rc.prepare_requirements_local_read_context = local_prepare
    rc.provisioning.run_sync = run_sync
    rc._ensure_requirements_importable = lambda: None
    rc._requirement_unit_freshness = lambda **_kwargs: {"fresh": True, "unhandled_prompts": 0}
    rc._ensure_background_extraction = lambda: {"running": True}
    try:
        result = rc.get_processed_requirements(query="performance logs rca", cwd="/repo")
    finally:
        rc.prepare_requirements_context = saved["prepare"]
        rc.prepare_requirements_local_read_context = saved["local_prepare"]
        rc.provisioning.run_sync = saved["run_sync"]
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._requirement_unit_freshness = saved["freshness"]
        rc._ensure_background_extraction = saved["background"]

    check(order == ["local_prepare", "processor"], "public get-requirements uses processor after local prep")
    check(result["success"] is True, "public get-requirements succeeds through semantic processor")
    check(result["count"] == 1, "public get-requirements returns processor result")
    check(result["requirements"][0]["text"].startswith("Semantic processor"), "semantic processor result is returned")
    check("rg_args" not in result, "public result does not expose raw rg args")
    check("command" not in result, "public result does not expose command")


def test_processor_timeout_response_fails_without_fallback() -> None:
    import requirement_context as rc

    result = rc.build_processed_requirements_response(
        query="processor saturation",
        cwd="/repo",
        processed={
            "requirements": [],
            "error": (
                "processor_failed: get-requirements processor timed out before returning "
                "requirements; no retry attempted"
            ),
        },
    )

    check(result["success"] is False, "processor timeout fails without fallback")
    check(result["count"] == 0, "processor timeout returns no substitute requirements")
    check("timed out" in result.get("error", ""), "processor timeout error is preserved")


def test_processor_readtimeout_response_fails_without_fallback() -> None:
    import requirement_context as rc

    result = rc.build_processed_requirements_response(
        query="processor saturation",
        cwd="/repo",
        processed={
            "requirements": [],
            "error": "processor_failed: ReadTimeout",
        },
    )

    check(result["success"] is False, "processor ReadTimeout fails without fallback")
    check(result["count"] == 0, "processor ReadTimeout returns no substitute requirements")
    check("ReadTimeout" in result.get("error", ""), "processor ReadTimeout error is preserved")


def test_mcp_timeout_fails_without_fallback() -> None:
    spec = importlib.util.spec_from_file_location("requirements_mcp_server_test", PKG_ROOT / "mcp" / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    calls: list[tuple[str, dict, float]] = []

    class FakeClient:
        def call_internal(self, path, body=None, *, timeout=60.0):
            calls.append((path, dict(body or {}), timeout))
            if path == "/api/internal/get-requirements":
                raise TimeoutError("timed out")
            raise AssertionError(path)

    saved_client = module.Client
    saved_spill = module.spill_large_result
    module.Client = FakeClient
    module.spill_large_result = lambda result, *, label: result
    try:
        result = module.get_requirements_response(
            " public timeout ",
            cwd="/repo",
            cwds=["/repo/a"],
            all_projects=True,
            max_matches=4,
        )
    finally:
        module.Client = saved_client
        module.spill_large_result = saved_spill

    check(result["success"] is False, "MCP timeout fails without fallback")
    check("timed out" in result.get("error", ""), "MCP timeout error is returned")
    check([call[0] for call in calls] == [
        "/api/internal/get-requirements",
    ], "MCP does not call direct fallback after public timeout")


def test_requirements_processor_mcp_hides_recursive_tools() -> None:
    spec_path = PKG_ROOT / "mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("requirements_mcp_server_tool_profile_test", spec_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    saved = os.environ.get("BETTER_CLAUDE_REQUIREMENTS_PROCESSOR")
    try:
        os.environ.pop("BETTER_CLAUDE_REQUIREMENTS_PROCESSOR", None)
        public_tools = {tool.name for tool in module.build_server()._tool_manager.list_tools()}
        check(
            "fire_get_requirements" in public_tools,
            "normal requirements MCP exposes async fire_get_requirements tool",
        )
        check(
            "get_requirements_results" in public_tools,
            "normal requirements MCP exposes async get_requirements_results tool",
        )
        check(
            "get_requirements" not in public_tools,
            "normal requirements MCP does not expose blocking get_requirements tool",
        )
        check(
            "query_provider_native_transcript_index" in public_tools,
            "normal requirements MCP exposes provider-native index tool",
        )

        os.environ["BETTER_CLAUDE_REQUIREMENTS_PROCESSOR"] = "1"
        processor_tools = {tool.name for tool in module.build_server()._tool_manager.list_tools()}
        check(
            processor_tools == {"query_provider_native_transcript_index"},
            "processor requirements MCP exposes only provider-native index tool",
        )
    finally:
        if saved is None:
            os.environ.pop("BETTER_CLAUDE_REQUIREMENTS_PROCESSOR", None)
        else:
            os.environ["BETTER_CLAUDE_REQUIREMENTS_PROCESSOR"] = saved


def test_requirements_processor_spec_sets_restricted_tool_profile() -> None:
    from requirement_analysis.processor_spec import GET_REQUIREMENTS_PROCESSOR_SPEC

    check(
        GET_REQUIREMENTS_PROCESSOR_SPEC.tool_profile == "requirements_processor",
        "requirements processor spec requests restricted MCP tool profile",
    )


def test_mcp_timeout_result_fails_without_fallback() -> None:
    spec = importlib.util.spec_from_file_location("requirements_mcp_server_timeout_result_test", PKG_ROOT / "mcp" / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    calls: list[str] = []

    class FakeClient:
        def call_internal(self, path, body=None, *, timeout=60.0):
            calls.append(path)
            if path == "/api/internal/get-requirements":
                return {
                    "success": False,
                    "requirements": [],
                    "count": 0,
                    "error": (
                        "processor_failed: get-requirements processor timed out before "
                        "returning requirements; no retry attempted"
                    ),
                }
            raise AssertionError(path)

    saved_client = module.Client
    saved_spill = module.spill_large_result
    module.Client = FakeClient
    module.spill_large_result = lambda result, *, label: result
    try:
        result = module.get_requirements_response("public timeout result", cwd="/repo")
    finally:
        module.Client = saved_client
        module.spill_large_result = saved_spill

    check(result["success"] is False, "MCP timeout result fails without fallback")
    check("timed out" in result.get("error", ""), "MCP timeout result preserves error")
    check([call for call in calls] == [
        "/api/internal/get-requirements",
    ], "MCP does not call direct fallback after timeout result")


def test_mcp_transport_failure_returns_error() -> None:
    spec = importlib.util.spec_from_file_location("requirements_mcp_server_non_timeout_test", PKG_ROOT / "mcp" / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    calls: list[str] = []

    class FakeClient:
        def call_internal(self, path, body=None, *, timeout=60.0):
            calls.append(path)
            raise RuntimeError("backend unavailable")

    saved_client = module.Client
    module.Client = FakeClient
    try:
        result = module.get_requirements_response("public failure", cwd="/repo")
    finally:
        module.Client = saved_client

    check(result["success"] is False, "MCP transport failure returns an error")
    check(calls == ["/api/internal/get-requirements"], "MCP calls only the public requirements endpoint")


def test_raw_search_keeps_processor_off_sync_path() -> None:
    import requirement_context as rc

    saved = {
        "prepare": rc.prepare_requirements_context,
        "local_prepare": rc.prepare_requirements_local_read_context,
        "ensure_importable": rc._ensure_requirements_importable,
        "load_units": rc._load_unit_records,
        "run_rg": rc._run_rg,
    }
    order: list[str] = []

    def fail(*_args, **_kwargs):
        raise AssertionError("raw requirements lookup must not run sync preparation")

    def local_prepare(**_kwargs):
        order.append("local_prepare")
        return {
            "success": True,
            "sync": {"success": True, "changed": False, "skipped": "local_read"},
            "freshness": {"fresh": True},
            "extraction": {"running": True},
        }

    rc.prepare_requirements_context = fail
    rc.prepare_requirements_local_read_context = local_prepare
    rc._ensure_requirements_importable = lambda: None
    rc._load_unit_records = lambda: [{
        "source_key": "s:1:unit:0",
        "text": "Raw processor lookup stays responsive.",
        "kind": "explicit",
        "origin": "user_prompt",
        "source": "user",
        "cwd": "/repo",
    }]
    rc._run_rg = lambda path, args: {
        "command": ["rg", *args, str(path)],
        "returncode": 0,
        "stdout": "1:Raw processor lookup stays responsive.\n",
        "stderr": "",
    }
    try:
        result = rc.search_requirements(
            rg_args=["responsive"],
            cwd="/repo",
            include_unprocessed_prompts=False,
            provider_native_only=False,
            max_matches=5,
        )
    finally:
        rc.prepare_requirements_context = saved["prepare"]
        rc.prepare_requirements_local_read_context = saved["local_prepare"]
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._load_unit_records = saved["load_units"]
        rc._run_rg = saved["run_rg"]

    check(order == ["local_prepare"], "raw search uses local prep only")
    check(result["success"] is True, "raw search succeeds through local prep")
    check(result["count"] == 1, "raw search returns matching unit")


def test_provider_native_only_search_skips_unit_corpus() -> None:
    import requirement_context as rc

    saved = {
        "prepare": rc.prepare_requirements_local_read_context,
        "ensure_importable": rc._ensure_requirements_importable,
        "load_units": rc._load_unit_records,
        "run_rg": rc._run_rg,
        "native": rc._search_native_transcript_bundles,
    }
    calls: list[str] = []

    def fail(*_args, **_kwargs):
        raise AssertionError("provider-native search must not touch processed requirement units")

    def native(**kwargs):
        calls.append("native")
        check(kwargs["enabled"] is True, "provider-native search enables native transcript bundles")
        check(kwargs["remaining"] == 5, "provider-native search respects max_matches")
        return {
            "enabled": True,
            "searched": True,
            "matches": [{
                "source_key": "native:/repo:1",
                "text": "User approved provider-native requirement extraction.",
                "kind": "native_transcript_bundle",
                "source": "provider_native_transcript",
                "cwd": "/repo",
                "ts": "2026-01-01T00:00:00Z",
            }],
            "count": 1,
            "query": "provider native",
            "index": {"ready": True},
        }

    rc.prepare_requirements_local_read_context = fail
    rc._ensure_requirements_importable = lambda: None
    rc._load_unit_records = fail
    rc._run_rg = fail
    rc._search_native_transcript_bundles = native
    try:
        result = rc.search_requirements(
            rg_args=["-i", "-e", "provider native"],
            cwd="/repo",
            provider_native_only=True,
            max_matches=5,
        )
    finally:
        rc.prepare_requirements_local_read_context = saved["prepare"]
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._load_unit_records = saved["load_units"]
        rc._run_rg = saved["run_rg"]
        rc._search_native_transcript_bundles = saved["native"]

    check(calls == ["native"], "provider-native search uses only native transcript bundles")
    check(result["success"] is True, "provider-native search succeeds")
    check(result["authority"] == "provider_native_transcript_corpus", "provider-native search declares corpus authority")
    check(result["count"] == 1, "provider-native search returns native evidence")


def test_search_defaults_to_provider_native_corpus() -> None:
    import requirement_context as rc

    saved = {
        "prepare": rc.prepare_requirements_local_read_context,
        "ensure_importable": rc._ensure_requirements_importable,
        "load_units": rc._load_unit_records,
        "run_rg": rc._run_rg,
        "native": rc._search_native_transcript_bundles,
    }

    def fail(*_args, **_kwargs):
        raise AssertionError("default search must not touch the legacy mined-unit path")

    rc.prepare_requirements_local_read_context = fail
    rc._ensure_requirements_importable = lambda: None
    rc._load_unit_records = fail
    rc._run_rg = fail
    rc._search_native_transcript_bundles = lambda **kwargs: {
        "enabled": True, "searched": True, "matches": [], "count": 0,
    }
    try:
        result = rc.search_requirements(rg_args=["-i", "-e", "anything"], cwd="/repo")
    finally:
        rc.prepare_requirements_local_read_context = saved["prepare"]
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._load_unit_records = saved["load_units"]
        rc._run_rg = saved["run_rg"]
        rc._search_native_transcript_bundles = saved["native"]

    check(result["success"] is True, "default search succeeds on native path")
    check(result["authority"] == "provider_native_transcript_corpus",
          "search defaults to the provider-native corpus")


def test_compare_mode_runs_both_paths_and_diffs() -> None:
    import requirement_context as rc

    saved = {
        "prepare": rc.prepare_requirements_local_read_context,
        "ensure_importable": rc._ensure_requirements_importable,
        "load_units": rc._load_unit_records,
        "run_rg": rc._run_rg,
        "native": rc._search_native_transcript_bundles,
    }
    calls: list[str] = []

    def local_prepare(**_kwargs):
        calls.append("legacy_prepare")
        return {
            "success": True,
            "sync": {"success": True, "changed": False, "skipped": "local_read"},
            "freshness": {"fresh": True},
            "extraction": {"running": True},
        }

    def native(**kwargs):
        if not kwargs.get("enabled"):
            return {"enabled": False, "searched": False, "matches": [], "count": 0}
        calls.append("native")
        return {
            "enabled": True,
            "searched": True,
            "matches": [{
                "text": "Native-only requirement.",
                "kind": "native_transcript_bundle",
                "source": "provider_native_transcript",
                "cwd": "/repo",
                "ts": "2026-02-02T00:00:00Z",
            }],
            "count": 1,
        }

    rc.prepare_requirements_local_read_context = local_prepare
    rc._ensure_requirements_importable = lambda: None
    rc._load_unit_records = lambda: [{
        "source_key": "s:1:unit:0",
        "text": "Legacy-only requirement.",
        "kind": "explicit",
        "origin": "user_prompt",
        "source": "user",
        "cwd": "/repo",
        "ts": "2026-01-01T00:00:00Z",
    }]
    rc._run_rg = lambda path, args: {
        "command": ["rg", *args, str(path)],
        "returncode": 0,
        "stdout": "1:Legacy-only requirement.\n",
        "stderr": "",
    }
    rc._search_native_transcript_bundles = native
    try:
        result = rc.search_requirements(
            rg_args=["-i", "-e", "requirement"],
            cwd="/repo",
            compare=True,
            max_matches=5,
        )
    finally:
        rc.prepare_requirements_local_read_context = saved["prepare"]
        rc._ensure_requirements_importable = saved["ensure_importable"]
        rc._load_unit_records = saved["load_units"]
        rc._run_rg = saved["run_rg"]
        rc._search_native_transcript_bundles = saved["native"]

    check(result.get("compare") is True, "compare mode flags its result")
    check(calls == ["native", "legacy_prepare"], "compare mode runs native then legacy")
    check(result["native"]["authority"] == "provider_native_transcript_corpus",
          "compare mode carries the full native result")
    check(result["legacy"]["count"] == 1, "compare mode carries the full legacy result")
    diff = result["diff"]
    check(diff["native_count"] == 1 and diff["legacy_count"] == 1, "compare diff reports per-side counts")
    check(diff["identical_match_count"] == 0, "compare diff detects disjoint matches")
    check(diff["native_only_ts"] == ["2026-02-02T00:00:00Z"], "compare diff lists native-only timestamps")
    check(diff["legacy_only_ts"] == ["2026-01-01T00:00:00Z"], "compare diff lists legacy-only timestamps")


def test_processor_prompt_is_available_to_running_backend() -> None:
    import requirement_context as rc

    saved = rc._ensure_requirements_importable
    rc._ensure_requirements_importable = lambda: PKG_ROOT
    try:
        prompt = rc.GET_REQUIREMENTS_PROCESSOR_SPEC.build_provision_prompt({})
        instructions = rc.GET_REQUIREMENTS_PROCESSOR_SPEC.build_instructions("session search parse_failed", {
            "cwd": "/repo",
            "cwds": [],
            "all_projects": False,
            "max_matches": 5,
        })
    finally:
        rc._ensure_requirements_importable = saved

    check(prompt.startswith("<get-requirements-processor-prep>"), "processor prompt is available")
    check("Your purpose is to recover durable user requirements" in prompt,
          "processor prompt explains its purpose")
    check("searchable FTS5 projection of raw provider conversation logs" in prompt,
          "processor prompt explains the provider-native transcript index")
    check("when you need evidence from prior conversations" in prompt,
          "processor prompt explains when to use the index")
    check("not as a requirement by itself" in prompt,
          "processor prompt distinguishes the caller query from stored requirements")
    check("Do not call the get-requirements skill" in prompt, "processor prompt forbids recursive public lookup")
    check("query_provider_native_transcript_index" in prompt, "processor prompt uses free-form SQL on the native index")
    check("native_element_fts" in prompt, "processor prompt documents the index schema")
    check("bm25" in prompt, "processor prompt explains FTS ranking")
    check("Optimize for high recall within the processor's time budget" in prompt,
          "processor prompt requires high-recall search within the budget")
    check("first plausible match" in prompt, "processor prompt forbids early stopping after one match")
    check("provider_native_only" not in prompt, "processor prompt has no legacy fallback call")
    check("rg_args" not in prompt, "processor prompt has no rg pattern interface")
    check("at most 2 rounds" in prompt, "processor prompt caps searching at two parallel rounds")
    check("Never issue a third round" in prompt, "processor prompt forbids a third search round")
    check("parallel batch" in prompt, "processor prompt requires batched parallel queries, not serial calls")
    check("returns the complete result" in prompt, "processor prompt documents complete SQL results")
    check("confirms, adopts, or refines" in prompt, "processor prompt requires user confirmation for proposals")
    check("close to the user's original wording" in prompt, "processor prompt enforces wording faithfulness")
    check("verbatim from the evidence" in prompt, "processor prompt forbids inferred directional/ordinal terms")
    check("Never invent a requirement" in prompt, "processor prompt forbids invented requirements")
    check("origin is the decisive evidence provenance" in prompt, "processor prompt explains origin")
    check("user_confirmed_assistant_proposal" in prompt, "processor prompt documents confirmed proposal origin")
    check("source is only the short human-readable evidence pointer" in prompt,
          "processor prompt separates source from machine provenance")
    check("Your purpose is to recover durable user requirements" in instructions,
          "processor instructions explain their purpose")
    check("searchable FTS5 projection of raw provider conversation logs" in instructions,
          "processor instructions explain the provider-native transcript index")
    check("when you need evidence from prior conversations" in instructions,
          "processor instructions explain when to use the index")
    check("not as a requirement by itself" in instructions,
          "processor instructions distinguish the caller query from stored requirements")
    check("Do not call the get-requirements skill" in instructions, "processor instructions forbid recursive public lookup")
    check("query_provider_native_transcript_index" in instructions, "processor instructions use free-form SQL on the native index")
    check("native_element_fts" in instructions, "processor instructions document the index schema")
    check("Optimize for speed AND recall" in instructions,
          "processor instructions require speed-first high-recall search")
    check("first plausible match" in instructions, "processor instructions forbid early stopping after one match")
    check("provider_native_only" not in instructions, "processor instructions have no legacy fallback call")
    check("rg_args" not in instructions, "processor instructions have no rg pattern interface")
    check("at most 2 rounds" in instructions, "processor instructions cap searching at two parallel rounds")
    check("Never issue a third round" in instructions, "processor instructions forbid a third search round")
    check("bounded projection" in instructions, "processor instructions tell callers to bound SQL explicitly")
    check("confirms, adopts, or refines" in instructions, "processor instructions require user confirmation for proposals")
    check("verbatim from the evidence" in instructions, "processor instructions forbid inferred directional/ordinal terms")
    check("`kind` is the requirement lifecycle/status" in instructions,
          "processor instructions separate kind from origin")
    check("user_refined_assistant_proposal" in instructions,
          "processor instructions document refined proposal origin")


def test_processor_dispatch_is_isolated_and_timeout_budgeted() -> None:
    import requirement_context as rc

    saved = rc._ensure_requirements_importable
    rc._ensure_requirements_importable = lambda: PKG_ROOT
    try:
        spec = rc.GET_REQUIREMENTS_PROCESSOR_SPEC
        version = spec.version
        run_mode = spec.run_mode
        ephemeral_forks = spec.ephemeral_forks
    finally:
        rc._ensure_requirements_importable = saved
    server = (PKG_ROOT / "mcp" / "server.py").read_text(encoding="utf-8")

    check(version >= 3, "processor spec version invalidates stale processor prompt and parser bases")
    check(run_mode == "fork", "processor uses fork mode for lookup isolation")
    check(ephemeral_forks is True, "processor uses ephemeral fork per lookup")
    import re

    import requirements_query_runner as runner
    from provisioning.manager import _sync_timeout_seconds

    mcp_timeout = float(re.search(r"_GET_REQUIREMENTS_TIMEOUT = ([0-9.]+)", server).group(1))
    saved_importable = rc._ensure_requirements_importable
    rc._ensure_requirements_importable = lambda: PKG_ROOT
    try:
        run_sync_total = _sync_timeout_seconds(rc.GET_REQUIREMENTS_PROCESSOR_SPEC)
    finally:
        rc._ensure_requirements_importable = saved_importable
    check(runner.PROCESSOR_RESULT_TIMEOUT_SECONDS > run_sync_total,
          "public get-requirements result timeout lets processor run_sync own completion")
    check(mcp_timeout > runner.PROCESSOR_RESULT_TIMEOUT_SECONDS,
          "MCP timeout exceeds the public backend result timeout")
    check(mcp_timeout - runner.PROCESSOR_RESULT_TIMEOUT_SECONDS >= 30.0,
          "MCP timeout keeps enough headroom after backend result timeout")
    check(run_sync_total >= 300.0, "processor run_sync budget is at least 5 minutes")
    check("_SEARCH_TIMEOUT = 120.0" in server, "raw search keeps bounded timeout")


def test_processor_spec_fails_closed_without_private_registration() -> None:
    import requirement_context as rc

    saved_get = rc.provisioning.get
    saved_importable = rc._ensure_requirements_importable

    def missing_get(key):
        raise KeyError(key)

    def unavailable():
        raise RuntimeError("extension is not active")

    rc.provisioning.get = missing_get
    rc._ensure_requirements_importable = unavailable
    try:
        result = rc._run_requirements_processor(query="missing private", cwd="/repo")
    finally:
        rc.provisioning.get = saved_get
        rc._ensure_requirements_importable = saved_importable

    check(result["requirements"] == [], "missing private processor returns no requirements")
    check(result["error"].startswith("processor_failed: RuntimeError: provisioned spec"),
          "missing private processor fails closed through processor_failed")


def test_prepare_orchestration_is_cheap_and_nonblocking() -> None:
    import requirement_context as rc

    saved = {k: getattr(rc, k) for k in
             ("_ensure_requirements_importable", "_refresh_user_prompts",
              "_requirement_unit_freshness", "_ensure_background_extraction")}
    calls = {"refresh": 0, "bg": 0}
    try:
        rc._ensure_requirements_importable = lambda: None
        rc._refresh_user_prompts = lambda: (calls.__setitem__("refresh", calls["refresh"] + 1), {"success": True})[1]
        rc._requirement_unit_freshness = lambda **k: {"fresh": False}
        rc._ensure_background_extraction = lambda: (calls.__setitem__("bg", calls["bg"] + 1), {"running": True})[1]

        out = rc.prepare_requirements_context()
        check(calls["refresh"] == 1, "user_prompts synced inline (cheap)")
        check(calls["bg"] == 1, "background extraction ensured once")
        check(set(out) >= {"sync", "freshness", "extraction"}, "prepare returns sync+freshness+extraction")
    finally:
        for k, v in saved.items():
            setattr(rc, k, v)


def test_ensure_background_injects_paths_and_swallows_already_running() -> None:
    import requirement_context as rc
    from requirement_analysis import cli

    saved_root = rc._requirements_package_root
    saved_launch = cli.launch_background
    captured: dict = {}
    try:
        rc._requirements_package_root = lambda: Path("/pkg/root")
        cli.launch_background = lambda argv, *, extra_pythonpath=None: captured.update(
            argv=argv, extra_pythonpath=extra_pythonpath
        ) or {"pid": 1}

        rc._ensure_background_extraction()
        check(captured.get("argv") in (["--extract", "--background"], None), "launches --extract --background")
        pp = captured.get("extra_pythonpath") or []
        check(not captured or (str(Path("/pkg/root")) in pp and str(ROOT) in pp),
              "injects package root + backend dir into child PYTHONPATH")

        def _raise(*a, **k):
            raise RuntimeError("requirement analysis already running: pid=999 phase=prephase")
        cli.launch_background = _raise
        res = rc._ensure_background_extraction()
        check(res.get("running") is True, "already-running guard is swallowed, not raised")
    finally:
        rc._requirements_package_root = saved_root
        cli.launch_background = saved_launch


def test_launch_env_child_can_import_with_injected_paths() -> None:
    # The real failure mode: the detached child's interpreter lacks the package
    # and backend modules on sys.path. Prove the injected roots are sufficient.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(PKG_ROOT), str(ROOT)])
    proc = subprocess.run(
        [sys.executable, "-c", "import requirement_analysis.cli, portable_lock, requirement_context"],
        env=env, cwd=str(TMP_HOME), capture_output=True, text=True,
    )
    check(proc.returncode == 0,
          f"child imports cli+portable_lock+requirement_context with injected PYTHONPATH (stderr: {proc.stderr[-300:]})")


def test_processor_tool_forces_unprocessed_prompts() -> None:
    src = (PKG_ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
    fn = src.split("def get_requirements_internal", 1)[1].split("def ", 1)[0]
    check("include_unprocessed_prompts=True" in fn,
          "get_requirements_internal forces include_unprocessed_prompts=True")
    check("include_unprocessed_prompts: bool" not in fn,
          "the LLM-controllable include_unprocessed_prompts param is dropped (deterministic)")
    check("provider_native_only: bool = True" in fn,
          "get_requirements_internal defaults to the provider-native corpus")
    check("provider_native_only=provider_native_only" in fn,
          "get_requirements_internal forwards provider_native_only")
    check("compare: bool = False" in fn,
          "get_requirements_internal exposes manual compare mode, off by default")
    check("compare=compare" in fn,
          "get_requirements_internal forwards compare")
    check("Normal agents should use\n            fire_get_requirements and get_requirements_results" in fn,
          "get_requirements_internal points normal agents at the async public tools")
    check("Normal agents should use\n            get_requirements." not in fn,
          "get_requirements_internal does not reference the removed blocking public tool")


def test_index_sql_tool_is_exposed_and_safe() -> None:
    import requirement_context as rc
    from requirement_analysis.processor_spec import GetRequirementsProcessorSpec

    src = (PKG_ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
    check("def query_provider_native_transcript_index(" in src,
          "MCP exposes free-form SQL on the native index")
    check("/api/internal/get-requirements/index-sql" in src,
          "index SQL tool routes through the internal index-sql endpoint")
    tool_fn = src.split("def query_provider_native_transcript_index_response", 1)[1].split("def ", 1)[0]
    check('"sql is required"' in tool_fn, "index SQL tool rejects empty sql")

    calls: list[dict] = []

    def flaky_sql(sql, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"error": "OperationalError: interrupted", "columns": [], "rows": []}
        return {"columns": ["text"], "rows": [["warm row"]], "covered": True, "usable": True}

    import native_transcript_index as idx
    saved = idx.run_readonly_sql
    idx.run_readonly_sql = flaky_sql
    try:
        result = rc.run_native_index_sql("SELECT text FROM native_element_fts LIMIT 1")
    finally:
        idx.run_readonly_sql = saved

    check(result["success"] is True, "index SQL wrapper succeeds after warm retry")
    check(result["rows"] == [["warm row"]], "index SQL wrapper returns rows")
    check(len(calls) == 2 and calls[1].get("timeout_s") == rc.NATIVE_BUNDLE_COLD_RETRY_TIMEOUT_SECONDS,
          "index SQL wrapper retries cold interrupt once with the longer budget")
    check("row_limit" not in calls[0], "index SQL wrapper does not forward a row limit")

    calls.clear()

    def rejected_sql(sql, **kwargs):
        calls.append(kwargs)
        return {"error": "metadata filters with MATCH on native_element_fts are slow", "columns": [], "rows": []}

    idx.run_readonly_sql = rejected_sql
    try:
        rejected = rc.run_native_index_sql(
            "SELECT text FROM native_element_fts WHERE native_element_fts MATCH 'x' AND cwd = '/repo' LIMIT 5"
        )
    finally:
        idx.run_readonly_sql = saved

    check(rejected["success"] is False and "metadata filters with MATCH" in rejected["error"],
          "index SQL wrapper returns rejected SQL errors")
    check(len(calls) == 1, "index SQL wrapper does not retry non-interrupted rejection")

    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    check("/api/internal/get-requirements/index-sql" in main_src,
          "backend exposes the internal index-sql endpoint")
    check("run_native_index_sql" in main_src, "endpoint routes to the SQL wrapper")
    processor_instructions = GetRequirementsProcessorSpec().build_instructions("chat panel", {"cwd": "/repo"})
    check("Use native_element_fts for text-only MATCH searches" in processor_instructions,
          "processor instructs text-only FTS MATCH")
    check("Do not put metadata filters such as cwd, path" in processor_instructions,
          "processor blocks metadata filters directly on FTS MATCH")


def test_assistant_uses_shared_native_transcript_tool_only() -> None:
    assistant_root = REPO / "better-agent-private" / "extensions" / "assistant"
    prompt = (assistant_root / "prompts" / "system.md").read_text(encoding="utf-8")
    server = (assistant_root / "mcp" / "server.py").read_text(encoding="utf-8")
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")

    check("query_provider_native_transcript_index" in prompt,
          "assistant prompt uses the shared native transcript query tool")
    check("search_in_native_sessions" not in prompt,
          "assistant prompt does not reference the old transcript query tool")
    check("def search_in_native_sessions(" not in server,
          "assistant MCP no longer exposes a duplicate transcript query tool")
    check("/api/internal/assistant-ui/search-native-sql" not in main_src,
          "backend no longer exposes the assistant-only native SQL route")
    check("resolve_ba_session" in server and "adopt_native_session" in server,
          "assistant keeps native session resolve/adopt helpers")


def test_public_tool_guidance_asks_for_task_description() -> None:
    skill = (PKG_ROOT / "skills" / "get-requirements" / "SKILL.md").read_text(encoding="utf-8")
    server = (PKG_ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
    public_fn = server.split("def fire_get_requirements(", 1)[1].split("def get_requirements_results", 1)[0]

    check("task you are about to start" in skill,
          "get-requirements skill asks callers for the task they are about to start")
    check("fire_get_requirements" in skill and "get_requirements_results" in skill,
          "get-requirements skill directs callers through the async MCP tools")
    check("wait=False" in skill and "wait=True" in skill,
          "get-requirements skill explains fire wait modes")
    check("1-3 minutes" in skill,
          "get-requirements skill warns the async lookup can take 1-3 minutes")
    check("guardrails throughout the work" in skill,
          "get-requirements skill tells agents to use requirements throughout the work")
    check("not generic search keywords" in skill,
          "get-requirements skill rejects generic keyword queries")
    check("origin" in skill and "decisive evidence provenance" in skill,
          "get-requirements skill explains origin provenance")
    check("concrete task the caller is about to start" in public_fn,
          "public MCP description asks for the concrete task")
    check("concise task description" in public_fn,
          "public MCP description asks for a concise task description")
    check("wait: bool = False" in public_fn,
          "public MCP fire tool exposes wait=False by default")
    check("wait=False" in public_fn and "wait=True" in public_fn,
          "public MCP description explains fire wait modes")
    check("1-3 minutes" in public_fn,
          "public MCP description warns the async lookup can take 1-3 minutes")
    check("not generic search keywords" in public_fn,
          "public MCP description rejects generic keyword queries")
    check("origin for decisive" in public_fn,
          "public MCP description explains origin provenance")


def test_native_bundle_sql_retries_once_on_cold_interrupt() -> None:
    import native_transcript_index as idx
    import requirement_context as rc

    saved = idx.run_readonly_sql
    calls: list[float | None] = []

    def flaky_run(sql, params=(), *, timeout_s=None):
        calls.append(timeout_s)
        if len(calls) == 1:
            return {"error": "OperationalError: interrupted", "columns": [], "rows": []}
        return {
            "columns": ["hit_index", "text", "path", "element_index"],
            "rows": [[1, "warm retry row", "/p.jsonl", 1]],
            "covered": True,
            "usable": True,
        }

    idx.run_readonly_sql = flaky_run
    try:
        rows = rc._native_transcript_sql_window_rows(
            idx, tokens=["needle"], cwds=("/repo",), limit=2,
        )
    finally:
        idx.run_readonly_sql = saved

    check(len(calls) == 2, "interrupted first query retries exactly once")
    check(calls[1] == rc.NATIVE_BUNDLE_COLD_RETRY_TIMEOUT_SECONDS,
          "retry uses the longer cold-cache SQL budget")
    check(rows and rows[0]["text"] == "warm retry row", "retry result is returned")

    calls.clear()

    def always_interrupted(sql, params=(), *, timeout_s=None):
        calls.append(timeout_s)
        return {"error": "OperationalError: interrupted", "columns": [], "rows": []}

    idx.run_readonly_sql = always_interrupted
    try:
        try:
            rc._native_transcript_sql_window_rows(
                idx, tokens=["needle"], cwds=("/repo",), limit=2,
            )
            raised = False
        except RuntimeError:
            raised = True
    finally:
        idx.run_readonly_sql = saved

    check(len(calls) == 2, "persistent interrupt does not retry more than once")
    check(raised, "persistent interrupt still fails loudly after the retry")


def test_native_transcript_bundle_lookup_uses_indexed_rowids() -> None:
    import json

    import native_session_prompt_search as nsp
    import native_transcript_index as idx
    import requirement_context as rc
    from paths import encode_cwd

    scratch = TMP_HOME / "native-bundle"
    claude = scratch / "claude-projects"
    shutil.rmtree(scratch, ignore_errors=True)
    path = claude / encode_cwd("/repo") / "native-bundle-sid.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        json.dumps({
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "nativebundle needle alpha"},
        }),
        json.dumps({
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "assistant", "content": "reply context beta"},
        }),
    ]) + "\n", encoding="utf-8")

    saved_roots = nsp._native_roots
    nsp._native_roots = lambda: [(claude, "claude")]
    idx.reset_for_test()
    try:
        idx.refresh_once()
        result = rc._native_transcript_bundle_records(
            query="nativebundle needle",
            cwds=("/repo",),
            limit=4,
        )
    finally:
        nsp._native_roots = saved_roots
        idx.reset_for_test()

    matches = result.get("matches") or []
    text = matches[0]["text"] if matches else ""
    check(result.get("searched") is True, "native transcript bundle search runs")
    check(len(matches) == 1, "native transcript bundle returns one hit")
    check("nativebundle needle alpha" in text and "reply context beta" in text,
          "native transcript bundle includes hit window")


def test_requirements_query_executors_are_split() -> None:
    """The public processor path re-enters /search via the
    get_requirements_internal MCP tool; sharing one bounded pool between the
    two endpoints self-deadlocks under >=2 concurrent public calls. The
    processor (reentrant, long-running) and search (leaf) paths must use
    distinct pools so a processor worker never waits on a slot it holds."""
    from concurrent.futures import ThreadPoolExecutor

    import requirements_query_runner as runner

    check(
        isinstance(runner.REQUIREMENTS_PROCESSOR_EXECUTOR, ThreadPoolExecutor),
        "processor executor is a ThreadPoolExecutor",
    )
    check(
        isinstance(runner.REQUIREMENTS_SEARCH_EXECUTOR, ThreadPoolExecutor),
        "search executor is a ThreadPoolExecutor",
    )
    check(
        runner.REQUIREMENTS_PROCESSOR_EXECUTOR is not runner.REQUIREMENTS_SEARCH_EXECUTOR,
        "processor and search endpoints use distinct pools (no self-deadlock)",
    )


def test_large_index_sql_result_spills_to_file() -> None:
    spec = importlib.util.spec_from_file_location("requirements_mcp_server_spill_test", PKG_ROOT / "mcp" / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
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


def test_reentrant_search_does_not_deadlock() -> None:
    """Behavioral lock: a processor task on the processor pool that nests a
    /search on the search pool completes for several concurrent processors.
    Under the old single-pool design two processors would saturate the pool
    and the nested search would starve forever."""
    import asyncio

    import requirements_query_runner as runner

    async def _processor_like(idx: int) -> str:
        async def _search_part() -> str:
            return await runner.run_requirements_query(
                f"search.{idx}",
                lambda: f"search-{idx}",
                executor=runner.REQUIREMENTS_SEARCH_EXECUTOR,
            )

        def _processor_work() -> str:
            # The processor runs sync and waits for its nested /search result,
            # exactly like provisioning.run_sync blocks the processor worker.
            return asyncio.run(_search_part())

        return await runner.run_requirements_query(
            f"processed.{idx}",
            _processor_work,
            executor=runner.REQUIREMENTS_PROCESSOR_EXECUTOR,
        )

    async def _main() -> list[str]:
        return await asyncio.gather(*(_processor_like(i) for i in range(4)))

    try:
        results = asyncio.run(asyncio.wait_for(_main(), timeout=10.0))
        ok = sorted(results) == [f"search-{i}" for i in range(4)]
    except asyncio.TimeoutError:
        ok = False
    check(ok, "reentrant processor->search completes without pool self-deadlock")


def test_processor_query_returns_success_before_result_timeout() -> None:
    import asyncio

    import requirements_query_runner as runner

    async def _main() -> dict:
        return await runner.run_requirements_processor_query(
            "processor.success",
            lambda: {"requirements": [{"text": "semantic result"}]},
            executor=runner.REQUIREMENTS_PROCESSOR_EXECUTOR,
            result_timeout_seconds=1.0,
        )

    result = asyncio.run(_main())
    check(result["requirements"][0]["text"] == "semantic result",
          "processor query returns semantic result before public timeout")


def test_processor_query_times_out_before_full_processor_budget() -> None:
    import asyncio
    import time

    import requirements_query_runner as runner

    async def _main() -> tuple[float, str]:
        started = time.perf_counter()
        try:
            await runner.run_requirements_processor_query(
                "processor.timeout",
                lambda: time.sleep(0.25) or {"requirements": []},
                executor=runner.REQUIREMENTS_PROCESSOR_EXECUTOR,
                result_timeout_seconds=0.02,
            )
        except TimeoutError as exc:
            return time.perf_counter() - started, str(exc)
        return time.perf_counter() - started, ""

    elapsed, error = asyncio.run(_main())
    check(error == "get-requirements processor timed out before returning requirements",
          "processor query reports explicit result timeout")
    check(elapsed < 0.15, "processor query does not wait for full processor work")


def test_internal_get_requirements_timeout_returns_failure_response() -> None:
    import asyncio

    import main

    saved = {
        "prepare": main.run_requirements_query,
        "processor": main.run_requirements_processor_query,
        "runtime_gate": main._require_builtin_runtime_extension,
    }
    calls: list[str] = []

    async def fake_query(name, fn, /, *, executor, **kwargs):
        calls.append(name)
        if name == "requirements.processed.prepare":
            return {"success": True}
        return fn(**kwargs)

    async def fake_processor(*_args, **_kwargs):
        calls.append("processor.timeout")
        raise TimeoutError("forced processor timeout")

    main.run_requirements_query = fake_query
    main.run_requirements_processor_query = fake_processor
    main._require_builtin_runtime_extension = lambda _extension_id: None
    try:
        result = asyncio.run(main.internal_get_requirements(
            {"query": "processor timeout", "cwd": "/repo", "max_matches": 3},
            x_internal_token=main.coordinator.internal_token,
        ))
    finally:
        main.run_requirements_query = saved["prepare"]
        main.run_requirements_processor_query = saved["processor"]
        main._require_builtin_runtime_extension = saved["runtime_gate"]

    check(calls == [
        "requirements.processed.prepare",
        "processor.timeout",
        "requirements.processed.finalize",
    ], "internal get-requirements finalizes after processor timeout")
    check(result["success"] is False, "endpoint timeout returns failure response")
    check("processor timed out" in result.get("error", ""),
          "endpoint timeout response keeps explicit timeout reason")


def run() -> None:
    test_greedy_packing_respects_capacity_and_cap()
    test_milp_failure_falls_back_to_greedy()
    test_query_path_has_no_inline_extraction()
    test_requirements_query_executors_are_split()
    test_reentrant_search_does_not_deadlock()
    test_processor_query_returns_success_before_result_timeout()
    test_processor_query_times_out_before_full_processor_budget()
    test_internal_get_requirements_timeout_returns_failure_response()
    test_public_get_requirements_keeps_processor_off_sync_path()
    test_processor_timeout_response_fails_without_fallback()
    test_processor_readtimeout_response_fails_without_fallback()
    test_mcp_timeout_fails_without_fallback()
    test_requirements_processor_mcp_hides_recursive_tools()
    test_requirements_processor_spec_sets_restricted_tool_profile()
    test_mcp_timeout_result_fails_without_fallback()
    test_mcp_transport_failure_returns_error()
    test_raw_search_keeps_processor_off_sync_path()
    test_provider_native_only_search_skips_unit_corpus()
    test_search_defaults_to_provider_native_corpus()
    test_compare_mode_runs_both_paths_and_diffs()
    test_processor_prompt_is_available_to_running_backend()
    test_processor_dispatch_is_isolated_and_timeout_budgeted()
    test_processor_spec_fails_closed_without_private_registration()
    test_prepare_orchestration_is_cheap_and_nonblocking()
    test_ensure_background_injects_paths_and_swallows_already_running()
    test_launch_env_child_can_import_with_injected_paths()
    test_processor_tool_forces_unprocessed_prompts()
    test_index_sql_tool_is_exposed_and_safe()
    test_assistant_uses_shared_native_transcript_tool_only()
    test_public_tool_guidance_asks_for_task_description()
    test_native_bundle_sql_retries_once_on_cold_interrupt()
    test_native_transcript_bundle_lookup_uses_indexed_rowids()
    test_large_index_sql_result_spills_to_file()


if __name__ == "__main__":
    try:
        run()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        sys.exit(1)
    print("\nALL PASSED")
